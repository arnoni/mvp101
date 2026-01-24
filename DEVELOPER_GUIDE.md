# Developer Introduction & Architecture Guide

Welcome to the **DillDrill** codebase! This guide is designed to help new developers understand the project structure, the Domain-First architecture, and the specific requirements of Technical Design Document (TDD) v1.1.

## 1. Project Overview

**DillDrill** is a "URL-First Radar" for detecting construction noise and real-estate points of interest (POIs).

**Key Philosophy (TDD v1.1):**
*   **Privacy-First:** We track generic "Anonymous IDs" rather than user accounts.
*   **Domain-Driven:** Core logic resides in services, not in the API routes.
*   **Lat/Lng Native:** Users (or the frontend) provide raw coordinates. We do *not* geocode server-side (Mapbox integration has been removed).
*   **Tiered Access:** A "Policy Engine" governs who can see what and how often.

## 2. High-Level Architecture

The application follows a **Service-Layer Pattern** built on FastAPI.

```mermaid
graph TD
    Client[Client / Frontend] -->|HTTP Request| Middleware[AnonIdMiddleware]
    Middleware -->|Standardized Request| Route[API Routes]
    
    Route -->|Context| PolicyEngine[Policy Engine]
    
    subgraph "Domain Core"
        PolicyEngine -->|Check| QuotaRepo[Quota Repository (Redis)]
        PolicyEngine -->|Check| Entitlement[Entitlement Service]
        
        Route -->|If Allowed| POIService[POI Service]
        POIService -->|Read| MasterList[MasterList.json]
    end
    
    POIService -->|Results| Route
    Route -->|Response| Client
```

## 3. Module Guide

### 3.1 Core (`app/core/`)
*   **`config.py`**: Centralized configuration using Pydantic `BaseSettings`. Handles environment variables, feature flags (e.g., `ENABLE_REDIS`), and constants like `DA_NANG_BBOX`.
*   **`middleware.py`**:
    *   **`AnonIdMiddleware`**: Inspects every request. If a `dd_anon_id` cookie is missing, it generates a new UUID and sets the cookie. This ID is critical for the `PolicyEngine` to track usage.

### 3.2 Services (`app/services/`)
This is where the business logic lives.

*   **`policy_engine.py` (The Brain):**
    *   **Responsibility:** Decides *if* a request can proceed.
    *   **Logic:** Checks User Tier (Free vs. Paid) -> Checks Quota -> Checks for required Friction (Turnstile).
    *   **Output:** Returns a `PolicyDecision` (ALLOW, BLOCK, CHALLENGE_REQUIRED).
    *   **Key Concept:** It does *not* execute the search; it only guards the door.

*   **`poi_service.py` (The Search):**
    *   **Responsibility:** Finds relevant data.
    *   **Algorithm (Greedy 30m):**
        1.  Find all POIs within 100m.
        2.  Sort by distance.
        3.  Pick the closest.
        4.  Pick the next closest *only if* it is >30m away from *all* already picked points.
    *   **Data Source:** Loads `static/masterlist.json` into memory on startup.

*   **`quota_repository.py` (The State):**
    *   **Responsibility:** specific usage tracking.
    *   **Implementation:** Primary backing is **Upstash Redis**. If Redis fails or is disabled via config, it gracefully degrades to an in-memory dictionary.
    *   **Async:** Fully async operation.

*   **`area_bucketer.py`:**
    *   **Responsibility:** Privacy and caching logic. Converts a precise float Lat/Lng into a coarse "Area Code" string. Used for aggregating usage stats without tracking precise user locations. (Currently stubbed to 3 decimal places).

*   **`entitlement_service.py`:**
    *   **Responsibility:** Determines if a user is `tier: FREE` or `tier: PAID`.
    *   **Current State:** Checks for a `dd_paid_session` cookie.

*   **`kmz_service.py`:**
    *   **Responsibility:** Generates Google Earth (`.kmz`) files dynamically from search results.

*   **`i18n.py`:**
    *   **Responsibility:** Simple in-memory translation service for the server-rendered frontend (supports EN, ES, RU, KO).

### 3.3 API (`app/api/`)
*   **`routes.py`**:
    *   **`/api/find-nearest`**: The main search endpoint. It accepts Lat/Lng, invokes the Policy Engine, and if allowed, calls the POI Service.
    *   **`/download-kmz`**: Generates a file download based on the *previous* search. Counts as a "read" against the user's quota.

### 3.4 Utils (`app/utils/`)
*   **`security.py`**: Handles Cloudflare Turnstile verification.
*   **`haversine.py`**: Calculates distances between coordinates.

## 4. TDD v1.1 Specification Highlights

If you are modifying the code, ensure you adhere to these strict rules from the TDD:

1.  **Input:** The API *must* accept `lat` and `lon` (float). Do not accept address strings (Geocoding removed).
2.  **Quota:** Every search consumes 1 unit of quota. The Policy Engine must strictly enforce:
    *   **Free Tier:** 2 reads / day.
    *   **Paid Tier:** 50 reads / day.
3.  **Friction:** We use Cloudflare Turnstile.
    *   If the Policy Engine returns `CHALLENGE_REQUIRED`, the client must present a valid `turnstile_token`.
4.  **Privacy:** Never log precise coordinates associated with a user ID. Use `AreaBucketer` if you need to aggregate spatial data.
5.  **Logging:** Use `structlog` for structured logging. Do not use standard `logging` directly for application logic.

## 5. Getting Started

1.  **Environment Variables:** Ensure your `.env` file has:
    ```env
    UPSTASH_REDIS_REST_URL="https://..."
    UPSTASH_REDIS_REST_TOKEN="..."
    CLOUDFLARE_TURNSTILE_SECRET="your-secret"
    CLOUDFLARE_TURNSTILE_SITE_KEY="your-public-key"
    ENV="development" # or "production"
    ```
2.  **Run Locally:**
    ```bash
    uvicorn app.main:app --reload
    ```
3.  **Testing Quotas:**
    *   The app uses cookies. To reset your identity/quota locally, delete the `dd_anon_id` cookie in your browser dev tools.

## 6. Directory Structure

```text
.
├── app/
│   ├── api/            # Routes & endpoints
│   ├── core/           # Config, middleware
│   ├── models/         # Pydantic DTOs
│   ├── services/       # Domain logic (The most important folder)
│   ├── utils/          # Helpers (Haversine, security, etc.)
│   ├── main.py         # Entry point & lifespan management
│   └── logging.py      # Structured logging config
├── static/             # Assets (images, css, js) & MasterList.json
├── templates/          # Jinja2 HTML templates
└── requirements.txt    # Python dependencies
```
