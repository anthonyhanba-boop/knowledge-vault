# Project Mirror: visual reverse-image search

Upload an image and find public webpages that contain the same image, a resized or recompressed copy, a crop, or a visually similar image.
Results come **only** from real reverse-image-search providers. Mirror then **re-checks each candidate itself**: it downloads the image and compares perceptual hashes, so the match labels come from Mirror's own comparison, not just the provider's claim.

> Mirror does **not** do facial recognition, biometric identification or person tracking. It compares whole-image visual fingerprints (pHash / dHash / wHash, colour histograms and a crop search) and nothing else.

## Quick start
```bash
pip install -r requirements.txt
cp .env.example .env        # add at least one provider key
uvicorn mirror.app.main:app --reload
# open http://localhost:8000
```
With Docker and PostgreSQL: `docker compose up --build`.

Tests: `python -m pytest mirror/tests` (hashing accuracy, dedupe, the full API flow, retry, privacy isolation, signed URLs and SSRF).

## Search providers (research summary, Nov 2026)

| Provider | Upload support | What it returns | Status in Mirror |
|---|---|---|---|
| **Google Cloud Vision: Web Detection** | yes (base64) | full, partial and similar images, pages containing the image | ✅ primary provider |
| **TinEye API** (commercial) | yes (multipart) | exact and modified copies, backlinks, **crawl dates** | ✅ only provider with date filtering |
| **SerpApi: Google Lens engine** | URL only | exact matches and visual matches with page titles | ✅ needs `PUBLIC_BASE_URL` |
| Bing Visual Search | n/a | n/a | ❌ Microsoft retired the Bing Search APIs in Aug 2025 |
| Yandex / Google Images / PimEyes | no official API | n/a | ❌ not integrated: scraping would break their terms |

### Getting keys
* **Google Cloud Vision.** At console.cloud.google.com, create a project, enable **Cloud Vision API**, then go to *APIs & Services → Credentials → Create API key*. Restrict the key to the Vision API. The first 1,000 units per month are free. Set `GOOGLE_VISION_API_KEY`.
* **TinEye.** At services.tineye.com/TinEyeAPI, buy a search bundle and copy the API key. Set `TINEYE_API_KEY`.
* **SerpApi.** At serpapi.com, sign up and copy the key from *Manage API key* (there is a free tier). Set `SERPAPI_API_KEY` **and** `PUBLIC_BASE_URL`, which must be the internet-reachable URL of this server. SerpApi can only fetch images by URL, so Mirror gives it an HMAC-signed link to the sanitized upload. The link expires after 10 minutes and carries `X-Robots-Tag: noindex`.

The **Status** page shows which providers are configured, their call and failure counts and latency, and a live health check (`POST /api/providers/check`).

## Architecture
```
Browser (static SPA, no build)  ──►  FastAPI
                                   ├─ upload: content-sniffed via Pillow, size cap, EXIF stripped (re-encoded)
                                   ├─ background job (asyncio task)
                                   │    1. fingerprint (sha256, pHash, dHash, wHash, histogram)
                                   │    2. providers in parallel, retry + exponential backoff
                                   │    3. merge: URL normalisation (www/utm/trailing slash), one row per page
                                   │    4. verify: SSRF-safe fetch (+robots.txt) → hash compare + crop search
                                   │    5. classify: exact / near-duplicate / cropped / similar
                                   ├─ SQLAlchemy: SQLite (dev) or PostgreSQL (DATABASE_URL)
                                   └─ purge loop: deletes retained originals after UPLOAD_RETENTION_MINUTES
```
* **Match types.** *exact*: average hash distance ≤3 and the same aspect ratio. *near_duplicate*: distance ≤10 (resized, recompressed, small edits). *cropped*: a coarse-to-fine sliding-window search across scales, in both directions, finds a window with distance ≤10. *similar*: everything else that a provider reported. Results tagged **✓ re-checked** were downloaded and compared by Mirror. Results tagged **provider-reported** could not be downloaded, so they keep the provider's own category.
* **News.** Detected heuristically from a list of known news domains plus URL patterns (`/news/`, `/2024/05/`). No provider supplies a news category.
* **Dates.** Only TinEye returns crawl dates. When a date filter is set, results without a date are excluded.
* **Failure recovery.** Each provider gets up to 3 tries (no retry on authentication or bad-request errors). A partial failure still completes the search. If any provider failed, the sanitized original is kept for `UPLOAD_RETENTION_MINUTES`, so **Retry failed providers** reruns only those providers and merges the new results. Jobs interrupted by a server restart are marked as retryable.
* **Scaling.** The job runner is in-process. To scale out horizontally, move `pipeline.run_search` into a queue worker (arq, RQ or Celery) and replace the in-memory rate limiter with Redis. The API layer is stateless apart from those two pieces.

## Privacy and security
* Uploads require an explicit consent checkbox; the server enforces it too.
* Uploads are re-encoded, which strips EXIF and GPS metadata, and stored with mode 0600 under random names. They are never served publicly, except through the expiring signed URL for SerpApi.
* The original is **deleted as soon as the search finishes**, unless a provider failed, in which case it is kept for up to 30 minutes for retry. Only a 320px thumbnail stays for history, and deleting the search deletes it.
* History is tied to an anonymous, signed, HttpOnly cookie, so other browsers can't read it. Users can delete one search or all of them.
* Candidate downloads are SSRF-guarded: public IPs only, every redirect re-checked, 8 MB cap, decompression-bomb guard. They also respect robots.txt (`RESPECT_ROBOTS_TXT`).
* Other safeguards: per-IP rate limits, CSP and security headers, and API keys that stay server-side only.
* For a multi-user deployment, put an identity provider in front (OAuth / OIDC) and map users to the `owner` column.
