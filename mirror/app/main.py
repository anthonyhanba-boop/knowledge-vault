"""Project Mirror - FastAPI application."""
from __future__ import annotations

import asyncio
import csv
import html
import io
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import hashing, pipeline
from .config import settings
from .db import ProviderHealth, Result, Search, SessionLocal, init_db, new_id, utcnow
from .providers import ALL_PROVIDERS, ProviderError
from .security import (OWNER_COOKIE, USER_AGENT, UnsafeURL, api_limiter, assert_public_url, make_owner_token,
                       read_owner_token, search_limiter, sign_upload)

STATIC = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    task = asyncio.create_task(pipeline.purge_loop())
    yield
    task.cancel()


app = FastAPI(title="Project Mirror", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def security_mw(request: Request, call_next):
    ip = request.client.host if request.client else "unknown"
    if request.url.path.startswith("/api/") and not api_limiter.allow(ip):
        return JSONResponse({"detail": "Too many requests"}, status_code=429)
    owner = read_owner_token(request.cookies.get(OWNER_COOKIE))
    new_cookie = None
    if not owner:
        owner, new_cookie = make_owner_token()
    request.state.owner = owner
    resp: Response = await call_next(request)
    if new_cookie:
        resp.set_cookie(OWNER_COOKIE, new_cookie, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 365,
                        secure=request.url.scheme == "https")
    resp.headers.update({
        "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": "default-src 'self'; img-src 'self' https: data: blob:; style-src 'self' 'unsafe-inline' "
                                   "https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
                                   "script-src 'self'; connect-src 'self'; frame-ancestors 'none'",
    })
    return resp


def owner(request: Request) -> str:
    return request.state.owner


def _own_search(db, search_id: str, who: str) -> Search:
    s = db.get(Search, search_id)
    if not s or s.owner != who:
        raise HTTPException(404, "Search not found")
    return s


def _own_result(db, result_id: str, who: str) -> Result:
    r = db.get(Result, result_id)
    if not r or r.search.owner != who:
        raise HTTPException(404, "Result not found")
    return r


def result_dict(r: Result) -> dict:
    return {
        "id": r.id, "page_url": r.page_url, "page_title": r.page_title, "image_url": r.image_url,
        "thumbnail_url": r.thumbnail_url, "domain": r.domain, "providers": r.providers,
        "match_type": r.match_type, "provider_category": r.provider_category, "verified": r.verified,
        "similarity": r.similarity, "hash_distance": r.hash_distance, "image_format": r.image_format,
        "is_news": r.is_news, "indexed_at": r.indexed_at.isoformat() if r.indexed_at else None,
        "hidden": r.hidden, "saved": r.saved, "broken_report": r.broken_report, "search_id": r.search_id,
    }


def search_dict(s: Search, with_results: bool = False) -> dict:
    d = {
        "id": s.id, "created_at": s.created_at.isoformat(), "status": s.status, "progress": s.progress,
        "stage": s.stage, "filename": s.filename, "width": s.width, "height": s.height, "format": s.fmt,
        "sha256": s.sha256, "phash": s.phash, "provider_status": s.provider_status or {}, "error": s.error,
        "has_thumb": bool(s.thumb_path), "image_retained": bool(s.upload_path),
        "result_count": len(s.results),
    }
    if with_results:
        d["results"] = [result_dict(r) for r in s.results]
    return d


# ---------------- providers / health ----------------
@app.get("/api/providers")
def providers():
    with SessionLocal() as db:
        out = []
        for p in ALL_PROVIDERS:
            h = db.get(ProviderHealth, p.name)
            out.append({
                "name": p.name, "label": p.label, "configured": p.configured(), "docs_url": p.docs_url,
                "config_hint": p.config_hint(), "supports_dates": p.supports_dates,
                "health": None if not h else {
                    "last_ok_at": h.last_ok_at.isoformat() if h.last_ok_at else None,
                    "last_error_at": h.last_error_at.isoformat() if h.last_error_at else None,
                    "last_error": h.last_error, "last_latency_ms": h.last_latency_ms,
                    "calls": h.calls, "failures": h.failures,
                },
            })
    return {"providers": out}


@app.post("/api/providers/check")
async def check_providers():
    results = {}
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}) as client:
        for p in ALL_PROVIDERS:
            if not p.configured():
                results[p.name] = {"ok": False, "error": "not configured"}
                continue
            t0 = time.monotonic()
            try:
                await p.ping(client)
                ms = int((time.monotonic() - t0) * 1000)
                pipeline.record_health(p.name, True, ms)
                results[p.name] = {"ok": True, "latency_ms": ms}
            except (ProviderError, httpx.HTTPError) as e:
                pipeline.record_health(p.name, False, int((time.monotonic() - t0) * 1000), str(e))
                results[p.name] = {"ok": False, "error": str(e)}
    return results


