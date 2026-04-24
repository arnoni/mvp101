# Quota/Tier Audit (2026-04-22)

## Key findings

- A/B cohort is stored on `users.ab_cohort`.
- Free cohort limits are looked up from `free_quotas.daily_limit` by joining `users.ab_cohort = free_quotas.cohort`.
- Entitlement resolution currently checks simulated passes first, then free cohort fallback.
- Entitlement resolution does **not** currently query `user_passes` + `billing_plans` directly.
- Real paid limits are sourced from `billing_plans.daily_limit` at purchase/webhook processing time and cached in Redis entitlement payload.

## SQL used in code

```sql
SELECT to_regclass('public.free_quotas') IS NOT NULL AS exists;
```

```sql
SELECT u.ab_cohort, fq.daily_limit
FROM users u
LEFT JOIN free_quotas fq ON fq.cohort = u.ab_cohort
WHERE u.id = :user_id
LIMIT 1;
```

```sql
SELECT duration_days, daily_limit
FROM billing_plans
WHERE code = :code
LIMIT 1;
```

## Potential startup/timeline conflict

If Redis entitlement cache is empty or stale for a user with an active real pass, `EntitlementService.get_tier` currently returns FREE because it doesn't query `user_passes`. This can temporarily under-assign tier/quota until another paid flow refreshes cache.
