# Email Identity Deduplication in Join Research Flow — Static Investigation

Date: 2026-05-09
Scope: backend routes, services, models, and migrations only. No manual browser/device testing and no implementation changes.

## Executive summary

| Finding | Status | Short answer |
| --- | --- | --- |
| Duplicate `users` rows for the same normalized email | **Safe for lowercase duplicates; Unclear for whitespace/canonical normalization** | Current join-research paths lower-case email and rely on `users.email` unique plus Postgres upsert, so repeated lowercase-equivalent submissions converge to one user. The code does not explicitly trim before storage, and the DB has no functional unique index on `lower(trim(email))`. |
| Multiple valid magic-link tokens for same email/user | **Unsafe** | Token issuance inserts a new `magic_link_tokens` row and does not invalidate previous unredeemed/unexpired rows. Multiple valid tokens can exist until redeemed/expired/rate-limited. |
| Token activation creates user without existing-user check | **Safe for duplicate rows, but redundant** | Activation performs `INSERT ... ON CONFLICT DO UPDATE` against `users.email`, so it is get-or-create/upsert rather than unconditional create. It resolves the token row first, but then uses `token_row.email` rather than only `token_row.user_id`. |
| Multiple active simulated passes for same `user_id` | **Unsafe** | Activation inserts `simulated_user_passes` unconditionally for the latest pending simulated intent; the schema has only non-unique indexes, no one-active-pass constraint. |
| `unlock-intent` and `magic-link` share user-creation codepath | **Unsafe / divergence risk** | `/api/billing/unlock-intent` independently upserts `User` before creating a simulated intent, then calls `MagicAuthService.create_magic_link`, which upserts `User` again. `/api/auth/magic-link` eventually uses the service for simulated sends, but its lookup/rate-limit/resend logic is route-local. |
| PostHog identity after activation on two devices | **Unclear — needs follow-up** | Server-side PostHog captures use backend `user_id`, and the rendered page calls `posthog.identify(user_id)` when authenticated. There is no `posthog.alias` call found, so anonymous pre-login identities are not explicitly merged. Two devices should identify to the same backend `user_id` after reload, but pre-login anonymous identities may remain separate. |
| DB-level dedup constraints | **Safe for raw `users.email`; Unsafe/Unclear for normalized variants and active passes/tokens** | `users.email` is unique. `magic_link_tokens` uniqueness is only on `token_hash`. `simulated_user_passes` lacks uniqueness for active pass per user or per simulated intent. |

## 1. User creation

### Email normalization

**Finding: Unclear — needs follow-up.** Application code consistently lowercases join-research emails, but it does not explicitly call `.strip()` before lookup/insert in the inspected codepaths.

- `/api/billing/unlock-intent` logs and stores `email = payload.email.lower()` and then uses that value in the user upsert. References: `app/api/billing.py:67-75`, `app/api/billing.py:110-117`, `app/api/billing.py:138-146`.
- `/api/auth/magic-link` implementation assigns `email = payload.email.lower()` before rate limits and lookups. References: `app/api/auth.py:342-360`, `app/api/auth.py:394-418`.
- `MagicAuthService.create_magic_link` normalizes with `normalized_email = email.lower()` before upserting the user and inserting the token. References: `app/services/magic_auth_service.py:210-221`, `app/services/magic_auth_service.py:225-248`.
- Token activation lowercases `token_row.email` in the upsert. References: `app/api/auth.py:812-827`; service-level `redeem_token` does the same at `app/services/magic_auth_service.py:281-293`.
- Request schemas use Pydantic `EmailStr`, but no schema-level trim/canonicalization is visible in these files. References: `app/api/auth.py:35-50`, `app/schemas/billing.py:16-20`.

### Get-or-create vs unconditional create

**Finding: Safe.** User creation in the relevant join-research paths uses Postgres upsert/get-or-create semantics rather than unconditional inserts.

- `MagicAuthService.create_magic_link` uses `pg_insert(User).values(...).on_conflict_do_update(index_elements=[User.email])...returning(User.id)`. References: `app/services/magic_auth_service.py:225-237`.
- `/api/billing/unlock-intent` independently uses the same `pg_insert(User)...on_conflict_do_update(index_elements=[User.email])...returning(User.id)`. References: `app/api/billing.py:138-146`.
- `/api/auth/magic` activation also uses `pg_insert(User)...on_conflict_do_update(index_elements=[User.email])...returning(User.id, User.email)`. References: `app/api/auth.py:812-831`.
- The deprecated `/api/auth/login` delegates to `_resend_magic_link_impl`, so it does not have a separate unconditional user insert in this route. References: `app/api/auth.py:752-767`.

### DB uniqueness on `users.email` or normalized variant

