"""
Centralised configuration.

Every tunable in the system is read from the environment here and nowhere else.
Modules import `settings`; they never call os.getenv themselves. That means one
place to look when behaviour differs between your laptop, CI and a container,
and one place to document the knobs.

Deliberately stdlib-only: `src.config` is imported by the core pipeline, so it
must not drag in pydantic, FastAPI or anything else the ML path doesn't need.

Load order for each value:
    1. explicit environment variable
    2. a matching line in `.env` at the project root (if present)
    3. the default below

Usage:
    from src.config import settings
    settings.embedder_backend        # -> "tfidf"
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# --------------------------------------------------------------- .env loading


def _load_dotenv(path: Path) -> dict[str, str]:
    """Minimal .env parser. Real env vars always win, so this never clobbers."""
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip().strip('"').strip("'")
        out[key.strip()] = val
    return out


_DOTENV = _load_dotenv(ROOT / ".env")


def _get(name: str, default: str) -> str:
    return os.environ.get(name, _DOTENV.get(name, default))


def _get_opt(name: str) -> str | None:
    val = os.environ.get(name, _DOTENV.get(name))
    return val or None


def _get_int(name: str, default: int) -> int:
    try:
        return int(_get(name, str(default)))
    except ValueError:
        return default


def _get_float(name: str, default: float) -> float:
    try:
        return float(_get(name, str(default)))
    except ValueError:
        return default


def _get_opt_float(name: str) -> float | None:
    raw = _get_opt(name)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _get_bool(name: str, default: bool) -> bool:
    return _get(name, "1" if default else "0").lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------- settings


@dataclass(frozen=True)
class Settings:
    # -- paths --
    root: Path = ROOT
    db_path: Path = field(default_factory=lambda: Path(
        _get("DB_PATH", str(ROOT / "data" / "aggregator.db"))
    ))
    sample_data_path: Path = field(default_factory=lambda: Path(
        _get("SAMPLE_DATA_PATH", str(ROOT / "data" / "sample_listings.json"))
    ))

    # -- embedding --
    # "auto" tries sentence-transformers then falls back; "tfidf" skips the
    # slow torch import entirely; "transformer" hard-fails if unavailable.
    embedder_backend: str = field(default_factory=lambda: _get("EMBEDDER_BACKEND", "auto").lower())
    embedder_model: str = field(default_factory=lambda: _get("EMBEDDER_MODEL", "all-MiniLM-L6-v2"))

    # -- entity resolution --
    # None means "use EntityResolver.DEFAULT_THRESHOLD for the active backend".
    similarity_threshold: float | None = field(
        default_factory=lambda: _get_opt_float("SIMILARITY_THRESHOLD")
    )

    # -- catalogue (the PRODUCT dimension; see src/catalogue.py) --
    # auto      try the live keyless API, fall back to the bundled file
    # dummyjson  force live -- fail loudly rather than silently degrading
    # file       force the offline fixture
    catalogue_source: str = field(
        default_factory=lambda: _get("CATALOGUE_SOURCE", "auto").lower())

    # -- money --
    # Ranking compares prices numerically, so everything is converted to this
    # currency before it reaches TOPSIS. See src/money.py.
    base_currency: str = field(default_factory=lambda: _get("BASE_CURRENCY", "INR").upper())

    # -- caching --
    cache_ttl_seconds: int = field(default_factory=lambda: _get_int("CACHE_TTL_SECONDS", 3600))
    cache_enabled: bool = field(default_factory=lambda: _get_bool("CACHE_ENABLED", True))

    # -- live sources --
    ebay_client_id: str | None = field(default_factory=lambda: _get_opt("EBAY_CLIENT_ID"))
    ebay_client_secret: str | None = field(default_factory=lambda: _get_opt("EBAY_CLIENT_SECRET"))
    ebay_marketplace: str = field(default_factory=lambda: _get("EBAY_MARKETPLACE", "EBAY_US"))

    # -- HTTP resilience (see src/adapters/http.py) --
    http_timeout_seconds: int = field(default_factory=lambda: _get_int("HTTP_TIMEOUT_SECONDS", 15))
    http_max_retries: int = field(default_factory=lambda: _get_int("HTTP_MAX_RETRIES", 3))
    http_backoff_base: float = field(default_factory=lambda: _get_float("HTTP_BACKOFF_BASE", 0.5))
    http_rate_limit_per_sec: float = field(
        default_factory=lambda: _get_float("HTTP_RATE_LIMIT_PER_SEC", 5.0)
    )
    user_agent: str = field(default_factory=lambda: _get(
        "USER_AGENT",
        "ecom-aggregator/0.2 (academic research project; contact: repo owner)",
    ))

    # -- synthetic market (used when no live API keys are configured) --
    synthetic_seed: int = field(default_factory=lambda: _get_int("SYNTHETIC_SEED", 20260804))
    synthetic_daily_volatility: float = field(
        default_factory=lambda: _get_float("SYNTHETIC_DAILY_VOLATILITY", 0.012)
    )

    # -- logging --
    log_level: str = field(default_factory=lambda: _get("LOG_LEVEL", "INFO").upper())
    log_format: str = field(default_factory=lambda: _get("LOG_FORMAT", "text").lower())

    # -- api --
    api_host: str = field(default_factory=lambda: _get("API_HOST", "127.0.0.1"))
    api_port: int = field(default_factory=lambda: _get_int("API_PORT", 8000))
    api_cors_origins: str = field(default_factory=lambda: _get("API_CORS_ORIGINS", "*"))

    def has_ebay_credentials(self) -> bool:
        return bool(self.ebay_client_id and self.ebay_client_secret)

    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.api_cors_origins.split(",") if o.strip()]

    def describe(self) -> dict[str, object]:
        """Config dump for /health and startup logs. Secrets are redacted."""
        secret_names = {"ebay_client_id", "ebay_client_secret"}
        out: dict[str, object] = {}
        for f in fields(self):
            val = getattr(self, f.name)
            if f.name in secret_names:
                out[f.name] = "***set***" if val else None
            elif isinstance(val, Path):
                out[f.name] = str(val)
            else:
                out[f.name] = val
        return out


settings = Settings()


def reload_settings() -> Settings:
    """Re-read the environment. Only needed by tests that monkeypatch env vars."""
    global settings
    settings = Settings()
    return settings
