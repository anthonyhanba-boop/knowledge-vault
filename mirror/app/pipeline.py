"""Background search pipeline: providers -> merge/dedupe -> visual verification -> persist."""
from __future__ import annotations

import asyncio
import io
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import httpx
from PIL import Image

from . import hashing
from .config import settings
from .db import ProviderHealth, Result, Search, SessionLocal, utcnow
from .providers import ALL_PROVIDERS, Candidate, Provider, ProviderError
from .providers.base import SearchInput
from .security import USER_AGENT, UnsafeURL, robots_allows, safe_get, sign_upload

log = logging.getLogger("mirror.pipeline")

MATCH_RANK = {"exact": 0, "near_duplicate": 1, "cropped": 2, "similar": 3}
PROVIDER_CATEGORY_MAP = {"exact": "exact", "partial": "cropped", "page": "exact", "similar": "similar"}
NEWS_DOMAINS = {
    "nytimes.com", "washingtonpost.com", "theguardian.com", "bbc.co.uk", "bbc.com", "cnn.com", "reuters.com",
    "apnews.com", "bloomberg.com", "wsj.com", "ft.com", "forbes.com", "npr.org", "nbcnews.com", "cbsnews.com",
    "abcnews.go.com", "foxnews.com", "usatoday.com", "latimes.com", "independent.co.uk", "telegraph.co.uk",
    "dailymail.co.uk", "aljazeera.com", "politico.com", "axios.com", "theverge.com", "techcrunch.com",
    "wired.com", "arstechnica.com", "huffpost.com", "time.com", "newsweek.com", "economist.com", "lemonde.fr",
    "spiegel.de", "elpais.com", "cbc.ca", "abc.net.au", "smh.com.au", "thehindu.com", "nypost.com", "vice.com",
    "businessinsider.com", "cnbc.com", "news.yahoo.com", "news.google.com", "yahoo.com", "msn.com",
}
NEWS_PATH = re.compile(r"/(news|article|articles|story|stories)/|/20\d\d/\d{1,2}/", re.I)
FORMAT_EXT = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp", "gif": "gif", "bmp": "bmp", "svg": "svg", "avif": "avif"}

_running: dict[str, asyncio.Task] = {}


# ---------------- helpers ----------------
def normalize_url(u: str | None) -> str:
    if not u:
        return ""
    p = urlparse(u.strip())
    netloc = p.netloc.lower().removeprefix("www.")
    path = p.path.rstrip("/") or "/"
    query = "&".join(q for q in p.query.split("&") if q and not q.lower().startswith(("utm_", "fbclid", "gclid")))
    return urlunparse((p.scheme.lower(), netloc, path, "", query, ""))


def domain_of(u: str | None) -> str:
    return urlparse(u).netloc.lower().removeprefix("www.") if u else ""


def is_news(page_url: str | None) -> bool:
    d = domain_of(page_url)
    if not d:
        return False
    if any(d == n or d.endswith("." + n) for n in NEWS_DOMAINS) or d.startswith("news."):
        return True
    return bool(NEWS_PATH.search(urlparse(page_url).path))


def format_from_url(u: str | None) -> str | None:
    if not u:
        return None
    m = re.search(r"\.([a-z0-9]{3,4})(?:$|[?#])", urlparse(u).path.lower() + "")
    return FORMAT_EXT.get(m.group(1)) if m else None


def _set(search_id: str, **fields) -> None:
    with SessionLocal() as db:
        s = db.get(Search, search_id)
        if s:
            for k, v in fields.items():
                setattr(s, k, v)
            db.commit()


def record_health(name: str, ok: bool, latency_ms: int, error: str | None = None) -> None:
    with SessionLocal() as db:
        h = db.get(ProviderHealth, name) or ProviderHealth(name=name, calls=0, failures=0)
        h.calls = (h.calls or 0) + 1
        h.last_latency_ms = latency_ms
        if ok:
            h.last_ok_at = utcnow()
        else:
            h.failures = (h.failures or 0) + 1
            h.last_error_at, h.last_error = utcnow(), (error or "")[:500]
        db.merge(h)
        db.commit()


async def run_provider(client: httpx.AsyncClient, p: Provider, inp: SearchInput, attempts: int = 3) -> tuple[list[Candidate], str | None]:
    """Run one provider with retry + exponential backoff. Returns (candidates, error)."""
    err = None
    for attempt in range(attempts):
        t0 = time.monotonic()
        try:
            cands = await p.search(client, inp)
            record_health(p.name, True, int((time.monotonic() - t0) * 1000))
            return cands, None
        except ProviderError as e:
            err = str(e)
            record_health(p.name, False, int((time.monotonic() - t0) * 1000), err)
            if not e.retryable:
                break
        except (httpx.HTTPError, ValueError) as e:
            err = f"{p.label}: {type(e).__name__}: {e}"
            record_health(p.name, False, int((time.monotonic() - t0) * 1000), err)
        await asyncio.sleep(1.5 * 2 ** attempt)
    return [], err


