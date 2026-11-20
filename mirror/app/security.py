"""Security helpers: anonymous signed owner sessions, rate limiting, SSRF-safe fetching, robots.txt."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import secrets
import socket
import time
from collections import defaultdict, deque
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from .config import settings

USER_AGENT = "ProjectMirrorBot/1.0 (+reverse-image-verification)"
OWNER_COOKIE = "mirror_owner"


# ---------- signed anonymous owner identity (scopes history per browser) ----------
def _sign(value: str) -> str:
    return hmac.new(settings.secret_key.encode(), value.encode(), hashlib.sha256).hexdigest()[:32]


def make_owner_token() -> tuple[str, str]:
    owner = secrets.token_hex(16)
    return owner, f"{owner}.{_sign(owner)}"


def read_owner_token(token: str | None) -> str | None:
    if not token or "." not in token:
        return None
    owner, sig = token.split(".", 1)
    return owner if hmac.compare_digest(sig, _sign(owner)) else None


def sign_upload(search_id: str, expires: int) -> str:
    return _sign(f"upload:{search_id}:{expires}")


# ---------- rate limiting (sliding window, per key) ----------
class RateLimiter:
    def __init__(self, limit: int, window_s: int):
        self.limit, self.window = limit, window_s
        self.hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True


search_limiter = RateLimiter(settings.rate_limit_per_hour, 3600)
api_limiter = RateLimiter(600, 60)


# ---------- SSRF protection ----------
class UnsafeURL(Exception):
    pass


def _is_public_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return addr.is_global and not addr.is_multicast


async def assert_public_url(url: str) -> None:
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise UnsafeURL("only http(s) URLs are allowed")
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80),
                                       type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise UnsafeURL(f"DNS failure: {e}") from e
    for info in infos:
        if not _is_public_ip(info[4][0]):
            raise UnsafeURL("URL resolves to a non-public address")


# ---------- robots.txt ----------
_robots_cache: dict[str, tuple[float, RobotFileParser | None]] = {}


async def robots_allows(client: httpx.AsyncClient, url: str) -> bool:
    if not settings.respect_robots:
        return True
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    cached = _robots_cache.get(base)
    if not cached or time.time() - cached[0] > 3600:
        rp: RobotFileParser | None = RobotFileParser()
        try:
            await assert_public_url(base + "/robots.txt")
            r = await client.get(base + "/robots.txt", timeout=5, follow_redirects=True)
            if r.status_code >= 400:
                rp = None  # no robots.txt => allowed
            else:
                rp.parse(r.text.splitlines())
        except Exception:
            rp = None
        _robots_cache[base] = cached = (time.time(), rp)
    rp = cached[1]
    return True if rp is None else rp.can_fetch(USER_AGENT, url)


async def safe_get(client: httpx.AsyncClient, url: str, max_bytes: int, timeout: float = 8.0) -> httpx.Response:
    """GET a public URL, re-validating every redirect hop and capping the body size."""
    for _ in range(4):
        await assert_public_url(url)
        async with client.stream("GET", url, timeout=timeout, follow_redirects=False) as r:
            if r.is_redirect and "location" in r.headers:
                url = str(r.url.join(r.headers["location"]))
                continue
            body = bytearray()
            async for chunk in r.aiter_bytes():
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise UnsafeURL("response too large")
            return httpx.Response(r.status_code, headers=r.headers, content=bytes(body), request=r.request)
    raise UnsafeURL("too many redirects")
