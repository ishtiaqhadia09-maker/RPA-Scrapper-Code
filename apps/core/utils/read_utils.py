"""Read credentials and config from the project .env file."""

from __future__ import annotations

from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_ENV = _PROJECT_ROOT / ".env"


def _parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


class ReadConfig:
    """Read app URL and credentials from .env."""

    _cache: dict[str, str] | None = None

    @classmethod
    def _config(cls) -> dict[str, str]:
        if cls._cache is None:
            path = _DEFAULT_ENV
            if not path.is_file():
                raise FileNotFoundError(f".env file not found: {path}")
            cls._cache = _parse_env(path)
        return cls._cache

    @classmethod
    def reload(cls) -> None:
        cls._cache = None

    @classmethod
    def get(cls, key: str, default: str = "") -> str:
        return cls._config().get(key, default)

    @classmethod
    def getAppURL(cls) -> str:
        return cls.get("APP_URL")

    @classmethod
    def getUsername(cls) -> str:
        return cls.get("IQVIA_USER") or cls.get("APP_USERNAME")

    @classmethod
    def getPassword(cls) -> str:
        return cls.get("IQVIA_PASS") or cls.get("APP_PASSWORD")

    @classmethod
    def getOtp(cls) -> str:
        return cls.get("IQVIA_OTP") or cls.get("APP_OTP")

    @classmethod
    def getReportRow(cls) -> int | None:
        """Optional 1-based product-list row to process alone (IQVIA_ROW in .env)."""
        raw = cls.get("IQVIA_ROW").strip()
        if not raw:
            return None
        try:
            row = int(raw)
        except ValueError as exc:
            raise ValueError(
                f"IQVIA_ROW must be a positive integer, got {raw!r}"
            ) from exc
        if row < 1:
            raise ValueError(f"IQVIA_ROW must be >= 1, got {row}")
        return row
