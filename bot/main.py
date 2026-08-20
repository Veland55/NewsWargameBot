"""Точка входа: python -m bot.main"""
from __future__ import annotations

import asyncio
import logging
import socket
import sys
from concurrent.futures import ThreadPoolExecutor

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramUnauthorizedError

from . import handlers
from .claude import ClaudeClient
from .config import Config
from .db import Storage
from .llm import LLMClient
from .publisher import Publisher
from .quota import Quota
from .rss import FETCH_TIMEOUT, close_http
from .vk import VKClient
from .web import run_web_panel

log = logging.getLogger("bot")


async def run() -> None:
    cfg = Config.from_env()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)

    # feedparser качает ленты через urllib в отдельном треде — таймаут задаём глобально.
    socket.setdefaulttimeout(FETCH_TIMEOUT)

    # Ленты разбираются строго по одной, поэтому и поток нужен один. По
    # умолчанию asyncio завёл бы их до 32 — каждый со своей ареной malloc,
    # то есть с лишними мегабайтами, которые процесс уже не отдаст.
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=2, thread_name_prefix="feed")
    )

    storage = Storage(cfg.db_path)
    storage.prune_usage()
    llm = LLMClient(cfg.llm_base_url, cfg.llm_api_key, cfg.llm_model)
    # Модель, выбранная через /setmodel, важнее значения из .env.
    if saved_model := storage.get("model"):
        llm.model = saved_model
    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    quota = Quota(storage, llm, bot, cfg.admin_ids)
    llm.on_usage = quota.record
    vk = VKClient(cfg.vk_token, cfg.vk_group_id, cfg.vk_user_token)
    claude = ClaudeClient(cfg.claude_api_key, cfg.claude_model)
    publisher = Publisher(bot, storage, llm, cfg.channel_id,
                          admin_ids=cfg.admin_ids, quota=quota, vk=vk, claude=claude)

    dp = Dispatcher()
    handlers.setup(handlers.router, cfg.admin_ids)
    dp.include_router(handlers.router)
    # Прокидываем зависимости в хендлеры через DI aiogram.
    dp.workflow_data.update(st=storage, publisher=publisher)

    try:
        me = await bot.get_me()
    except TelegramUnauthorizedError:
        await bot.session.close()
        storage.close()
        raise SystemExit(
            "Telegram отклонил BOT_TOKEN.\n"
            "Проверьте значение в .env — токен выдаёт @BotFather "
            "(вид: 123456789:AAE...). Если токен пересоздавали, впишите новый."
        )
    except TelegramAPIError as exc:
        await bot.session.close()
        storage.close()
        raise SystemExit(f"Не удалось связаться с Telegram: {exc}")

    log.info("бот @%s запущен; канал: %s; модель: %s; VK: %s; Claude: %s",
             me.username, publisher.channel or "не задан", llm.model,
             (f"сообщество {publisher.vk_group}, картинка — {vk.photo_mode}"
              ) if publisher.vk_on else "выключен",
             f"включён, {claude.model}" if publisher.claude_mode else "выключен")
    if not llm.api_key:
        log.warning("LLM_API_KEY не задан — новости будут публиковаться без обработки")
    if storage.get("claude_mode") == "1" and not claude.api_key:
        log.warning("режим Claude включён командой, но CLAUDE_API_KEY не задан — "
                    "работает обычный LLM")
    if not publisher.channel:
        log.warning("канал не задан — укажите CHANNEL_ID в .env или /setchannel")
    if publisher.debug:
        log.warning("включён режим отладки — посты уходят в личку, а не в канал")
    if cfg.vk_token and not publisher.vk_group.isdigit():
        log.warning("VK_TOKEN задан, но id сообщества нет — "
                    "укажите VK_GROUP_ID в .env или /vk group <id>")

    web_runner = None
    if cfg.web_panel_password:
        web_runner, _ = await run_web_panel(storage, publisher, bot,
                                            cfg.web_panel_password, cfg.web_panel_port)
        log.info("веб-панель на http://0.0.0.0:%s (пароль задан)", cfg.web_panel_port)
    else:
        log.info("веб-панель выключена — WEB_PANEL_PASSWORD не задан в .env")

    poller = asyncio.create_task(publisher.run_forever(), name="rss-poller")
    try:
        await dp.start_polling(bot, handle_signals=True)
    finally:
        publisher.stop()
        poller.cancel()
        await asyncio.gather(poller, return_exceptions=True)
        if web_runner is not None:
            await web_runner.cleanup()
        await llm.close()
        await vk.close()
        await claude.close()
        await close_http()
        await bot.session.close()
        storage.close()
        log.info("остановлен")


def main() -> None:
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit) as exc:
        if isinstance(exc, SystemExit) and exc.code:
            print(exc, file=sys.stderr)
            raise


if __name__ == "__main__":
    main()
