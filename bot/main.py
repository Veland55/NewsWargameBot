"""Точка входа: python -m bot.main"""
from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramUnauthorizedError
from aiogram.types import MenuButtonWebApp, WebAppInfo

from . import handlers
from .claude import ClaudeClient
from .config import Config
from .db import Storage
from .llm import LLMClient
from .publisher import Publisher
from .quota import Quota
from .rss import close_http
from .search import BingNewsClient, SearchClient
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

    # Раньше здесь default executor урезался до 2 воркеров под фидпарсер —
    # но aiohttp резолвит DNS для ВСЕХ запросов бота (RSS, LLM, VK, поиск,
    # сам Telegram Bot API) через тот же default executor (нет aiodns).
    # Двух воркеров хватало впритык для последовательного разбора лент, но
    # при зависшем DNS одной ленты оба потока замораживались навсегда
    # (getaddrinfo не подчиняется никакому asyncio-таймауту) — и весь
    # остальной сетевой код бота вставал в очередь без единого свободного
    # потока. Оставляем asyncio-дефолт (min(32, cpu_count()+4)) вместо
    # искусственного сужения.

    storage = Storage(cfg.db_path)
    storage.prune_usage()
    llm = LLMClient(cfg.llm_base_url, cfg.llm_api_key, cfg.llm_model)
    # Модель, выбранная через /setmodel, важнее значения из .env.
    if saved_model := storage.get("model"):
        llm.model = saved_model
    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    vk = VKClient(cfg.vk_token, cfg.vk_group_id, cfg.vk_user_token)
    claude = ClaudeClient(cfg.claude_api_key, cfg.claude_model)
    # Gemini даёт OpenAI-совместимый /chat/completions — тот же LLMClient, что
    # и для DeepSeek/OpenRouter, просто второй экземпляр со своим ключом/URL.
    # Отдельного протокол-клиента, как для Claude, тут не нужно.
    # reasoning_effort="low": свежие модели Gemini «думают» перед ответом и без
    # этого тратят весь max_tokens на невидимые рассуждения, возвращая пустой
    # или обрезанный на середине текст (finish_reason=length). low сильно
    # сокращает эти рассуждения, но не до нуля — на подробных шаблонах всё
    # равно уходит 600-1000 токенов до первого слова ответа, поэтому берём
    # стартовый max_tokens побольше общего дефолта (800), чтобы не обрезать
    # пост на первой же попытке; LLMClient.complete всё равно подстрахует
    # повтором с удвоенным лимитом, если и этого не хватит.
    gemini = LLMClient(cfg.gemini_base_url, cfg.gemini_api_key, cfg.gemini_model,
                       reasoning_effort="low", max_tokens=1600)
    # Quota сама подключает on_usage у всех трёх клиентов (см. bot/quota.py) —
    # раньше это делалось руками только для llm, и расход в режиме Claude/
    # Gemini нигде не учитывался.
    quota = Quota(storage, llm, bot, cfg.admin_ids, claude=claude, gemini=gemini)
    # Сайты без RSS (см. bot/search.py) находят новые статьи через веб-поиск,
    # не разбором ленты — своим средствам сайта узнать «что нового» доверять
    # нельзя, кэш CDN нередко отдаёт устаревший снимок. Bing News — бесплатный
    # публичный RSS без ключа, работает всегда; Serper — платный (после
    # бесплатного лимита) API, нужен SERPER_API_KEY. Оба запрашиваются разом
    # и результаты сливаются — независимая индексация с разной задержкой,
    # каждый может найти то, что другой ещё не проиндексировал.
    search = SearchClient(cfg.serper_api_key)
    bing = BingNewsClient()
    publisher = Publisher(bot, storage, llm, cfg.channel_id,
                          admin_ids=cfg.admin_ids, quota=quota, vk=vk, claude=claude,
                          gemini=gemini, search=search, bing=bing,
                          panel_url=cfg.web_panel_public_url)

    dp = Dispatcher()
    handlers.setup(handlers.router, cfg.admin_ids)
    dp.include_router(handlers.router)
    # Прокидываем зависимости в хендлеры через DI aiogram.
    dp.workflow_data.update(st=storage, publisher=publisher, panel_url=cfg.web_panel_public_url)

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

    log.info("бот @%s запущен; канал: %s; модель: %s; VK: %s; Claude: %s; Gemini: %s",
             me.username, publisher.channel or "не задан", llm.model,
             (f"сообщество {publisher.vk_group}, картинка — {vk.photo_mode}"
              ) if publisher.vk_on else "выключен",
             f"включён, {claude.model}" if publisher.claude_mode else "выключен",
             f"включён, {gemini.model}" if publisher.gemini_mode else "выключен")
    if not llm.api_key:
        log.warning("LLM_API_KEY не задан — новости будут публиковаться без обработки")
    if storage.get("claude_mode") == "1" and not claude.api_key:
        log.warning("режим Claude включён командой, но CLAUDE_API_KEY не задан — "
                    "работает обычный LLM")
    if storage.get("gemini_mode") == "1" and not gemini.api_key:
        log.warning("режим Gemini включён командой, но GEMINI_API_KEY не задан — "
                    "работает обычный LLM")
    if not search.configured and any(f["kind"] == "search" for f in storage.feeds()):
        log.warning("есть сайты без RSS (/addsite), SERPER_API_KEY не задан — "
                    "работают только на Bing News (без ключа, но с меньшим охватом)")
    if not publisher.channel:
        log.warning("канал не задан — укажите CHANNEL_ID в .env или /setchannel")
    if publisher.debug:
        log.warning("включён режим отладки — посты уходят в личку, а не в канал")
    if cfg.vk_token and not publisher.vk_group.isdigit():
        log.warning("VK_TOKEN задан, но id сообщества нет — "
                    "укажите VK_GROUP_ID в .env или /vk group <id>")

    web_runner = None
    if cfg.web_panel_password:
        # Если задан публичный https-адрес — значит перед ботом уже стоит
        # nginx (см. SETUP.md), и слушать можно только localhost: свой порт
        # наружу светить незачем, только через прокси. Без публичного адреса
        # (сценарий "просто http по IP") слушаем все интерфейсы, иначе панель
        # была бы недоступна снаружи вообще.
        bind_host = "127.0.0.1" if cfg.web_panel_public_url else "0.0.0.0"
        web_runner, _ = await run_web_panel(storage, publisher, bot,
                                            cfg.web_panel_password, cfg.web_panel_port,
                                            host=bind_host, admin_ids=cfg.admin_ids,
                                            secure_cookies=bool(cfg.web_panel_public_url))
        where = cfg.web_panel_public_url or f"http://{bind_host}:{cfg.web_panel_port}"
        log.info("веб-панель: %s (слушает %s:%s)", where, bind_host, cfg.web_panel_port)

        if cfg.web_panel_public_url:
            button = MenuButtonWebApp(text="Панель", web_app=WebAppInfo(url=cfg.web_panel_public_url))
            for admin_id in cfg.admin_ids:
                try:
                    await bot.set_chat_menu_button(chat_id=admin_id, menu_button=button)
                except TelegramAPIError as exc:
                    # Обычно значит, что админ ещё ни разу не писал боту —
                    # Telegram не даёт поставить кнопку меню для незнакомого чата.
                    log.warning("не удалось поставить кнопку меню для %s: %s "
                                "(admin должен хотя бы раз написать боту)", admin_id, exc)
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
        await gemini.close()
        await search.close()
        await bing.close()
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
