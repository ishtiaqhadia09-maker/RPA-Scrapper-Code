"""Playwright session persistence helpers."""

from __future__ import annotations

from pathlib import Path

from apps.core.paths import AUTH_DIR


class AuthSessionManager:
    """Manages Playwright browser auth state files under project_root/auth/."""

    @staticmethod
    def _auth_path(scraper_name: str) -> Path:
        AUTH_DIR.mkdir(parents=True, exist_ok=True)
        return AUTH_DIR / f"{scraper_name}_auth.json"

    def get_auth_path(self, scraper_name: str) -> str:
        return str(self._auth_path(scraper_name))

    def auth_file_exists(self, scraper_name: str) -> bool:
        return self._auth_path(scraper_name).is_file()

    def get_storage_state_path(self, scraper_name: str) -> str | None:
        path = self._auth_path(scraper_name)
        return str(path) if path.is_file() else None

    def save_auth_from_context(self, context, scraper_name: str) -> None:
        context.storage_state(path=self.get_auth_path(scraper_name))
