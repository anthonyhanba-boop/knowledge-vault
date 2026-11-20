"""SerpApi Google Lens engine. Lens via SerpApi only accepts an image URL, so this provider
needs PUBLIC_BASE_URL: Mirror then exposes the sanitized upload at an HMAC-signed URL that
expires after a few minutes."""
from __future__ import annotations

import httpx

from ..config import settings
from .base import Candidate, Provider, ProviderError, SearchInput, raise_for

ENDPOINT = "https://serpapi.com/search.json"


class SerpApiLensProvider(Provider):
    name = "serpapi_lens"
    label = "Google Lens (SerpApi)"
    docs_url = "https://serpapi.com/google-lens-api"
    key_env = "SERPAPI_API_KEY"

    def configured(self) -> bool:
        return bool(settings.serpapi_api_key and settings.public_base_url)

    def config_hint(self) -> str:
        return "Set SERPAPI_API_KEY and PUBLIC_BASE_URL (a URL where this server is reachable from the internet)"

    async def _query(self, client: httpx.AsyncClient, url: str, type_: str) -> dict:
        r = await client.get(ENDPOINT, params={"engine": "google_lens", "url": url, "type": type_,
                                               "api_key": settings.serpapi_api_key}, timeout=60)
        raise_for(r, self.label)
        data = r.json()
        if data.get("error") and "hasn't returned any results" not in data["error"]:
            raise ProviderError(f"{self.label}: {data['error']}")
        return data

    async def search(self, client: httpx.AsyncClient, inp: SearchInput) -> list[Candidate]:
        if not inp.public_url:
            raise ProviderError(f"{self.label}: no public image URL available", retryable=False)
        out: list[Candidate] = []
        exact = await self._query(client, inp.public_url, "exact_matches")
        for m in exact.get("exact_matches", []):
            out.append(Candidate(self.name, "exact", page_url=m.get("link"), page_title=m.get("title"),
                                 image_url=m.get("image"), thumbnail_url=m.get("thumbnail")))
        visual = await self._query(client, inp.public_url, "visual_matches")
        for m in visual.get("visual_matches", []):
            out.append(Candidate(self.name, "similar", page_url=m.get("link"), page_title=m.get("title"),
                                 image_url=m.get("image"), thumbnail_url=m.get("thumbnail")))
        return out

    async def ping(self, client: httpx.AsyncClient) -> None:
        r = await client.get("https://serpapi.com/account.json", params={"api_key": settings.serpapi_api_key}, timeout=10)
        raise_for(r, self.label)