# ---------------- searches ----------------
@app.post("/api/searches", status_code=201)
async def create_search(request: Request, file: UploadFile = File(...), consent: bool = Form(False),
                        who: str = Depends(owner)):
    if not consent:
        raise HTTPException(400, "You must confirm you have the right to search this image.")
    ip = request.client.host if request.client else "unknown"
    if not search_limiter.allow(ip):
        raise HTTPException(429, f"Search limit reached ({settings.rate_limit_per_hour}/hour). Try again later.")
    limit = settings.max_upload_mb * 1024 * 1024
    raw = await file.read(limit + 1)
    if len(raw) > limit:
        raise HTTPException(413, f"Image exceeds {settings.max_upload_mb} MB")
    try:
        img, fmt = await asyncio.to_thread(hashing.load_image, raw)
    except hashing.InvalidImage as e:
        raise HTTPException(415, str(e))
    if img.width < 32 or img.height < 32:
        raise HTTPException(422, "Image is too small to search (min 32x32)")

    clean = await asyncio.to_thread(hashing.sanitize, img)
    fp = await asyncio.to_thread(hashing.fingerprint, img, raw)
    sid = new_id()
    up = settings.data_dir / "uploads" / f"{sid}.jpg"
    th = settings.data_dir / "thumbs" / f"{sid}.jpg"
    up.write_bytes(clean)
    up.chmod(0o600)
    th.write_bytes(await asyncio.to_thread(hashing.make_thumbnail, img))
    with SessionLocal() as db:
        s = Search(id=sid, owner=who, filename=(file.filename or "image")[:200], width=img.width, height=img.height,
                   fmt=hashing.ALLOWED_FORMATS[fmt], sha256=fp.sha256, phash=str(fp.phash), dhash=str(fp.dhash),
                   upload_path=str(up), thumb_path=str(th), provider_status={})
        db.add(s)
        db.commit()
        d = search_dict(s)
    pipeline.start_search(sid)
    return d


@app.get("/api/searches")
def list_searches(who: str = Depends(owner)):
    with SessionLocal() as db:
        rows = db.query(Search).filter(Search.owner == who).order_by(Search.created_at.desc()).limit(100).all()
        return {"searches": [search_dict(s) for s in rows]}


@app.get("/api/searches/{search_id}")
def get_search(search_id: str, who: str = Depends(owner)):
    with SessionLocal() as db:
        return search_dict(_own_search(db, search_id, who), with_results=True)


def _delete_files(s: Search) -> None:
    for p in (s.upload_path, s.thumb_path):
        if p:
            Path(p).unlink(missing_ok=True)


@app.delete("/api/searches/{search_id}", status_code=204)
def delete_search(search_id: str, who: str = Depends(owner)):
    with SessionLocal() as db:
        s = _own_search(db, search_id, who)
        _delete_files(s)
        db.delete(s)
        db.commit()


@app.delete("/api/searches", status_code=204)
def delete_all(who: str = Depends(owner)):
    with SessionLocal() as db:
        for s in db.query(Search).filter(Search.owner == who):
            _delete_files(s)
            db.delete(s)
        db.commit()


@app.post("/api/searches/{search_id}/retry")
async def retry(search_id: str, who: str = Depends(owner)):
    with SessionLocal() as db:
        s = _own_search(db, search_id, who)
        if s.status in ("queued", "running"):
            raise HTTPException(409, "Search is still running")
        if not s.upload_path or not Path(s.upload_path).exists():
            raise HTTPException(410, "The image was deleted for privacy after the retention window. Upload it again.")
        failed = [k for k, v in (s.provider_status or {}).items() if v.get("state") != "ok"]
        if s.status == "failed" and not failed:
            failed = None  # crashed or interrupted: run everything
        elif not failed:
            failed = [p.name for p in ALL_PROVIDERS if p.configured() and p.name not in (s.provider_status or {})]
            if not failed:
                raise HTTPException(409, "No failed providers to retry")
        s.status, s.stage, s.progress = "queued", "Retrying", 0.0
        db.commit()
    pipeline.start_search(search_id, only=failed)
    return {"retrying": failed or "all"}


