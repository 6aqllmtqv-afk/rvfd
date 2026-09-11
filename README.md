# BestChange Data Collector

This project pulls BestChange API v2.0 reference data into SQLite and can fetch current rates for a supplied list of currency pairs.

## Security

Do **not** commit your API key. Put it in `.env` or a server environment variable. Because the key was shared in chat, rotate/reissue it before production use.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env and set BESTCHANGE_API_KEY
```

## Sync reference data

```bash
set -a; source .env; set +a
python collector.py --reference-only
```

Creates `bestchange.db` with raw JSON plus normalized tables for:
- langs
- groups
- countries
- cities
- currencies
- changers

## Sync rates for known pairs

Create `pairs.txt` with BestChange IDs:

```text
305-89
305-89-1
31-12
30-53
```

Then:

```bash
set -a; source .env; set +a
python collector.py --pairs pairs.txt
```

The API supports batch requests of up to 500 pairs. The collector keeps the full raw response so the exact API payload is preserved even if the schema evolves.

## Important architecture note

BestChange API v2.0 documents reference endpoints plus `presences` and `rates` for a specified direction; it does not document a single `GET all-directions` endpoint. Therefore this first collector does **not** guess every possible currency combination. The pair list should be generated/maintained from the directions your application actually needs, then fetched in batches.

## Mirrors and request behavior

The collector tries the main API and then mirror1..mirror4. It uses a persistent HTTP session, gzip-capable requests, and waits about one second between rate batches to respect the API's documented caching/rate guidance.

## Web monitor

Run:

    pip install -r requirements-web.txt
    python app.py

Open http://127.0.0.1:8080

The browser never receives the BestChange API key. It calls the local `/api/*` endpoints; the server talks to BestChange API and caches identical rate requests for `BESTCHANGE_CACHE_TTL` seconds.
