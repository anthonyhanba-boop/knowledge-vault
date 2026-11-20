"""Google Cloud Vision API - WEB_DETECTION feature (accepts raw image bytes)."""
from __future__ import annotations

import base64

import httpx

from ..config import settings
from .base import Candidate, Provider, ProviderError, SearchInput, raise_for

ENDPOINT = "https://vision.googleapis.com/v1/images:annotate"


class GoogleVisionProvider(Provider):
    name = "google_vision"
    label = "Google Cloud Vision"
    docs_url = "https://cloud.google.com/vision/docs/detecting-web"
    key_env = "GOOGLE_VISION_API_KEY"

    def configured(self) -> bool:
        return bool(settings.google_vision_api_key)

    async def _annotate(self, client: httpx.AsyncClient, content: bytes, max_results: int) -> dict:
        body = {"requests": [{
            "image": {"content": base64.b64encode(content).decode()},
            "features": [{"type": "WEB_DETECTION", "maxResults": max_results}],
            "imageContext": {"webDetectionParams": {"includeGeoResults": False}},
        }]}
        r = await client.post(ENDPOINT, params={"key": settings.google_vision_api_key}, json=body, timeout=30)
        raise_for(r, self.label)
        resp = (r.json().get("responses") or [{}])[0]
        if "error" in resp:
            raise ProviderError(f"{self.label}: {resp['error'].get('message', 'error')}")
        return resp.get("webDetection", {})

    async def search(self, client: httpx.AsyncClient, inp: SearchInput) -> list[Candidate]:
        wd = await self._annotate(client, inp.image_bytes, 100)
        out: list[Candidate] = []
        for page in wd.get("pagesWithMatchingImages", []):
            imgs = [("exact", i["url"]) for i in page.get("fullMatchingImages", [])] + \
                   [("partial", i["url"]) for i in page.get("partialMatchingImages", [])]
            if not imgs:
                out.append(Candidate(self.name, "page", page_url=page.get("url"), page_title=page.get("pageTitle")))
            for cat, url in imgs:
                out.append(Candidate(self.name, cat, page_url=page.get("url"),
                                     page_title=_strip_tags(page.get("pageTitle")), image_url=url))
        for cat, key in (("exact", "fullMatchingImages"), ("partial", "partialMatchingImages"),
                         ("similar", "visuallySimilarImages")):
            for i in wd.get(key, []):
                out.append(Candidate(self.name, cat, image_url=i.get("url")))
        return out

    async def ping(self, client: httpx.AsyncClient) -> None:
        # 1x1 white PNG
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC")
        await self._annotate(client, png, 1)


def _strip_tags(s: str | None) -> str | None:
    if not s:
        return s
    import re
    return re.sub(r"<[^>]+>", "", s)