@app.get("/api/searches/{search_id}/thumb")
def thumb(search_id: str, who: str = Depends(owner)):
    with SessionLocal() as db:
        s = _own_search(db, search_id, who)
        if not s.thumb_path or not Path(s.thumb_path).exists():
            raise HTTPException(404)
        return FileResponse(s.thumb_path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@app.get("/api/u/{search_id}")
def signed_upload(search_id: str, exp: int, sig: str):
    """Short-lived signed URL used only by URL-based providers (SerpApi Lens)."""
    import hmac
    if exp < time.time() or not hmac.compare_digest(sig, sign_upload(search_id, exp)):
        raise HTTPException(403)
    with SessionLocal() as db:
        s = db.get(Search, search_id)
        if not s or not s.upload_path or not Path(s.upload_path).exists():
            raise HTTPException(404)
        return FileResponse(s.upload_path, media_type="image/jpeg",
                            headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, noimageindex"})


# ---------------- results ----------------
class ResultPatch(BaseModel):
    hidden: bool | None = None
    saved: bool | None = None


@app.patch("/api/results/{result_id}")
def patch_result(result_id: str, body: ResultPatch, who: str = Depends(owner)):
    with SessionLocal() as db:
        r = _own_result(db, result_id, who)
        if body.hidden is not None:
            r.hidden = body.hidden
        if body.saved is not None:
            r.saved = body.saved
        db.commit()
        return result_dict(r)


@app.post("/api/results/{result_id}/report")
async def report_broken(result_id: str, who: str = Depends(owner)):
    """Record a broken-link report and actually re-check the URLs."""
    with SessionLocal() as db:
        r = _own_result(db, result_id, who)
        urls = {"page": r.page_url, "image": r.image_url}
    checks = {}
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}) as client:
        for k, u in urls.items():
            if not u:
                continue
            try:
                await assert_public_url(u)
                resp = await client.head(u, timeout=8, follow_redirects=True)
                if resp.status_code in (405, 501):
                    resp = await client.get(u, timeout=8, follow_redirects=True)
                checks[k] = resp.status_code
            except (UnsafeURL, httpx.HTTPError) as e:
                checks[k] = f"unreachable: {type(e).__name__}"
    report = {"reported_at": utcnow().isoformat(), "checks": checks,
              "confirmed_broken": any(not isinstance(v, int) or v >= 400 for v in checks.values())}
    with SessionLocal() as db:
        r = _own_result(db, result_id, who)
        r.broken_report = report
        db.commit()
        return result_dict(r)


@app.get("/api/saved")
def saved(who: str = Depends(owner)):
    with SessionLocal() as db:
        rows = (db.query(Result).join(Search).filter(Search.owner == who, Result.saved.is_(True))
                .order_by(Search.created_at.desc()).all())
        return {"results": [result_dict(r) for r in rows]}


# ---------------- export ----------------
EXPORT_FIELDS = ["match_type", "verified", "similarity", "domain", "page_title", "page_url", "image_url",
                 "providers", "image_format", "is_news", "indexed_at", "saved", "hidden"]


@app.get("/api/searches/{search_id}/export")
def export(search_id: str, format: str = "json", include_hidden: bool = False, who: str = Depends(owner)):
    with SessionLocal() as db:
        s = _own_search(db, search_id, who)
        d = search_dict(s, with_results=True)
    d["results"] = [r for r in d["results"] if include_hidden or not r["hidden"]]
    d["exported_at"] = utcnow().isoformat()
    name = f"mirror-report-{search_id[:8]}"
    if format == "json":
        return Response(json.dumps(d, indent=2), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{name}.json"'})
    if format == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(EXPORT_FIELDS)
        for r in d["results"]:
            w.writerow([";".join(r[f]) if isinstance(r[f], list) else ("" if r[f] is None else r[f]) for f in EXPORT_FIELDS])
        return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})
    if format == "html":
        e = html.escape
        rows = "".join(
            f"<tr><td>{e(r['match_type'])}{' ✓' if r['verified'] else ''}</td><td>{e(r['domain'])}</td>"
            f"<td>{e(r['page_title'] or '')}</td><td><a href='{e(r['page_url'] or '')}'>{e(r['page_url'] or '')}</a></td>"
            f"<td><a href='{e(r['image_url'] or '')}'>image</a></td><td>{e(', '.join(r['providers']))}</td>"
            f"<td>{e((r['indexed_at'] or '')[:10])}</td></tr>" for r in d["results"])
        prov = "".join(f"<li>{e(v.get('label', k))}: {e(v.get('state', ''))} ({v.get('count', 0)} raw) "
                       f"{e(v.get('error') or '')}</li>" for k, v in d["provider_status"].items())
        doc = f"""<!doctype html><meta charset=utf-8><title>Mirror report {e(search_id[:8])}</title>
<style>body{{font:14px system-ui;margin:32px;color:#111}}table{{border-collapse:collapse;width:100%}}
td,th{{border:1px solid #ddd;padding:6px;text-align:left;vertical-align:top;word-break:break-all}}th{{background:#f4f4f6}}</style>
<h1>Project Mirror — search report</h1><p>File: {e(d['filename'])} · {d['width']}×{d['height']} · SHA-256 {e(d['sha256'])}<br>
Searched {e(d['created_at'])} · exported {e(d['exported_at'])}</p><h3>Providers</h3><ul>{prov}</ul>
<h3>{len(d['results'])} results</h3><table><tr><th>Match</th><th>Domain</th><th>Title</th><th>Page</th><th>Image</th>
<th>Providers</th><th>Indexed</th></tr>{rows}</table>"""
        return Response(doc, media_type="text/html",
                        headers={"Content-Disposition": f'attachment; filename="{name}.html"'})
    raise HTTPException(400, "format must be json, csv or html")


@app.get("/api/health")
def health():
    return {"ok": True, "configured_providers": [p.name for p in ALL_PROVIDERS if p.configured()]}


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