**Finding: Safe for exact stored `email`; Unclear for normalized variant.** There is a DB/model uniqueness constraint on raw `users.email`, but no functional lower/trim unique constraint was found in the inspected schema/model.

- ORM model declares `email` as `nullable=False, unique=True`. Reference: `app/models/models.py:27-35`.
- Initial migration creates `users.email` and `sa.UniqueConstraint('email')`. References: `alembic/versions/22cf0ac436cd_v1_initial_schema.py:76-86`.
- No inspected model/migration creates a unique index on `lower(email)`, `trim(email)`, or `lower(trim(email))`; the upserts target `User.email` directly. References: `app/services/magic_auth_service.py:225-237`, `app/api/billing.py:138-146`, `app/api/auth.py:812-827`.

### Is uniqueness application-only, and present everywhere?

**Finding: Safe for inspected join-research codepaths because DB uniqueness exists; Unclear for all possible user creation outside the searched surface.** Deduplication is not application-only for raw email, because the DB unique constraint backs the `ON CONFLICT` paths. However, lowercasing is application logic and would not protect against already-existing non-lowercase or whitespace variants if any non-normalizing path exists.

- DB uniqueness backs the upsert target: `users.email` unique in model/migration. References: `app/models/models.py:27-35`, `alembic/versions/22cf0ac436cd_v1_initial_schema.py:76-86`.
- The inspected join-research issuance and activation paths lowercase before insert/upsert. References: `app/services/magic_auth_service.py:220-248`, `app/api/billing.py:110-146`, `app/api/auth.py:812-827`.

## 2. Magic-link token behavior

### Are previous tokens invalidated when a new token is created?

**Finding: Unsafe.** New token creation inserts a fresh `magic_link_tokens` row. No update is performed to mark older unredeemed tokens for the same email/user as redeemed/expired.

- `MagicAuthService.create_magic_link` creates a fresh token hash and inserts `MagicLinkToken` with `user_id`, `email`, `token_hash`, and `expires_at`; there is no preceding update for prior rows. References: `app/services/magic_auth_service.py:220-248`.
- The route-local active-pass resend path also inserts a new `MagicLinkToken` directly, with no invalidation of prior tokens. References: `app/api/auth.py:670-683`.
- The token table has uniqueness on `token_hash` only and a non-unique email/created index. References: `app/models/models.py:226-245`, `alembic/versions/22cf0ac436cd_v1_initial_schema.py:88-104`.

### Can multiple tokens for the same user/email be valid simultaneously?

**Finding: Unsafe.** Yes, the schema and creation path allow multiple unredeemed, unexpired rows for the same `user_id`/`email`.

- Validity on activation is checked per token row: missing row, non-null `redeemed_at`, or expired `expires_at` causes rejection. No same-user/email sibling-token check is made. References: `app/api/auth.py:793-810`; service-level redeem path mirrors this at `app/services/magic_auth_service.py:263-279`.
- The schema has no unique constraint on `(user_id, redeemed_at)` or `(email, redeemed_at)` and only indexes email/created, expires, and redeemed timestamps. References: `app/models/models.py:226-245`, `alembic/versions/bf88275af98e_add_retention_indexes.py:21-27`.

### Rate limit on token issuance per email per time window

**Finding: Safe in `/api/auth/magic-link`; Unclear/Unsafe for `/api/billing/unlock-intent`.** `/api/auth/magic-link` has Redis limits keyed by email and IP. `/api/billing/unlock-intent` creates and sends a magic link through the service without those route-level Redis rate-limit keys.

- `_resend_magic_link_impl` uses `magic_resend:cooldown:{email}`, `magic_resend:count:{email}:{ip}`, and `magic_ip_limit:{ip}`; it allows up to 2 email+IP sends in 180 seconds and up to 10 per IP per hour before returning the generic response. References: `app/api/auth.py:394-418`.
- `/api/billing/unlock-intent` calls `MagicAuthService.create_magic_link(email=email)` after intent creation, but the service itself does not contain a rate-limit check. References: `app/api/billing.py:248-290`, `app/services/magic_auth_service.py:210-248`.

### Token tied to `user_id` or only email

**Finding: Safe for issuance; Unclear/redundant on activation.** Token records contain both `user_id` and `email`, and issuance sets both. Activation, however, does not use `token_row.user_id` as the authoritative identity; it upserts by `token_row.email`.

