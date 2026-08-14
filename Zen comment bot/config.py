from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "y"}


def _int(name: str, default: int = 0) -> int:
    value = os.getenv(name)
    return int(value) if value not in (None, "") else default

def _int_list(name: str) -> list[int]:
    value = os.getenv(name, "")
    if not value.strip():
        return []

    return [
        int(item.strip())
        for item in value.split(",")
        if item.strip()
    ]


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_admin_ids: list[int]

    yandex_api_key: str
    yandex_folder_id: str
    yandex_model: str
    yandex_base_url: str

    dzen_channel_url: str
    dzen_studio_url: str
    dzen_comments_url: str
    dzen_author_name: str
    dzen_headless: bool

    comments_enabled_on_start: bool
    poll_seconds: int
    max_comments_per_cycle: int
    max_publications_to_scan: int
    max_article_chars: int
    reply_delay_min_seconds: int
    reply_delay_max_seconds: int
    dry_run: bool

    blog_url: str
    data_dir: Path
    db_path: Path
    auth_state_path: Path
    debug_dir: Path

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.getenv("DATA_DIR", "/app/data")).expanduser()
        dzen_channel_url = os.getenv("DZEN_CHANNEL_URL", "https://dzen.ru/profile/specons").rstrip("/")
        default_studio = "https://dzen.ru/profile/editor/specons"
        return cls(
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_admin_ids=_int_list("TELEGRAM_ADMIN_ID"),
            yandex_api_key=os.getenv("YANDEX_API_KEY", ""),
            yandex_folder_id=os.getenv("YANDEX_FOLDER_ID", ""),
            yandex_model=os.getenv("YANDEX_MODEL", ""),
            yandex_base_url=os.getenv("YANDEX_BASE_URL", "https://ai.api.cloud.yandex.net/v1").rstrip("/"),
            dzen_channel_url=dzen_channel_url,
            dzen_studio_url=os.getenv("DZEN_STUDIO_URL", default_studio).rstrip("/"),
            dzen_comments_url=os.getenv("DZEN_COMMENTS_URL", "").strip(),
            dzen_author_name=os.getenv("DZEN_AUTHOR_NAME", "").strip(),
            dzen_headless=_bool("DZEN_HEADLESS", True),
            comments_enabled_on_start=_bool("COMMENTS_ENABLED_ON_START", False),
            poll_seconds=max(60, _int("POLL_SECONDS", 180)),
            max_comments_per_cycle=max(1, _int("MAX_COMMENTS_PER_CYCLE", 10)),
            max_publications_to_scan=max(1, _int("MAX_PUBLICATIONS_TO_SCAN", 12)),
            max_article_chars=max(1000, _int("MAX_ARTICLE_CHARS", 12000)),
            reply_delay_min_seconds=max(0, _int("REPLY_DELAY_MIN_SECONDS", 8)),
            reply_delay_max_seconds=max(0, _int("REPLY_DELAY_MAX_SECONDS", 25)),
            dry_run=_bool("DRY_RUN", True),
            blog_url=os.getenv("BLOG_URL", "https://boykovgroup.ru/blog").rstrip("/"),
            data_dir=data_dir,
            db_path=data_dir / "comments.sqlite3",
            auth_state_path=data_dir / "dzen_state.json",
            debug_dir=data_dir / "debug",
        )

    def yandex_model_uri(self) -> str:
        if self.yandex_model:
            return self.yandex_model
        if not self.yandex_folder_id:
            return ""
        return f"gpt://{self.yandex_folder_id}/yandexgpt/latest"

    def validate_runtime(self) -> list[str]:
        errors: list[str] = []
        if not self.telegram_bot_token:
            errors.append("TELEGRAM_BOT_TOKEN не задан")
        if not self.telegram_admin_ids:
            errors.append("TELEGRAM_ADMIN_ID is required")
        if not self.yandex_api_key:
            errors.append("YANDEX_API_KEY не задан")
        if not self.yandex_model_uri():
            errors.append("YANDEX_FOLDER_ID или YANDEX_MODEL не задан")
        if not self.auth_state_path.exists():
            errors.append(f"Нет авторизации Дзена: {self.auth_state_path}")
        return errors
