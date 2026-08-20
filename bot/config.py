"""Конфигурация из переменных окружения / .env (без внешних зависимостей)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path | None = None) -> None:
    """Минимальный парсер .env: KEY=VALUE, # комментарии, кавычки опциональны."""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass(slots=True)
class Config:
    bot_token: str
    channel_id: str
    admin_ids: set[int] = field(default_factory=set)
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_model: str = "deepseek/deepseek-v4-flash"
    llm_api_key: str = ""
    vk_token: str = ""
    vk_group_id: str = ""
    vk_user_token: str = ""
    claude_api_key: str = ""
    claude_model: str = "claude-sonnet-5"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    web_panel_password: str = ""
    web_panel_port: int = 8090
    web_panel_public_url: str = ""
    db_path: Path = ROOT / "data" / "bot.db"
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise SystemExit("BOT_TOKEN не задан (см. .env.example)")

        raw_admins = [x for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x]
        bad = [x for x in raw_admins if not x.lstrip("-").isdigit()]
        if bad:
            raise SystemExit(
                f"ADMIN_IDS содержит не числа: {', '.join(bad)}\n"
                "Нужны числовые id через запятую, например ADMIN_IDS=123456789\n"
                "Свой id можно узнать у @userinfobot"
            )
        admins = {int(x) for x in raw_admins}
        if not admins:
            raise SystemExit(
                "ADMIN_IDS не задан — управлять ботом было бы некому.\n"
                "Узнайте свой id у @userinfobot и впишите в .env"
            )

        channel = os.getenv("CHANNEL_ID", "").strip()
        if channel and not (channel.startswith("@") or channel.lstrip("-").isdigit()):
            raise SystemExit(
                f"CHANNEL_ID={channel!r} не похож на канал.\n"
                "Ожидается @имя_канала или числовой id вида -1001234567890"
            )

        # id сообщества всегда положительный: минус к нему приписывает vk.py.
        vk_group = os.getenv("VK_GROUP_ID", "").strip().lstrip("-")
        if vk_group and not vk_group.isdigit():
            raise SystemExit(
                f"VK_GROUP_ID={vk_group!r} должен быть числом.\n"
                "Это числовой id сообщества, а не короткое имя — узнать можно "
                "на vk.com/<имя> → «Ещё» → «Статистика», либо через regvk.com/id"
            )

        db_path = Path(os.getenv("DB_PATH", "data/bot.db"))
        if not db_path.is_absolute():
            db_path = ROOT / db_path

        return cls(
            bot_token=token,
            channel_id=channel,
            admin_ids=admins,
            llm_base_url=os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/"),
            llm_model=os.getenv("LLM_MODEL", "deepseek/deepseek-v4-flash").strip(),
            llm_api_key=os.getenv("LLM_API_KEY", "").strip(),
            vk_token=os.getenv("VK_TOKEN", "").strip(),
            vk_group_id=vk_group,
            vk_user_token=os.getenv("VK_USER_TOKEN", "").strip(),
            claude_api_key=os.getenv("CLAUDE_API_KEY", "").strip(),
            claude_model=os.getenv("CLAUDE_MODEL", "claude-sonnet-5").strip(),
            gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip(),
            gemini_base_url=os.getenv(
                "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai"
            ).rstrip("/"),
            web_panel_password=os.getenv("WEB_PANEL_PASSWORD", "").strip(),
            web_panel_port=int(os.getenv("WEB_PANEL_PORT", "8090") or 8090),
            web_panel_public_url=os.getenv("WEB_PANEL_PUBLIC_URL", "").strip().rstrip("/"),
            db_path=db_path,
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )
