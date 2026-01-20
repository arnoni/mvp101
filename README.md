# DillDrill — URL-First Radar MVP

**DillDrill** is a privacy-focused tool for detecting construction noise and real-estate points of interest (POIs). It allows users to check specific coordinates for nearby projects using a tiered access system.

## Features (v1.1)
- **Direct Coordinate Input**: No address geocoding; fast and privacy-preserving.
- **Privacy First**: Uses ephemeral "Anonymous IDs" for quotas; no account required.
- **Smart Filtering**: "Greedy 30m" algorithm ensures result diversity.
- **Tiered Access**:
  - **Free Tier**: 2 searches/day, 1 result per search.
  - **Paid Tier**: 50 searches/day, 5 results per search.
- **KMZ Export**: Download results for Google Earth.
- **Internationalization**: English, Spanish, Russian, Korean.

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
    UPSTASH_REDIS_URL="redis://..."
    CLOUDFLARE_TURNSTILE_SECRET="..."
    CLOUDFLARE_TURNSTILE_SITE_KEY="..."
    ```
4.  **Run**:
    ```bash
    uvicorn app.main:app --reload
    ```
