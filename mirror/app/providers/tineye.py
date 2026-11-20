"""TinEye commercial API (accepts image upload, returns backlinks with crawl dates)."""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from ..config import settings
from .base import Candidate, Provider, ProviderError, SearchInput, raise_for

BASE = "https://api.tineye.com/rest"


class TinEyeProvider(Provider):
    name = "tineye"
    label = "TinEye"
    docs_url = "https://services.tineye.com/developers/tineyeapi/overview"
    key_env = "TINEYE_API_KEY"
    supports_dates = True

    def configured(self) -> bool:
        return bool(settings.tineye_api_key)

    def _headers(self) -> dict:
        return {"x-api-key": settings.tineye_api_key}

    async def search(self, client: httpx.AsyncClient, inp: SearchInput) -> list[Candidate]:
        r = await client.post(f"{BASE}/search/", headers=self._headers(),
                              files={"image_upload": ("query.jpg", inp.image_bytes, "image/jpeg")},
                              data={"limit": "100", "backlink_limit": "20", "sort": "score", "order": "desc"},
                              timeout=45)
        raise_for(r, self.label)
        data = r.json()
        if data.get("code", 200) != 200:
            raise ProviderError(f"{self.label}: {'; '.join(data.get('messages', [])) or 'error'}")
        out: list[Candidate] = []
        for m in data.get("results", {}).get("matches", []):
            cat = "exact" if (m.get("query_match_percent") or 0) >= 95 else "partial"
            backlinks = m.get("backlinks") or [{}]
            for b in backlinks:
                out.append(Candidate(
                    self.name, cat,
                    page_url=b.get("backlink"), image_url=b.get("url") or m.get("image_url"),
                    thumbnail_url=m.get("image_url"), indexed_at=_date(b.get("crawl_date")),
                    extra={"score": m.get("score"), "format": m.get("format")},
                ))
        return out

    async def ping(self, client: httpx.AsyncClient) -> None:
        r = await client.get(f"{BASE}/remaining_searches/", headers=self._headers(), timeout=10)
        raise_for(r, self.label)


def _date(s: str | None) -> datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[:19], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None
