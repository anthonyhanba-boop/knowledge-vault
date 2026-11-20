"""Runtime configuration. All secrets come from environment variables and never leave the server."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    google_vision_api_key: str = field(default_factory=lambda: os.getenv("GOOGLE_VISION_API_KEY", ""))
    tineye_api_key: str = field(default_factory=lambda: os.getenv("TINEYE_API_KEY", ""))
    serpapi_api_key: str = field(default_factory=lambda: os.getenv("SERPAPI_API_KEY", ""))
    public_base_url: str = field(default_factory=lambda: os.getenv("PUBLIC_BASE_URL", "").rstrip("/"))

    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", "./data")))
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", ""))
    secret_key: str = field(default_factory=lambda: os.getenv("SECRET_KEY", ""))
    max_upload_mb: int = field(default_factory=lambda: int(os.getenv("MAX_UPLOAD_MB", "10")))
    upload_retention_minutes: int = field(default_factory=lambda: int(os.getenv("UPLOAD_RETENTION_MINUTES", "30")))
    rate_limit_per_hour: int = field(default_factory=lambda: int(os.getenv("RATE_LIMIT_SEARCHES_PER_HOUR", "30")))
    respect_robots: bool = field(default_factory=lambda: _bool("RESPECT_ROBOTS_TXT", True))
    verify_max_candidates: int = field(default_factory=lambda: int(os.getenv("VERIFY_MAX_CANDIDATES", "60")))

    def __post_init__(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "uploads").mkdir(exist_ok=True)
        (self.data_dir / "thumbs").mkdir(exist_ok=True)
        if not self.database_url:
            self.database_url = f"sqlite:///{(self.data_dir / 'mirror.db').as_posix()}"
        if not self.secret_key:
            keyfile = self.data_dir / ".secret_key"
            if not keyfile.exists():
                keyfile.write_text(secrets.token_hex(32))
                keyfile.chmod(0o600)
            self.secret_key = keyfile.read_text().strip()


settings = Settings()
