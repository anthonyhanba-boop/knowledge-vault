"""Provider interface. Providers return only what the upstream API actually returned."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import httpx


class ProviderError(Exception):
    def __init__(self, msg: str, retryable: bool = True):
        super().__init__(msg)
        self.retryable = retryable


@dataclass
class Candidate:
    provider: str
    category: str  # exact | partial | page | similar (as labelled by the provider)
    page_url: str | None = None
    page_title: str | None = None
    image_url: str | None = None
    thumbnail_url: str | None = None
    indexed_at: datetime | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class SearchInput:
    image_bytes: bytes          # sanitized JPEG
    public_url: str | None      # short-lived signed URL, only if PUBLIC_BASE_URL configured


class Provider:
    name: str = ""
    label: str = ""
    docs_url: str = ""
    key_env: str = ""
    supports_dates: bool = False

    def configured(self) -> bool:
        raise NotImplementedError

    def config_hint(self) -> str:
        return f"Set {self.key_env} in .env"

    async def search(self, client: httpx.AsyncClient, inp: SearchInput) -> list[Candidate]:
        raise NotImplementedError

    async def ping(self, client: httpx.AsyncClient) -> None:
        """Cheap authenticated health check; raise ProviderError on failure."""
        raise NotImplementedError


def raise_for(resp: httpx.Response, provider: str) -> None:
    if resp.status_code in (401, 403):
        raise ProviderError(f"{provider}: authentication rejected ({resp.status_code}) - check API key", retryable=False)
    if resp.status_code == 429:
        raise ProviderError(f"{provider}: rate limited / quota exceeded (429)")
    if resp.status_code >= 500:
        raise ProviderError(f"{provider}: upstream error {resp.status_code}")
    if resp.status_code >= 400:
        try:
            body = resp.json()
            err = body.get("error")
            msg = (err.get("message") if isinstance(err, dict) else err) or "; ".join(body.get("messages", []))
        except ValueError:
            msg = resp.text[:200]
        raise ProviderError(f"{provider}: request rejected ({resp.status_code}): {msg}", retryable=False)