- ORM model includes nullable `user_id` FK and required `email`. References: `app/models/models.py:226-231`.
- Initial migration also makes `user_id` nullable and adds the FK. References: `alembic/versions/22cf0ac436cd_v1_initial_schema.py:88-100`.
- `MagicAuthService.create_magic_link` upserts the user, captures `user_id`, then inserts the token with both `user_id` and normalized `email`. References: `app/services/magic_auth_service.py:225-248`.
- Direct route insertion for active-pass resend also writes both `user_id=active_pass["user_id"]` and `email=email`. References: `app/api/auth.py:670-683`.
- Activation uses `token_row.email.lower()` for a user upsert instead of directly resolving `token_row.user_id`. References: `app/api/auth.py:812-831`; service-level `redeem_token` mirrors this at `app/services/magic_auth_service.py:281-293`.

### Activation behavior: token → user_id or token.email → create user

**Finding: Safe for duplicate rows because of upsert, but not strictly token→user_id.** Activation first finds the token by hash and marks that token redeemed, then performs a user upsert using `token_row.email.lower()`.

- Route activation resolves `token_hash`, rejects redeemed/expired tokens, updates that token's `redeemed_at`, and then upserts `User` from `token_row.email.lower()`. References: `app/api/auth.py:793-831`.
- The service-level `redeem_token` follows the same pattern. References: `app/services/magic_auth_service.py:263-293`.

## 3. Simulated pass creation

### Conditional vs unconditional pass creation on activation

**Finding: Unsafe.** Magic-link activation inserts a new `simulated_user_passes` row whenever it finds a pending simulated intent for the activated user. It does not first query for an existing active simulated pass.

- Activation selects the latest `SimulatedPaymentIntent` for the user with status `initiated` or `magic_sent`. References: `app/api/auth.py:833-843`.
- If found, activation updates that intent to `activated` and inserts a new `SimulatedUserPass` with `status='active'` and a new `expires_at`. References: `app/api/auth.py:844-866`.
- There is no active simulated-pass lookup before the insert in this block. References: `app/api/auth.py:833-866`.

### DB constraint preventing multiple active simulated passes for the same `user_id`

**Finding: Unsafe.** No DB-level uniqueness prevents multiple active simulated passes per user.

- ORM table args only include a status check and non-unique indexes on `(user_id, expires_at)`, `(status, expires_at)`, `simulated_intent_id`, and `updated_at`. References: `app/models/models.py:322-358`.
- Initial migration creates the table with primary key and FKs but no unique constraints for user/status or simulated intent. References: `alembic/versions/22cf0ac436cd_v1_initial_schema.py:232-251`.
- Retention migration adds only a non-unique updated-at index. References: `alembic/versions/bf88275af98e_add_retention_indexes.py:21-27`.

### Behavior if an existing active pass is found

**Finding: Unsafe/Not implemented for simulated passes.** The activation path does not look for an existing active `simulated_user_passes` row, so there is no reuse/extend/ignore behavior for simulated passes. It always inserts for the pending simulated intent found.

- Simulated activation block selects intent, updates intent, inserts pass, and proceeds to funnel events. References: `app/api/auth.py:833-866`, `app/api/auth.py:918-970`.
- Note: a separate `/api/auth/magic-link` resend branch checks `user_passes` for paid active passes, not `simulated_user_passes`. References: `app/api/auth.py:446-461`.

## 4. Route convergence

### `/api/auth/magic-link`, `/api/auth/magic`, and `/api/billing/unlock-intent`

**Finding: Unsafe / divergence risk.** These routes do not fully share one user-creation/token/pass codepath.

- `/api/auth/magic-link` delegates to `_resend_magic_link_impl`, which does route-local Redis rate limiting, simulated-intent lookup, paid-pass lookup, and may call `_send_magic_link_email`; `_send_magic_link_email` then instantiates `MagicAuthService` and calls `create_magic_link`. References: `app/api/auth.py:294-323`, `app/api/auth.py:342-418`, `app/api/auth.py:507-550`, `app/api/auth.py:722-724`.
- `/api/auth/login` delegates to `_resend_magic_link_impl`, so it converges with `/api/auth/magic-link` for issuance. References: `app/api/auth.py:752-767`.
- `/api/auth/magic` has its own activation implementation instead of using `MagicAuthService.redeem_token`; it performs token lookup/redeem, user upsert, simulated intent activation, simulated pass insert, session creation, and cache busting in-route. References: `app/api/auth.py:776-831`, `app/api/auth.py:833-866`, `app/api/auth.py:982-1026`.
- `/api/billing/unlock-intent` independently upserts `User`, creates `SimulatedPaymentIntent`, records funnel events, then separately instantiates `MagicAuthService` and calls `create_magic_link`, which repeats a user upsert and inserts a token. References: `app/api/billing.py:120-146`, `app/api/billing.py:175-215`, `app/api/billing.py:248-290`, `app/services/magic_auth_service.py:225-248`.
- No `/api/auth/resend` route decorator was found in `app/api/auth.py`; the visible resend endpoint is `/api/auth/magic-link`. References: `app/api/auth.py:722-724`.

