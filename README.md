# DillDrill — URL-First Radar MVP

**DillDrill** is a privacy-focused tool for detecting construction noise and real-estate points of interest (POIs). It allows users to check specific coordinates for nearby projects using a tiered access system.

## Features (v1.1)
- **Direct Coordinate Input**: No address geocoding; fast and privacy-preserving.
- **Privacy First**: Anonymous IDs via fingerprint stored in a cookie; no account required.
- **Smart Filtering**: "Greedy 30m" algorithm ensures result diversity.
- **Tiered Access**:
  - **Free Tier**: 2 searches/day, 1 result per search.
  - **Paid Tier**: 50 searches/day, 5 results per search.
- **Cloudflare Turnstile**: Human verification required for Free tier; validated server-side.
- **Language Persistence**: Stores `dd_lang` and uses it in anonymous fingerprinting; defaults to last choice.
- **KMZ Export**: Download results for Google Earth.
- **Internationalization**: English, Spanish, Russian, Korean.
- **Dev Mode Visibility**: Landing page indicates when Redis fallback (in-memory quota) is active.

## Architecture
The project follows a Domain-First architecture using FastAPI, Redis (Upstash), and Jinja2.

## Developer Guide
For a detailed introduction to the codebase, modules, and architecture, please read the **[Developer Introduction & Architecture Guide](DEVELOPER_GUIDE.md)**.

## Getting Started

1.  **Clone the repo**.
2.  **Install dependencies**:
    ```bash
    pip install -r requirements.txt
    ```
3.  **Configure Environment**:
    Create a `.env` file with:
    ```env
    UPSTASH_REDIS_REST_URL="https://..."
    UPSTASH_REDIS_REST_TOKEN="..."
    CLOUDFLARE_TURNSTILE_SECRET="..."
    CLOUDFLARE_TURNSTILE_SITE_KEY="..."
    ENV="development" # or "production"
    # Optional: enable admin bypass with a static token
    ADMIN_BYPASS_TOKEN="..."
    ```
4.  **Run**:
    ```bash
    uvicorn app.main:app --reload
    ```

### Known Issues
- KMZ download currently increments a non-day-scoped quota key; aligning to daily scoping is planned.
- Dev Mode displays Redis fallback status on the landing page.
