"""Environment helpers shared by Wardline backend modules."""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_REGION = "us-east-1"
# backend/shared/config.py -> parents[2] is wardline/, parents[3] is the repo root.
WARDLINE_ROOT = Path(__file__).resolve().parents[2]
ENV_SEARCH_PATHS = (WARDLINE_ROOT / ".env", WARDLINE_ROOT.parent / ".env")


def resolve_region(explicit: str | None = None, *service_env_vars: str) -> str:
    """Pick an AWS region: explicit arg, service override, AWS_REGION, default."""

    candidates = [explicit, *(os.getenv(name) for name in service_env_vars)]
    candidates += [os.getenv("AWS_REGION"), os.getenv("AWS_DEFAULT_REGION")]
    return next((value for value in candidates if value), DEFAULT_REGION)


def load_env_file(path: Path) -> list[str]:
    """Load KEY=VALUE lines into os.environ without overriding existing values.

    Returns the names of the keys that were set. Values are never returned,
    printed, or logged.
    """

    loaded: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


def load_default_env() -> tuple[Path | None, list[str]]:
    """Load the first .env found in wardline/ then the repo root.

    Returns ``(path, loaded_keys)``, or ``(None, [])`` when no file exists.
    """

    for path in ENV_SEARCH_PATHS:
        if path.is_file():
            return path, load_env_file(path)
    return None, []