## 5. Analytics identity

### Is PostHog `identify` called with stable backend `user_id` after activation?

**Finding: Safe for authenticated page render.** After magic activation, the route creates a Redis session with `user_id` and sets the session cookie. The root template receives `request.state.user_id` from middleware and calls `posthog.identify(user_id)` when present.

- Magic activation session data stores `user_id` and `email`, then sets the configured session cookie. References: `app/api/auth.py:982-1014`.
- Identity middleware resolves the session cookie and sets `request.state.user_id` from session data. References: `app/middleware/identity.py:30-57`.
- Root context passes `user_id` and `anon_id` into the template. References: `app/main.py:448-454`.
- Template calls `posthog.identify('{{ user_id }}')` if `user_id` exists; otherwise it identifies `anon_id`. References: `templates/index.html:52-62`.
- Server-side PostHog capture helper uses `distinct_id=str(user_id)`, and simulated activation events call it with the backend UUID. References: `app/services/analytics.py:19-31`, `app/api/auth.py:918-970`.

### Is anonymous pre-login session aliased to authenticated user?

**Finding: Unsafe/Unclear — no alias found.** No `posthog.alias(...)` call was found in the searched backend/static/template code. The backend records `anon_id` in funnel events and carries quota forward, but PostHog aliasing is not explicitly performed.

- Template initializes PostHog and calls `identify`, but no alias call is present there. References: `templates/index.html:52-62`.
- Search found PostHog capture calls and the stub list includes an `alias` method name from the library snippet, but no application-level `posthog.alias(...)` invocation was found. References: `templates/index.html:51-62`, `static/app.js:143-160`, `app/services/analytics.py:19-31`.
- Activation deletes the anonymous cookie after setting the session cookie but does not alias PostHog IDs. Reference: `app/api/auth.py:1003-1017`.

### If same email activates links on two devices, one PostHog identity or two?

**Finding: Unclear — needs follow-up.** Based on static code, post-activation page loads should identify both devices as the same backend `user_id`, but each device's pre-login anonymous identity may remain separate because no alias/merge call is present.

- Same backend `user_id` should be obtained from the unique-email upsert during activation. References: `app/api/auth.py:812-831`, `app/models/models.py:27-35`.
- Both authenticated devices should render with the session-derived `user_id` and call `posthog.identify(user_id)`. References: `app/middleware/identity.py:30-57`, `app/main.py:448-454`, `templates/index.html:52-62`.
- Pre-login anonymous identities are generated per browser/device when no anon cookie exists, and the template identifies `anon_id` before login. References: `app/middleware/identity.py:63-115`, `templates/index.html:58-62`.
- Because no application-level `posthog.alias` call was found, the relationship between each anonymous pre-login identity and the backend user identity depends on PostHog client behavior, not explicit code in this repo. References: `templates/index.html:52-62`, `app/api/auth.py:1003-1017`.

## Scenario answer

For the requested scenario (`test@example.com` submitted on Browser A and Browser B, both links activated):

1. **Multiple `users` rows?** **Safe for exact/lowercase normalized email.** The relevant code lowercases and uses `ON CONFLICT (email) DO UPDATE`, backed by `users.email` unique. References: `app/services/magic_auth_service.py:220-248`, `app/api/auth.py:812-831`, `app/api/billing.py:138-146`, `app/models/models.py:27-35`, `alembic/versions/22cf0ac436cd_v1_initial_schema.py:76-86`.
2. **Multiple valid tokens?** **Unsafe.** Repeated issuance can create multiple unredeemed/unexpired tokens for the same email/user; no invalidation or same-email uniqueness exists. References: `app/services/magic_auth_service.py:220-248`, `app/api/auth.py:793-810`, `app/models/models.py:226-245`.
3. **Multiple active simulated passes?** **Unsafe.** Repeated unlock intents create multiple pending simulated intents for the same user, and activation inserts a simulated pass per pending intent without checking existing active passes; the DB does not prevent multiple active rows. References: `app/api/billing.py:175-185`, `app/api/auth.py:833-866`, `app/models/models.py:322-358`, `alembic/versions/22cf0ac436cd_v1_initial_schema.py:232-251`.
4. **Multiple analytics identities?** **Unclear.** Server captures and authenticated page loads use backend `user_id`, but anonymous pre-login sessions are per browser/device and are not explicitly aliased to the authenticated identity. References: `app/services/analytics.py:19-31`, `templates/index.html:52-62`, `app/middleware/identity.py:63-115`, `app/api/auth.py:1003-1017`.