# ---------------- verification ----------------
async def verify_candidates(client: httpx.AsyncClient, cands: list[Candidate], qfp: hashing.Fingerprint,
                            qimg: Image.Image, on_progress) -> dict[str, tuple[hashing.Comparison, str | None]]:
    urls: list[str] = []
    for c in cands:
        u = c.image_url or c.thumbnail_url
        if u and u not in urls:
            urls.append(u)
    urls = urls[: settings.verify_max_candidates]
    out: dict[str, tuple[hashing.Comparison, str | None]] = {}
    sem = asyncio.Semaphore(8)
    done = 0

    async def one(u: str) -> None:
        nonlocal done
        async with sem:
            try:
                if await robots_allows(client, u):
                    r = await safe_get(client, u, max_bytes=8 * 1024 * 1024)
                    if r.status_code == 200:
                        img = Image.open(io.BytesIO(r.content))
                        fmt = (img.format or "").lower() or None
                        img = await asyncio.to_thread(lambda: hashing.load_image(r.content)[0])
                        cmp = await asyncio.to_thread(hashing.compare, qfp, qimg, img)
                        out[u] = (cmp, "jpeg" if fmt == "jpeg" else fmt)
            except (UnsafeURL, httpx.HTTPError, hashing.InvalidImage, OSError, Image.DecompressionBombError):
                pass
            finally:
                done += 1
                on_progress(done, len(urls))

    await asyncio.gather(*(one(u) for u in urls))
    return out


# ---------------- main job ----------------
def build_results(search_id: str, cands: list[Candidate], verified: dict) -> list[Result]:
    merged: dict[str, dict] = {}
    for c in cands:
        if not (c.page_url or c.image_url):
            continue
        key = normalize_url(c.page_url) + "|" + normalize_url(c.image_url)
        m = merged.setdefault(key, {"c": c, "providers": set(), "cats": set()})
        m["providers"].add(c.provider)
        m["cats"].add(c.category)
        base = m["c"]
        base.page_title = base.page_title or c.page_title
        base.thumbnail_url = base.thumbnail_url or c.thumbnail_url
        base.indexed_at = base.indexed_at or c.indexed_at

    # image-only entries that are already represented on a page are redundant
    imgs_on_pages = {normalize_url(m["c"].image_url) for m in merged.values() if m["c"].page_url and m["c"].image_url}
    rows: list[Result] = []
    for m in merged.values():
        c: Candidate = m["c"]
        if not c.page_url and normalize_url(c.image_url) in imgs_on_pages:
            continue
        best_cat = min((PROVIDER_CATEGORY_MAP[x] for x in m["cats"]), key=MATCH_RANK.get)
        v = verified.get(c.image_url or "") or verified.get(c.thumbnail_url or "")
        r = Result(search_id=search_id, page_url=c.page_url, page_title=c.page_title, image_url=c.image_url,
                   thumbnail_url=c.thumbnail_url or c.image_url,
                   domain=domain_of(c.page_url) or domain_of(c.image_url), providers=sorted(m["providers"]),
                   provider_category=best_cat, is_news=is_news(c.page_url), indexed_at=c.indexed_at,
                   image_format=(c.extra.get("format") or "").lower() or format_from_url(c.image_url))
        if v:
            cmp, fmt = v
            r.verified, r.hash_distance, r.similarity = True, cmp.distance, cmp.similarity
            r.image_format = fmt or r.image_format
            r.match_type = "similar" if cmp.match_type == "different" else cmp.match_type
        else:
            r.match_type = best_cat
        rows.append(r)

    # one row per page: keep the strongest match
    by_page: dict[str, Result] = {}
    loose: list[Result] = []
    for r in rows:
        if not r.page_url:
            loose.append(r)
            continue
        k = normalize_url(r.page_url)
        cur = by_page.get(k)
        if cur is None or (MATCH_RANK[r.match_type], not r.verified) < (MATCH_RANK[cur.match_type], not cur.verified):
            if cur is not None:
                r.providers = sorted(set(r.providers) | set(cur.providers))
                r.page_title = r.page_title or cur.page_title
            by_page[k] = r
        else:
            cur.providers = sorted(set(r.providers) | set(cur.providers))
    final = list(by_page.values()) + loose
    final.sort(key=lambda r: (MATCH_RANK[r.match_type], not r.verified, -(r.similarity or 0), r.page_url is None))
    return final


