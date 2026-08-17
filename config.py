from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Не задана обязательная переменная {name}")
    return value

def _int_or_str(value: str) -> int | str:
    value = value.strip()
    return int(value) if value.lstrip("-").isdigit() else value

@dataclass(slots=True)
class Config:
    bot_token: str
    admin_ids: set[int]
    telegram_channel_id: int | str
    yc_folder_id: str
    yc_api_key: str | None
    yc_sa_key_file: str | None
    db_path: Path
    timezone: str
    default_publish_times: tuple[str, ...]
    subscription_price_stars: int
    subscription_title: str
    subscription_description: str
    tenant_channel_limit: int

def load_config() -> Config:
    admin_ids = {int(x.strip()) for x in _required("ADMIN_IDS").split(",") if x.strip()}
    api_key = os.getenv("YC_API_KEY", "").strip() or None
    sa_key_file = os.getenv("YC_SA_KEY_FILE", "").strip() or None
    if not api_key and not sa_key_file:
        raise RuntimeError("Нужно задать YC_API_KEY или YC_SA_KEY_FILE")


    return Config(
        bot_token=_required("TELEGRAM_BOT_TOKEN"),
        admin_ids=admin_ids,
        telegram_channel_id=_int_or_str(_required("TELEGRAM_CHANNEL_ID")),
        yc_folder_id=_required("YC_FOLDER_ID"),
        yc_api_key=api_key,
        yc_sa_key_file=sa_key_file,
        db_path=Path(os.getenv("DB_PATH", str(BASE_DIR / "data" / "bot.db"))),
        timezone=os.getenv("TIMEZONE", "Europe/Moscow").strip(),
        default_publish_times=tuple(
            part.strip()
            for part in os.getenv(
                "DEFAULT_PUBLISH_TIMES",
                "09:00,14:00,19:00",
            ).split(",")
            if part.strip()
        ),
        subscription_price_stars=max(1, int(os.getenv("SUBSCRIPTION_PRICE_STARS", "500"))),
        subscription_title=os.getenv("SUBSCRIPTION_TITLE", "Автопубликация статей").strip(),
        subscription_description=os.getenv(
            "SUBSCRIPTION_DESCRIPTION",
            "30 дней доступа к личной админ-панели и автопубликации в ваш Telegram-канал",
        ).strip(),
        tenant_channel_limit=max(1, int(os.getenv("TENANT_CHANNEL_LIMIT", "1"))),
    )