async def run_search(search_id: str, only: list[str] | None = None) -> None:
    with SessionLocal() as db:
        s = db.get(Search, search_id)
        if not s or not s.upload_path or not Path(s.upload_path).exists():
            if s:
                s.status, s.error, s.stage = "failed", "Uploaded image is no longer available", "Failed"
                db.commit()
            return
        raw = Path(s.upload_path).read_bytes()
        prev_status = dict(s.provider_status or {})

    providers = [p for p in ALL_PROVIDERS if p.configured() and (only is None or p.name in only)]
    _set(search_id, status="running", stage="Fingerprinting image", progress=0.05, error=None)
    qimg, _ = hashing.load_image(raw)
    qfp = hashing.fingerprint(qimg, raw)

    public_url = None
    if settings.public_base_url:
        exp = int(time.time()) + 600
        public_url = f"{settings.public_base_url}/api/u/{search_id}?exp={exp}&sig={sign_upload(search_id, exp)}"
    inp = SearchInput(image_bytes=raw, public_url=public_url)

    status = {k: v for k, v in prev_status.items() if only is not None and k not in only}
    for p in providers:
        status[p.name] = {"state": "running", "label": p.label}
    _set(search_id, provider_status=status, stage=f"Querying {len(providers)} provider(s)", progress=0.1)

    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}) as client:
        all_cands: list[Candidate] = []
        done = 0

        async def go(p: Provider) -> None:
            nonlocal done
            cands, err = await run_provider(client, p, inp)
            all_cands.extend(cands)
            status[p.name] = {"state": "failed" if err else "ok", "label": p.label, "count": len(cands), "error": err}
            done += 1
            _set(search_id, provider_status=dict(status), progress=0.1 + 0.4 * done / max(1, len(providers)))

        await asyncio.gather(*(go(p) for p in providers))

        if only is not None:  # retry run: keep previous results and add new ones
            with SessionLocal() as db:
                for r in db.query(Result).filter(Result.search_id == search_id):
                    cat = {"cropped": "partial", "near_duplicate": "exact"}.get(r.provider_category, r.provider_category)
                    for prov in (r.providers or ["previous"]):
                        all_cands.append(Candidate(prov, cat, r.page_url, r.page_title, r.image_url,
                                                   r.thumbnail_url, r.indexed_at))

        _set(search_id, stage=f"Verifying {min(len(all_cands), settings.verify_max_candidates)} candidate images", progress=0.5)

        def prog(d: int, total: int) -> None:
            _set(search_id, progress=0.5 + 0.45 * d / max(1, total), stage=f"Verifying images ({d}/{total})")

        verified = await verify_candidates(client, all_cands, qfp, qimg, prog)

    rows = build_results(search_id, all_cands, verified)
    any_retryable_fail = any(v.get("state") == "failed" for v in status.values())
    with SessionLocal() as db:
        s = db.get(Search, search_id)
        db.query(Result).filter(Result.search_id == search_id).delete()
        db.add_all(rows)
        s.provider_status = status
        s.finished_at = utcnow()
        s.progress, s.stage = 1.0, "Complete"
        if not providers:
            s.status, s.error = "failed", "No search providers are configured. See /api/providers."
        elif all(v.get("state") == "failed" for v in status.values()):
            s.status, s.error = "failed", "All providers failed. You can retry from the results page."
        else:
            s.status = "done"
        # privacy: delete the original immediately unless a retry may still need it
        if not any_retryable_fail and s.upload_path:
            Path(s.upload_path).unlink(missing_ok=True)
            s.upload_path = None
        db.commit()


def start_search(search_id: str, only: list[str] | None = None) -> None:
    async def wrapped():
        try:
            await run_search(search_id, only)
        except Exception as e:  # noqa: BLE001
            log.exception("search %s crashed", search_id)
            _set(search_id, status="failed", stage="Failed", error=f"Internal error: {type(e).__name__}")
        finally:
            _running.pop(search_id, None)

    _running[search_id] = asyncio.create_task(wrapped())


async def purge_loop() -> None:
    """Delete retained uploads after UPLOAD_RETENTION_MINUTES and recover jobs orphaned by a restart."""
    with SessionLocal() as db:
        for s in db.query(Search).filter(Search.status.in_(["queued", "running"])):
            s.status, s.stage, s.error = "failed", "Interrupted", "Server restarted during search; retry available."
        db.commit()
    while True:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=settings.upload_retention_minutes)
        with SessionLocal() as db:
            for s in db.query(Search).filter(Search.upload_path.isnot(None)):
                created = s.created_at if s.created_at.tzinfo else s.created_at.replace(tzinfo=timezone.utc)
                if created < cutoff and s.id not in _running:
                    Path(s.upload_path).unlink(missing_ok=True)
                    s.upload_path = None
            db.commit()
        await asyncio.sleep(60)
