"""Команды управления ботом (только для админов из ADMIN_IDS)."""
from __future__ import annotations

import html
import logging
import time

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.types import (BufferedInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, InputMediaPhoto,
                           LinkPreviewOptions, Message, WebAppInfo)

from .db import DEFAULTS, Storage
from .llm import LLMError
from .publisher import (TG_CAPTION_LIMIT, TG_LIMIT, Publisher, _ext_for,
                        html_problem, tg_len)
from .quota import until_reset
from .rss import fetch
from .vk import VKError, to_plain

log = logging.getLogger(__name__)
router = Router(name="admin")

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

HELP = """<b>RSS → ИИ → канал</b>

/panel — открыть веб-панель управления внутри Telegram (если подключена)

<b>Ленты</b>
/add &lt;url&gt; [название] — добавить ленту
/list — список лент
/del &lt;id&gt; — удалить ленту
/pause &lt;id&gt; · /resume &lt;id&gt; — выключить/включить ленту
/test &lt;id&gt; — прогнать последнюю новость через шаблон без публикации

<b>Опубликованные посты</b>
/posts [n] — последние опубликованные посты (по умолчанию 10)
/edit &lt;id&gt; — показать пост и что с ним можно сделать
/setpost &lt;id&gt; &lt;текст&gt; — заменить текст поста вручную
/regen &lt;id&gt; [пожелание] — перегенерировать текст через ИИ из исходной новости
/delimage &lt;id&gt; &lt;номер&gt; — убрать одну картинку из альбома (если их больше 1)

<b>Отладка</b>
/debug on · /debug off — посты приходят в личку вместо канала
/usage — расход лимита ИИ за сутки

<b>Шаблоны</b>
/template — показать промпт
/settemplate &lt;текст&gt; — задать промпт
/format — показать формат поста
/setformat &lt;текст&gt; — задать формат поста
/reset template|format — вернуть значение по умолчанию

<b>Свой промпт для отдельной ленты</b>
/feedtemplate &lt;id&gt; — показать промпт ленты (свой или общий)
/setfeedtemplate &lt;id&gt; &lt;текст&gt; — задать свой промпт только для этой ленты
/resetfeedtemplate &lt;id&gt; — вернуть общий промпт

Плейсхолдеры: <code>{title}</code> <code>{summary}</code> <code>{link}</code> <code>{source}</code> <code>{published}</code>
В формате поста дополнительно <code>{ai}</code> — ответ модели. Формат поста поддерживает HTML-теги Telegram.

Картинка из новости прикладывается сама, отдельного плейсхолдера не нужно: до 1024 символов — фото с подписью, длиннее — превью над текстом. Если картинки нет в самой ленте, бот берёт её со страницы новости (og:image). Выключить: <code>/set images 0</code>, только дочитывание со страницы — <code>/set og_image 0</code>

Несколько картинок альбомом вместо одной (до 10, работает при любом режиме — обычном, Claude, Gemini) — настройка отдельной ленты, не общая: <code>/feedimages &lt;id&gt; on</code>, сколько штук — <code>/set max_images 6</code> (общее для всех). Вернуть как раньше — <code>/feedimages &lt;id&gt; off</code>

Новость, похожая на уже опубликованную с другой ленты, сама в канал не уходит — ждёт разбора: <code>/duplicates</code> (посмотреть картинки, опубликовать или удалить — удобнее в веб-панели, раздел «Ленты»). Выключить: <code>/set dedup_enabled 0</code>

<b>Настройки</b>
/status — состояние бота
/model · /setmodel &lt;название&gt; — модель LLM
/interval &lt;мин&gt; — периодичность проверки
/set &lt;ключ&gt; &lt;значение&gt; — прочие параметры (см. /status)
/setchannel &lt;@канал|id&gt; — куда публиковать
/feedimages &lt;id&gt; — одна картинка или несколько альбомом для этой ленты (см. выше)
/vk — дублирование постов в сообщество VK
/claude — обработка через платный Claude
/gemini — обработка через Gemini (обычно бесплатно), взаимоисключимо с Claude
/checknow — проверить ленты немедленно
/stop · /start — глобальная пауза и снятие паузы"""


class IsAdmin(BaseFilter):
    def __init__(self, admin_ids: set[int]):
        self.admin_ids = admin_ids

    async def __call__(self, message: Message) -> bool:
        return bool(message.from_user and message.from_user.id in self.admin_ids)


def setup(router_: Router, admin_ids: set[int]) -> None:
    """Вешаем проверку прав на все хендлеры роутера сразу."""
    guard = IsAdmin(admin_ids)
    router_.message.filter(guard)


def _e(text: str) -> str:
    return html.escape(str(text or ""))


async def _reply(message: Message, text: str) -> None:
    await message.answer(text, parse_mode="HTML", link_preview_options=NO_PREVIEW)


async def _preview_post(message: Message, post) -> bool:
    """Показывает пост в личке так же, как он ушёл бы в канал. True — картинка
    (одна или альбом) показана, False — нечем, вызывающий шлёт текстом сам."""
    if post.images:
        media = [InputMediaPhoto(
            media=BufferedInputFile(data, filename=f"image{i}.{_ext_for(ctype)}"),
            caption=post.text if i == 0 and tg_len(post.text) <= TG_CAPTION_LIMIT else None,
            parse_mode="HTML" if i == 0 else None,
        ) for i, (data, ctype) in enumerate(post.images)]
        try:
            if len(media) == 1:
                await message.answer_photo(
                    media[0].media, caption=media[0].caption, parse_mode="HTML")
            else:
                await message.answer_media_group(media)
            if not (tg_len(post.text) <= TG_CAPTION_LIMIT):
                await message.answer(post.text, parse_mode="HTML",
                                     link_preview_options=NO_PREVIEW)
            return True
        except Exception as exc:
            await _reply(message, f"⚠️ Telegram не принял картинки: "
                                  f"<code>{_e(exc)}</code>\nПост уйдёт без них.")
            return False

    if post.image and tg_len(post.text) <= TG_CAPTION_LIMIT:
        try:
            await message.answer_photo(post.image, caption=post.text,
                                       parse_mode="HTML")
            return True
        except Exception as exc:
            await _reply(message, f"⚠️ Telegram не принял картинку "
                                  f"<code>{_e(post.image)}</code>: <code>{_e(exc)}</code>\n"
                                  f"Пост уйдёт без неё.")
            return False
    if post.image:
        # Длинный пост в подпись не влезает — как и в канале, показываем
        # картинку превью-ссылкой над текстом.
        await message.answer(
            post.text, parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(
                url=post.image, is_disabled=False, prefer_large_media=True,
                show_above_text=True),
        )
        return True
    return False


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await _reply(message, HELP)


@router.message(Command("start"))
async def cmd_start(message: Message, st: Storage) -> None:
    if st.get("paused") == "1":
        st.set("paused", "0")
        await _reply(message, "▶️ Публикация возобновлена.\n\n" + HELP)
    else:
        await _reply(message, HELP)


@router.message(Command("panel"))
async def cmd_panel(message: Message, panel_url: str = "") -> None:
    if not panel_url:
        await _reply(
            message,
            "Веб-панель не подключена к боту: не задан <code>WEB_PANEL_PUBLIC_URL</code> "
            "в .env (нужен настоящий https-адрес — Telegram не откроет по-другому). "
            "Подробности — SETUP.md, раздел «Веб-панель управления».",
        )
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🖥 Открыть панель", web_app=WebAppInfo(url=panel_url))
    ]])
    await message.answer("Панель управления ботом:", reply_markup=kb)


@router.message(Command("stop"))
async def cmd_stop(message: Message, st: Storage) -> None:
    st.set("paused", "1")
    await _reply(message, "⏸ Публикация приостановлена. Вернуть — /start")


@router.message(Command("add"))
async def cmd_add(message: Message, st: Storage, publisher: Publisher) -> None:
    args = (message.text or "").split(maxsplit=2)[1:]
    if not args:
        await _reply(message, "Как использовать: <code>/add https://example.com/rss Название</code>")
        return

    url = args[0].strip()
    title = args[1].strip() if len(args) > 1 else ""
    if not url.startswith(("http://", "https://")):
        await _reply(message, "Нужна ссылка, начинающаяся на http:// или https://")
        return

    await _reply(message, "Проверяю ленту…")
    result = await fetch(url)
    if result.error:
        await _reply(message, f"❌ Лента недоступна: <code>{_e(result.error)}</code>")
        return
    if not result.entries:
        await _reply(message, "❌ В ленте нет записей — проверьте адрес.")
        return

    feed_id = st.add_feed(url, title or result.feed_title[:120])
    if feed_id is None:
        await _reply(message, "Такая лента уже добавлена — /list")
        return

    last = result.entries[-1]
    await _reply(
        message,
        f"✅ Лента <b>#{feed_id}</b> добавлена: {_e(title or result.feed_title or url)}\n"
        f"Записей в выдаче: {len(result.entries)}\n"
        f"Последняя: {_e(last.title[:150])}\n\n"
        f"Проверить обработку: /test {feed_id}",
    )
    publisher.wake()


@router.message(Command("list"))
async def cmd_list(message: Message, st: Storage) -> None:
    feeds = st.feeds()
    if not feeds:
        await _reply(message, "Лент пока нет. Добавить: <code>/add &lt;url&gt;</code>")
        return

    lines = ["<b>Ленты</b>"]
    for f in feeds:
        mark = "✅" if f["enabled"] else "⏸"
        checked = (
            time.strftime("%d.%m %H:%M", time.localtime(f["last_check"]))
            if f["last_check"] else "ещё не проверялась"
        )
        lines.append(
            f"\n{mark} <b>#{f['id']}</b> {_e(f['title'] or '(без названия)')}\n"
            f"   <code>{_e(f['url'])}</code>\n"
            f"   проверена: {checked}, в архиве: {st.seen_count(f['id'])}"
        )
        if f["last_error"]:
            lines.append(f"   ⚠️ {_e(f['last_error'][:150])}")
        if f["template"]:
            lines.append(f"   📝 свой промпт — <code>/feedtemplate {f['id']}</code>")
        if f["multi_images"]:
            lines.append(f"   🖼 несколько картинок — <code>/feedimages {f['id']}</code>")
    await _reply(message, "\n".join(lines))


@router.message(Command("del"))
async def cmd_del(message: Message, command: CommandObject, st: Storage) -> None:
    feed_id = _parse_id(command.args)
    if feed_id is None:
        await _reply(message, "Как использовать: <code>/del 1</code> (id смотрите в /list)")
        return
    if st.delete_feed(feed_id):
        await _reply(message, f"🗑 Лента #{feed_id} удалена.")
    else:
        await _reply(message, f"Ленты #{feed_id} нет.")


@router.message(Command("pause"))
async def cmd_pause(message: Message, command: CommandObject, st: Storage) -> None:
    feed_id = _parse_id(command.args)
    if feed_id is None or not st.set_enabled(feed_id, False):
        await _reply(message, "Как использовать: <code>/pause 1</code>")
        return
    await _reply(message, f"⏸ Лента #{feed_id} выключена.")


@router.message(Command("resume"))
async def cmd_resume(message: Message, command: CommandObject, st: Storage) -> None:
    feed_id = _parse_id(command.args)
    if feed_id is None or not st.set_enabled(feed_id, True):
        await _reply(message, "Как использовать: <code>/resume 1</code>")
        return
    await _reply(message, f"▶️ Лента #{feed_id} включена.")


@router.message(Command("test"))
async def cmd_test(message: Message, command: CommandObject, st: Storage,
                   publisher: Publisher) -> None:
    feed_id = _parse_id(command.args)
    feed = st.feed(feed_id) if feed_id is not None else None
    if feed is None:
        await _reply(message, "Как использовать: <code>/test 1</code> (id смотрите в /list)")
        return

    await _reply(message, f"Забираю ленту и прогоняю через {_e(publisher.active_backend_label)}…")
    result = await fetch(feed["url"])
    if result.error or not result.entries:
        await _reply(message, f"❌ {_e(result.error or 'в ленте нет записей')}")
        return

    entry = result.entries[-1]
    started = time.monotonic()
    try:
        post = await publisher.build_post(entry, feed)
    except LLMError as exc:
        if publisher.claude_mode:
            backend_name, model_hint = "Claude", "Проверьте CLAUDE_API_KEY / CLAUDE_MODEL в .env"
        elif publisher.gemini_mode:
            backend_name, model_hint = "Gemini", "Проверьте GEMINI_API_KEY / GEMINI_MODEL в .env"
        else:
            backend_name, model_hint = publisher.llm.model, "Проверьте LLM_API_KEY / LLM_MODEL / LLM_BASE_URL в .env"
        await _reply(
            message,
            f"❌ {backend_name} вернул ошибку: <code>{_e(exc)}</code>\n\n{model_hint}",
        )
        return

    elapsed = time.monotonic() - started
    if post.images:
        picture = f"картинок: {len(post.images)}"
    elif post.image:
        picture = "с картинкой"
    else:
        picture = ("без картинки — в новости её нет" if st.get("images") == "1"
                   else "без картинки — /set images 1")
    await _reply(message, f"⬇️ Предпросмотр (за {elapsed:.1f}s, {picture}, "
                          f"<b>не</b> опубликовано)")
    shown = await _preview_post(message, post)
    if not shown:
        await message.answer(post.text, parse_mode="HTML",
                             link_preview_options=NO_PREVIEW)

    if publisher.vk_on:
        # В VK уходит тот же пост без разметки — показываем и его, чтобы
        # не выяснять постфактум, во что превратились теги и ссылки.
        await _reply(message, "⬇️ Так же новость уйдёт в VK (тоже не опубликовано):")
        # parse_mode=None обязателен: у бота по умолчанию HTML, а текст для VK
        # уже без разметки — случайный «<» иначе сорвал бы отправку.
        await message.answer(to_plain(post.text) or "(пусто)", parse_mode=None,
                             link_preview_options=NO_PREVIEW)


@router.message(Command("template"))
async def cmd_template(message: Message, st: Storage, publisher: Publisher) -> None:
    await _reply(
        message,
        f"<b>Промпт</b> (сейчас — {_e(publisher.active_backend_label)})\n"
        f"<pre>{_e(st.get('template'))}</pre>\n"
        f"Изменить: <code>/settemplate текст</code>",
    )


@router.message(Command("settemplate"))
async def cmd_settemplate(message: Message, st: Storage) -> None:
    text = _tail(message.text)
    if not text:
        await _reply(
            message,
            "Пришлите промпт одним сообщением после команды (перенос строки — Shift+Enter):\n\n"
            "<code>/settemplate Перепиши новость в 2 предложения.\n\n"
            "Заголовок: {title}\nТекст: {summary}</code>",
        )
        return
    if "{summary}" not in text and "{title}" not in text:
        await _reply(message, "⚠️ В промпте нет ни <code>{title}</code>, ни <code>{summary}</code> — "
                              "модель не получит саму новость. Шаблон не сохранён.")
        return
    st.set("template", text)
    await _reply(message, "✅ Промпт сохранён. Проверить: <code>/test &lt;id&gt;</code>")


@router.message(Command("feedtemplate"))
async def cmd_feedtemplate(message: Message, command: CommandObject, st: Storage) -> None:
    feed_id = _parse_id(command.args)
    feed = st.feed(feed_id) if feed_id is not None else None
    if feed is None:
        await _reply(message, "Как использовать: <code>/feedtemplate 1</code> (id смотрите в /list)")
        return
    name = _e(feed["title"] or feed["url"])
    if feed["template"]:
        await _reply(
            message,
            f"<b>Свой промпт ленты #{feed_id}</b> ({name})\n<pre>{_e(feed['template'])}</pre>\n\n"
            f"Изменить: <code>/setfeedtemplate {feed_id} текст</code>\n"
            f"Вернуть общий промпт: <code>/resetfeedtemplate {feed_id}</code>",
        )
        return
    await _reply(
        message,
        f"Лента #{feed_id} ({name}) использует общий промпт — /template.\n\n"
        f"Задать свой только для неё: <code>/setfeedtemplate {feed_id} текст</code>",
    )


@router.message(Command("setfeedtemplate"))
async def cmd_setfeedtemplate(message: Message, command: CommandObject, st: Storage) -> None:
    feed_id, text = _split_id_and_text(command.args)
    feed = st.feed(feed_id) if feed_id is not None else None
    if feed is None or not text:
        await _reply(
            message,
            "Как использовать: <code>/setfeedtemplate 1 текст промпта</code> (id смотрите в /list)\n\n"
            "Пришлите текст одним сообщением после id (перенос строки — Shift+Enter):\n\n"
            "<code>/setfeedtemplate 1 Перепиши в 2 предложения.\n\n"
            "Заголовок: {title}\nТекст: {summary}</code>",
        )
        return
    if "{summary}" not in text and "{title}" not in text:
        await _reply(message, "⚠️ В промпте нет ни <code>{title}</code>, ни <code>{summary}</code> — "
                              "модель не получит саму новость. Не сохранено.")
        return
    st.update_feed(feed_id, template=text)
    await _reply(message, f"✅ Свой промпт для ленты #{feed_id} сохранён. "
                          f"Проверить: <code>/test {feed_id}</code>")


@router.message(Command("resetfeedtemplate"))
async def cmd_resetfeedtemplate(message: Message, command: CommandObject, st: Storage) -> None:
    feed_id = _parse_id(command.args)
    feed = st.feed(feed_id) if feed_id is not None else None
    if feed is None:
        await _reply(message, "Как использовать: <code>/resetfeedtemplate 1</code> (id смотрите в /list)")
        return
    if not feed["template"]:
        await _reply(message, f"У ленты #{feed_id} и так общий промпт — менять нечего.")
        return
    st.update_feed(feed_id, template=None)
    await _reply(message, f"↩️ Лента #{feed_id} снова использует общий промпт — /template.")


@router.message(Command("format"))
async def cmd_format(message: Message, st: Storage) -> None:
    await _reply(
        message,
        f"<b>Формат поста</b>\n<pre>{_e(st.get('post_format'))}</pre>\n"
        f"Изменить: <code>/setformat текст</code>",
    )


@router.message(Command("setformat"))
async def cmd_setformat(message: Message, st: Storage) -> None:
    text = _tail(message.text)
    if not text:
        await _reply(
            message,
            "Пример:\n<code>/setformat &lt;b&gt;{title}&lt;/b&gt;\n\n{ai}\n\n"
            "&lt;a href=\"{link}\"&gt;Источник&lt;/a&gt;</code>",
        )
        return
    if "{ai}" not in text:
        await _reply(message, "⚠️ Без <code>{ai}</code> в посте не будет текста от модели. Не сохранено.")
        return
    problem = html_problem(text)
    if problem:
        await _reply(
            message,
            f"⚠️ Разметка не годится: {problem}.\n\n"
            f"Telegram отверг бы такой пост целиком. Формат не сохранён.\n"
            f"Допустимые теги: <code>b i u s code pre a blockquote</code>",
        )
        return
    st.set("post_format", text)
    await _reply(message, "✅ Формат поста сохранён. Проверить: <code>/test &lt;id&gt;</code>")


@router.message(Command("reset"))
async def cmd_reset(message: Message, command: CommandObject, st: Storage) -> None:
    what = (command.args or "").strip()
    keys = {"template": "template", "format": "post_format"}
    if what not in keys:
        await _reply(message, "Как использовать: <code>/reset template</code> или <code>/reset format</code>")
        return
    key = keys[what]
    st.set(key, DEFAULTS[key])
    await _reply(message, f"↩️ Значение «{what}» сброшено к умолчанию.")


@router.message(Command("interval"))
async def cmd_interval(message: Message, command: CommandObject, st: Storage,
                       publisher: Publisher) -> None:
    value = _parse_id(command.args)
    if value is None or not 1 <= value <= 1440:
        await _reply(message, f"Как использовать: <code>/interval 15</code> (1–1440 мин)\n"
                              f"Сейчас: {st.get_int('interval')} мин")
        return
    st.set("interval", value)
    publisher.wake()
    await _reply(message, f"✅ Проверяю ленты каждые {value} мин.")


@router.message(Command("set"))
async def cmd_set(message: Message, command: CommandObject, st: Storage) -> None:
    editable = {
        "interval", "max_per_cycle", "post_delay", "backfill",
        "max_age_days", "flood_guard",
        "on_llm_error", "require_russian", "disable_preview", "images",
        "og_image", "max_images", "keep_seen",
        "alert_thresholds", "free_daily_limit",
        "dedup_enabled", "dedup_window_days", "dedup_threshold",
    }
    parts = (command.args or "").split(maxsplit=1)
    if len(parts) < 2 or parts[0] not in editable:
        await _reply(
            message,
            "Как использовать: <code>/set max_per_cycle 5</code>\n\nДоступные ключи:\n"
            + "\n".join(f"· <code>{k}</code> = {_e(st.get(k))}" for k in sorted(editable)),
        )
        return
    key, value = parts[0], parts[1].strip()

    if key == "on_llm_error":
        if value not in ("raw", "skip"):
            await _reply(message, "on_llm_error принимает <code>raw</code> или <code>skip</code>")
            return
    elif key == "max_images":
        if not value.isdigit() or not (1 <= int(value) <= 10):
            await _reply(message, "max_images — число от 1 до 10.")
            return
    elif key == "dedup_threshold":
        if not value.isdigit() or not (1 <= int(value) <= 100):
            await _reply(message, "dedup_threshold — число от 1 до 100 (% схожести).")
            return
    elif key == "alert_thresholds":
        parsed = [p for p in value.replace(" ", "").split(",") if p]
        if not all(p.isdigit() and 1 <= int(p) <= 100 for p in parsed) or not parsed:
            await _reply(message, "Пороги — числа 1–100 через запятую, например "
                                  "<code>/set alert_thresholds 70,90</code>")
            return
        value = ",".join(str(int(p)) for p in sorted({int(p) for p in parsed}))
    elif not value.isdigit():
        await _reply(message, f"{_e(key)} ожидает число.")
        return

    st.set(key, value)
    await _reply(message, f"✅ <code>{_e(key)}</code> = <code>{_e(value)}</code>")


@router.message(Command("setchannel"))
async def cmd_setchannel(message: Message, command: CommandObject, st: Storage,
                         bot: Bot, publisher: Publisher) -> None:
    target = (command.args or "").strip()
    if not target:
        await _reply(message, f"Как использовать: <code>/setchannel @my_channel</code>\n"
                              f"Сейчас: <code>{_e(publisher.channel or 'не задан')}</code>")
        return
    try:
        chat = await bot.get_chat(target)
    except Exception as exc:
        await _reply(message, f"❌ Не вижу такой чат: <code>{_e(exc)}</code>\n"
                              f"Бот должен быть админом канала.")
        return
    st.set("channel_id", str(chat.id))
    await _reply(message, f"✅ Публикую в «{_e(chat.title or chat.id)}» (<code>{chat.id}</code>)")
    publisher.wake()


MODEL_HINTS = """Примеры моделей OpenRouter:

<b>DeepSeek</b> (платный, но дешёвый — цена за 1000 новостей):
· <code>deepseek/deepseek-v4-flash</code> — ~22 ₽
· <code>deepseek/deepseek-v3.2</code> — ~40 ₽
· <code>deepseek/deepseek-chat</code> — ~52 ₽

<b>Бесплатные</b> (не DeepSeek, есть суточный лимит):
· <code>nvidia/nemotron-3-super-120b-a12b:free</code>
· <code>google/gemma-4-31b-it:free</code>
· <code>openai/gpt-oss-20b:free</code>

Список: openrouter.ai/models"""


@router.message(Command("model"))
async def cmd_model(message: Message, st: Storage, publisher: Publisher) -> None:
    source = "задана командой" if st.get("model") else "из .env"
    await _reply(
        message,
        f"Текущая модель: <code>{_e(publisher.llm.model)}</code> ({source})\n"
        f"Сменить: <code>/setmodel &lt;название&gt;</code>\n\n{MODEL_HINTS}",
    )


@router.message(Command("setmodel"))
async def cmd_setmodel(message: Message, command: CommandObject, st: Storage,
                       publisher: Publisher) -> None:
    name = (command.args or "").strip()
    if not name:
        await _reply(message, f"Как использовать: <code>/setmodel deepseek/deepseek-v4-flash</code>\n"
                              f"Сейчас: <code>{_e(publisher.llm.model)}</code>\n\n{MODEL_HINTS}")
        return
    if " " in name:
        await _reply(message, "Название модели без пробелов, например "
                              "<code>deepseek/deepseek-v4-flash</code>")
        return

    previous = publisher.llm.model
    publisher.llm.model = name
    try:
        await publisher.llm.complete("Ответь одним словом: работает")
    except LLMError as exc:
        publisher.llm.model = previous
        await _reply(message, f"❌ Модель не ответила: <code>{_e(exc)}</code>\n"
                              f"Оставил прежнюю: <code>{_e(previous)}</code>")
        return

    st.set("model", name)
    await _reply(message, f"✅ Модель: <code>{_e(name)}</code> (проверена живым запросом)")


@router.message(Command("checknow"))
async def cmd_checknow(message: Message, st: Storage, publisher: Publisher) -> None:
    if not st.feeds(only_enabled=True):
        await _reply(message, "Нет активных лент.")
        return
    debug = publisher.debug
    await _reply(message, "🔧 Отладка: собираю посты в личку…" if debug
                 else "🔄 Проверяю ленты…")
    stats = await publisher.run_once(manual=True)
    verb = "показано в личке" if debug else "опубликовано"
    tail = f", ошибок: {stats['errors']}" if stats["errors"] else ""
    if publisher.vk_on and not debug:
        tail += f", в VK: {stats['vk']}"
    if stats.get("postponed"):
        tail += f", отложено: {stats['postponed']}"
    hint = ""
    if stats.get("postponed"):
        hint = ("\n\n⏳ Отложенные новости модель не смогла обработать. Они не "
                "потеряны и уйдут в канал со следующей попыткой — подробности "
                "в журнале. Публиковать такие без обработки: "
                "<code>/set on_llm_error raw</code>.")
    if debug and not stats["published"]:
        hint = ("\n\nНовых новостей нет. Отладка показывает только непрочитанные — "
                "чтобы посмотреть на конкретной новости, используйте "
                "<code>/test &lt;id&gt;</code>.")
    await _reply(message, f"Готово. Лент: {stats['feeds']}, {verb}: "
                          f"{stats['published']}{tail}{hint}")


def _post_kind_label(kind: str) -> str:
    return {"text": "текст", "photo": "фото", "album": "альбом"}.get(kind, kind)


@router.message(Command("posts"))
async def cmd_posts(message: Message, command: CommandObject, st: Storage) -> None:
    n = _parse_id(command.args) or 10
    n = max(1, min(30, n))
    rows = st.posts(n)
    if not rows:
        await _reply(message, "Опубликованных постов пока нет.")
        return
    lines = [f"<b>Последние посты</b> (до {n})"]
    for r in rows:
        when = time.strftime("%d.%m %H:%M", time.localtime(r["posted_at"]))
        edited = " ✏️" if r["edited_at"] else ""
        lines.append(
            f"\n<b>#{r['id']}</b> [{_post_kind_label(r['kind'])}]{edited} {when}\n"
            f"   {_e(r['title'][:100])}"
        )
    lines.append("\nПодробнее и редактировать: <code>/edit &lt;id&gt;</code>")
    await _reply(message, "\n".join(lines))


@router.message(Command("edit"))
async def cmd_edit(message: Message, command: CommandObject, st: Storage) -> None:
    post_id = _parse_id(command.args)
    row = st.post(post_id) if post_id is not None else None
    if row is None:
        await _reply(message, "Как использовать: <code>/edit 1</code> "
                              "(id смотрите в /posts)")
        return
    edited = (f"\nОтредактирован: {time.strftime('%d.%m %H:%M', time.localtime(row['edited_at']))}"
             if row["edited_at"] else "")
    images_hint = ""
    extra = st.post_extra_ids(post_id) if row["kind"] == "album" else []
    if extra:
        nums = ", ".join(str(n) for n in range(2, len(extra) + 2))
        images_hint = (f"\nКартинок в альбоме: {len(extra) + 1} (№1 — с текстом поста, "
                       f"не удаляется). Удалить одну из остальных (№{nums}): "
                       f"<code>/delimage {row['id']} 2</code>\n")
    await _reply(
        message,
        f"<b>Пост #{row['id']}</b> [{_post_kind_label(row['kind'])}]\n"
        f"Опубликован: {time.strftime('%d.%m %H:%M', time.localtime(row['posted_at']))}"
        f"{edited}{images_hint}\n"
        f"Источник: {_e(row['title'])}\n"
        f"<a href=\"{_e(row['link'])}\">ссылка на новость</a>\n\n"
        f"<b>Текущий текст в канале:</b>\n<pre>{_e(row['text'])}</pre>\n\n"
        f"Изменить текст вручную: <code>/setpost {row['id']} новый текст</code>\n"
        f"Перегенерировать через ИИ из исходной новости: <code>/regen {row['id']}</code> "
        f"(можно добавить пожелание: <code>/regen {row['id']} короче и без хештегов</code>)",
    )


@router.message(Command("delimage"))
async def cmd_delimage(message: Message, command: CommandObject, st: Storage,
                       publisher: Publisher) -> None:
    parts = (command.args or "").split()
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        await _reply(message, "Как использовать: <code>/delimage 42 3</code> — "
                              "пост #42, картинка №3 (номера — из <code>/edit 42</code>)")
        return
    post_id, n = int(parts[0]), int(parts[1])
    row = st.post(post_id)
    if row is None:
        await _reply(message, "Пост не найден.")
        return
    extra = st.post_extra_ids(post_id)
    if row["kind"] != "album" or not extra:
        await _reply(message, "В этом посте нет дополнительных картинок для удаления.")
        return
    if n < 2 or n - 2 >= len(extra):
        await _reply(message, f"Такой картинки нет — доступны номера 2-{len(extra) + 1}.")
        return
    error = await publisher.delete_post_image(post_id, extra[n - 2])
    if error:
        await _reply(message, f"❌ {error}")
        return
    await _reply(message, f"✅ Картинка №{n} удалена из поста #{post_id}.")


def _split_id_and_text(args: str | None) -> tuple[int | None, str]:
    """"<id> <текст...>" → (id, текст). Разделитель — любой пробельный символ,
    в т.ч. перенос строки (Shift+Enter сразу после id)."""
    parts = (args or "").split(maxsplit=1)
    post_id = _parse_id(parts[0]) if parts else None
    text = parts[1].strip() if len(parts) > 1 else ""
    return post_id, text


@router.message(Command("setpost"))
async def cmd_setpost(message: Message, command: CommandObject, st: Storage, bot: Bot) -> None:
    post_id, text = _split_id_and_text(command.args)
    row = st.post(post_id) if post_id is not None else None
    if row is None or not text.strip():
        await _reply(message, "Как использовать: <code>/setpost 1 новый текст поста</code> "
                              "(id смотрите в /posts)")
        return

    limit = TG_CAPTION_LIMIT if row["kind"] in ("photo", "album") else TG_LIMIT
    if tg_len(text) > limit:
        await _reply(message, f"⚠️ Текст длиннее лимита Telegram для этого поста "
                              f"({tg_len(text)} из {limit} — это "
                              + ("подпись к фото" if row["kind"] in ("photo", "album")
                                 else "сообщение") + "). Сократите и попробуйте снова.")
        return
    problem = html_problem(text)
    if problem:
        await _reply(message, f"⚠️ Разметка не годится: {problem}. Не сохранено.")
        return

    if not await _apply_post_edit(message, bot, st, row, text):
        return
    await _reply(message, f"✅ Пост #{row['id']} обновлён.")


@router.message(Command("regen"))
async def cmd_regen(message: Message, command: CommandObject, st: Storage, bot: Bot,
                    publisher: Publisher) -> None:
    post_id, extra = _split_id_and_text(command.args)
    row = st.post(post_id) if post_id is not None else None
    if row is None:
        await _reply(message, "Как использовать: <code>/regen 1</code> или "
                              "<code>/regen 1 сделай короче</code> (id из /posts)")
        return

    await _reply(message, f"Перегенерирую через {_e(publisher.active_backend_label)} из исходной новости…")
    try:
        text = await publisher.rebuild_post_text(row, extra)
    except LLMError as exc:
        await _reply(message, f"❌ Модель вернула ошибку: <code>{_e(exc)}</code>\n"
                              f"Текст поста не менялся.")
        return

    if not await _apply_post_edit(message, bot, st, row, text):
        return
    await _reply(message, f"✅ Пост #{row['id']} обновлён через {_e(publisher.active_backend_label)}.\n\n"
                          f"<b>Новый текст:</b>\n<pre>{_e(text)}</pre>")


async def _apply_post_edit(message: Message, bot: Bot, st: Storage,
                           row, text: str) -> bool:
    """Редактирует само сообщение в канале и обновляет запись в базе.
    True — получилось, False — Telegram отказал (текст не сохранён)."""
    try:
        if row["kind"] in ("photo", "album"):
            await bot.edit_message_caption(chat_id=row["chat_id"], message_id=row["message_id"],
                                           caption=text, parse_mode="HTML")
        else:
            await bot.edit_message_text(chat_id=row["chat_id"], message_id=row["message_id"],
                                        text=text, parse_mode="HTML",
                                        link_preview_options=LinkPreviewOptions(is_disabled=True))
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            await _reply(message, "Текст не изменился — Telegram отклонил правку "
                                  "как пустую. В базе тоже не трогал.")
            return False
        await _reply(message, f"❌ Telegram отказал в правке: <code>{_e(exc)}</code>\n"
                              f"Частые причины: пост старше ~48 часов, бот больше не "
                              f"админ канала, или сообщение было удалено вручную.")
        return False
    except TelegramAPIError as exc:
        await _reply(message, f"❌ Ошибка Telegram: <code>{_e(exc)}</code>")
        return False

    st.update_post_text(row["id"], text)
    return True


@router.message(Command("debug"))
async def cmd_debug(message: Message, command: CommandObject, st: Storage,
                    publisher: Publisher) -> None:
    arg = (command.args or "").strip().lower()
    if arg in ("on", "вкл", "1"):
        st.set("debug", "1")
        await _reply(
            message,
            "🔧 <b>Режим отладки включён.</b>\n\n"
            "· посты приходят сюда, в личку, а не в канал\n"
            "· новости <b>не</b> помечаются прочитанными — после выключения "
            "они уйдут в канал как обычно\n"
            "· автоматический цикл в отладке молчит, проверка — по "
            "<code>/checknow</code> (можно гонять одну и ту же новость, "
            "меняя <code>/settemplate</code>)\n\n"
            "Выключить: <code>/debug off</code>",
        )
        return
    if arg in ("off", "выкл", "0"):
        st.set("debug", "0")
        await _reply(message, "✅ Режим отладки выключен, публикую в канал: "
                             f"<code>{_e(publisher.channel or 'не задан')}</code>")
        return
    state = "включён 🔧" if publisher.debug else "выключен"
    await _reply(message, f"Режим отладки {state}.\n"
                          f"Как использовать: <code>/debug on</code> · "
                          f"<code>/debug off</code>")


@router.message(Command("feedimages"))
async def cmd_feedimages(message: Message, command: CommandObject, st: Storage) -> None:
    """Переключатель «одна картинка, как раньше» / «несколько картинок
    альбомом» — свойство отдельной ленты (feeds.multi_images), не общая
    настройка: разные источники по-разному годятся для альбома (см.
    Storage.set_multi_images)."""
    parts = (command.args or "").split(maxsplit=1)
    feed_id = int(parts[0]) if parts and parts[0].isdigit() else None
    feed = st.feed(feed_id) if feed_id is not None else None
    if feed is None:
        await _reply(message, "Как использовать: <code>/feedimages 1</code> (id ленты из /list)\n"
                              "Включить: <code>/feedimages 1 on</code> · выключить: <code>/feedimages 1 off</code>")
        return
    name = feed["title"] or feed["url"]
    arg = parts[1].strip().lower() if len(parts) > 1 else ""
    if arg in ("on", "вкл", "1"):
        st.set_multi_images(feed_id, True)
        await _reply(
            message,
            f"🖼 <b>Несколько картинок для «{_e(name)}» включено.</b>\n\n"
            f"Вместо одной бот качает со страницы новости до "
            f"{_e(st.get('max_images'))} картинок и публикует альбомом.\n\n"
            f"Сколько картинок: <code>/set max_images N</code> (1-10, общее для всех лент)\n"
            f"Вернуть одну картинку: <code>/feedimages {feed_id} off</code>",
        )
        return
    if arg in ("off", "выкл", "0"):
        st.set_multi_images(feed_id, False)
        await _reply(message, f"✅ У «{_e(name)}» — одна картинка на пост, как раньше. "
                              f"Включить снова — <code>/feedimages {feed_id} on</code>")
        return
    state = "включено 🖼" if feed["multi_images"] else "выключено (одна картинка)"
    await _reply(
        message,
        f"Несколько картинок для «{_e(name)}»: {state}\n"
        f"Штук за раз: <code>{_e(st.get('max_images'))}</code> (общее для всех лент)\n\n"
        f"<code>/feedimages {feed_id} on</code> · <code>/feedimages {feed_id} off</code> — переключить\n"
        f"<code>/set max_images N</code> — сколько картинок (1-10)",
    )


VK_HELP = """<b>Публикация в VK</b>

Посты дублируются на стену сообщества после публикации в Telegram.
Разметка убирается (VK её не понимает), ссылки разворачиваются в текст.

<b>Что нужно</b>
1. В сообществе: Управление → Работа с API → Создать ключ.
   Права: <b>Стена</b> и <b>Фотографии</b>.
2. Числовой id сообщества (не короткое имя): «Ещё» → «Статистика» в адресе,
   либо regvk.com/id
3. В <code>.env</code>: <code>VK_TOKEN=vk1.a...</code> и <code>VK_GROUP_ID=123456789</code>, затем перезапуск.

<b>Про картинку</b>
Загружать фото на стену VK разрешает <b>только пользовательскому ключу</b>.
Без него бот прикрепляет к записи ссылку на новость, и картинку подбирает сам
VK со страницы источника — иногда её нет вовсе.

Чтобы картинка была всегда, нужен <code>VK_USER_TOKEN</code>. Своё приложение создавать
не обязательно (для этого нужно юрлицо) — подойдёт id уже существующего:

1. Откройте в браузере под аккаунтом администратора сообщества:
<code>https://oauth.vk.com/authorize?client_id=6121396&amp;scope=photos,wall,groups,offline&amp;response_type=token&amp;redirect_uri=https://oauth.vk.com/blank.html&amp;display=page</code>
2. Разрешите доступ — откроется пустая страница.
3. Из адресной строки скопируйте всё между <code>access_token=</code> и <code>&amp;expires_in</code>.
4. Добавьте в <code>.env</code> строкой <code>VK_USER_TOKEN=...</code> и перезапустите бота.

Ключ бессрочный, хранить его надо как пароль. Публикует по-прежнему
сообщество — пользовательский ключ идёт только на загрузку фото.

<b>Команды</b>
/vk — это сообщение и состояние
/vk on · /vk off — включить и выключить дублирование
/vk group &lt;id&gt; — сменить сообщество без правки .env
/vk check — проверить ключи (запрос к VK, ничего не публикует)
/vk test &lt;id ленты&gt; — опубликовать последнюю новость ленты в VK"""


@router.message(Command("vk"))
async def cmd_vk(message: Message, command: CommandObject, st: Storage,
                 publisher: Publisher) -> None:
    args = (command.args or "").split()
    action = args[0].lower() if args else ""
    vk = publisher.vk

    if action in ("on", "вкл", "1"):
        st.set("vk_enabled", "1")
        if not publisher.vk_on:
            await _reply(message, "Включил, но публиковать пока не выйдет: "
                                  "не хватает VK_TOKEN или id сообщества.\n\n" + VK_HELP)
            return
        await _reply(message, f"✅ Дублирую в VK, сообщество "
                              f"<code>{_e(publisher.vk_group)}</code>")
        return

    if action in ("off", "выкл", "0"):
        st.set("vk_enabled", "0")
        await _reply(message, "⏸ В VK больше не публикую. Вернуть — <code>/vk on</code>")
        return

    if action == "group":
        value = args[1].lstrip("-") if len(args) > 1 else ""
        if not value.isdigit():
            await _reply(message, "Как использовать: <code>/vk group 123456789</code> — "
                                  "числовой id сообщества, не короткое имя.")
            return
        st.set("vk_group_id", value)
        await _reply(message, f"✅ Сообщество VK: <code>{value}</code>. "
                              f"Проверить: <code>/vk check</code>")
        return

    if action == "check":
        if vk is None or not vk.token:
            await _reply(message, "VK_TOKEN не задан.\n\n" + VK_HELP)
            return
        if not publisher.vk_group.isdigit():
            await _reply(message, "Не задан id сообщества: "
                                  "<code>/vk group 123456789</code>")
            return
        vk.group_id = publisher.vk_group
        await _reply(message, "Спрашиваю VK…")
        try:
            name = await vk.group_name()
        except VKError as exc:
            await _reply(message, f"❌ VK ответил ошибкой: <code>{_e(exc)}</code>\n\n"
                                  f"Чаще всего это чужой или отозванный ключ, "
                                  f"либо у ключа нет прав «Стена» и «Фотографии».")
            return
        lines = [f"✅ Ключ сообщества рабочий: <b>{_e(name)}</b> "
                 f"(<code>{_e(publisher.vk_group)}</code>)",
                 f"Дублирование сейчас "
                 f"{'включено' if publisher.vk_on else 'выключено'}."]
        if vk.user_token:
            # Тот же запрос, что и при публикации фото, но без самой загрузки.
            try:
                await vk._call("photos.getWallUploadServer",
                               _token=vk.user_token, group_id=publisher.vk_group)
                lines.append("🖼 Пользовательский ключ рабочий — картинки "
                             "уходят настоящим фото.")
            except VKError as exc:
                lines.append(f"⚠️ Пользовательский ключ не работает: "
                             f"<code>{_e(exc)}</code>\nКартинки пойдут "
                             f"карточкой-ссылкой.")
        else:
            lines.append("🖼 <code>VK_USER_TOKEN</code> не задан — вместо фото "
                         "карточка-ссылка, картинку выбирает сам VK. "
                         "Как это исправить — /vk")
        await _reply(message, "\n".join(lines))
        return

    if action == "test":
        feed_id = _parse_id(" ".join(args[1:]))
        feed = st.feed(feed_id) if feed_id is not None else None
        if feed is None:
            await _reply(message, "Как использовать: <code>/vk test 1</code> "
                                  "(id ленты из /list)")
            return
        if vk is None or not publisher.vk_group.isdigit() or not vk.token:
            await _reply(message, "Сначала настройте доступ.\n\n" + VK_HELP)
            return
        await _reply(message, "Беру последнюю новость и публикую её в VK…")
        result = await fetch(feed["url"])
        if result.error or not result.entries:
            await _reply(message, f"❌ {_e(result.error or 'в ленте нет записей')}")
            return
        try:
            post = await publisher.build_post(result.entries[-1], feed)
        except LLMError as exc:
            await _reply(message, f"❌ Модель вернула ошибку: <code>{_e(exc)}</code>")
            return
        vk.group_id = publisher.vk_group
        try:
            post_id = await vk.post(to_plain(post.text), post.image, post.link,
                                    images=post.images)
        except VKError as exc:
            await _reply(message, f"❌ VK не принял пост: <code>{_e(exc)}</code>")
            return
        link = f"https://vk.com/wall-{publisher.vk_group}_{post_id}" if post_id else ""
        await _reply(message, "✅ Опубликовано в VK"
                     + (f": {link}" if link else "")
                     + "\n\nЭто настоящая запись на стене — если она не нужна, удалите её.")
        return

    state = "включено ✅" if publisher.vk_on else "выключено"
    token = "задан" if vk and vk.token else "❌ не задан"
    group = publisher.vk_group or "не задан"
    picture = (vk.photo_mode if vk and vk.can_upload_photo
               else "карточка-ссылка — VK возьмёт картинку со страницы источника")
    await _reply(
        message,
        f"Дублирование в VK: {state}\n"
        f"Ключ сообщества: {token}\nСообщество: <code>{_e(group)}</code>\n"
        f"Картинка: {picture}\n\n" + VK_HELP,
    )


CLAUDE_HELP = """<b>Режим Claude</b>

Платно, вместо обычного режима.

<b>Нужно</b>: <code>CLAUDE_API_KEY=sk-ant-...</code> в <code>.env</code>,
при желании <code>CLAUDE_MODEL</code> (по умолчанию
<code>claude-sonnet-5</code>), затем перезапуск бота.

Промпт и формат поста — общие (/template, /format). В /usage не
считается — свой отдельный платный счёт. Несколько картинок альбомом
вместо одной — настройка отдельной ленты, не завязана на Claude: /feedimages

<b>Команды</b>
/claude — статус
/claude on · off — включить/выключить
/claude check — проверить ключ
/claude test &lt;id&gt; — прогнать новость ленты, показать в личке"""


@router.message(Command("claude"))
async def cmd_claude(message: Message, command: CommandObject, st: Storage,
                     publisher: Publisher) -> None:
    args = (command.args or "").split()
    action = args[0].lower() if args else ""
    claude = publisher.claude

    if action in ("on", "вкл", "1"):
        st.set("claude_mode", "1")
        st.set("gemini_mode", "0")  # одновременно работать может только один альтернативный бэкенд
        if not publisher.claude_mode:
            await _reply(message, "Включил, но работать пока не выйдет: "
                                  "не хватает CLAUDE_API_KEY в .env.\n\n" + CLAUDE_HELP)
            return
        await _reply(message, f"✅ Обрабатываю через Claude (<code>{_e(claude.model)}</code>).")
        return

    if action in ("off", "выкл", "0"):
        st.set("claude_mode", "0")
        await _reply(message, "⏸ Вернул обычный режим "
                              f"(<code>{_e(publisher.llm.model)}</code>). "
                              "Включить снова — <code>/claude on</code>")
        return

    if action == "check":
        if claude is None or not claude.api_key:
            await _reply(message, "CLAUDE_API_KEY не задан.\n\n" + CLAUDE_HELP)
            return
        await _reply(message, "Спрашиваю Claude…")
        try:
            reply = await claude.complete("Ответь одним словом: работает")
        except LLMError as exc:
            await _reply(message, f"❌ Claude ответил ошибкой: <code>{_e(exc)}</code>\n\n"
                                  f"Чаще всего дело в неверном ключе или названии модели "
                                  f"(<code>CLAUDE_MODEL</code>).")
            return
        await _reply(message, f"✅ Ключ рабочий, модель <code>{_e(claude.model)}</code> "
                              f"ответила: «{_e(reply[:200])}»")
        return

    if action == "test":
        feed_id = _parse_id(" ".join(args[1:]))
        feed = st.feed(feed_id) if feed_id is not None else None
        if feed is None:
            await _reply(message, "Как использовать: <code>/claude test 1</code> "
                                  "(id ленты из /list)")
            return
        if claude is None or not claude.api_key:
            await _reply(message, "CLAUDE_API_KEY не задан.\n\n" + CLAUDE_HELP)
            return
        images_note = " (качаю картинки со страницы — это дольше обычного)" if feed["multi_images"] else ""
        await _reply(message, f"Забираю ленту и прогоняю через Claude{images_note}…")
        result = await fetch(feed["url"])
        if result.error or not result.entries:
            await _reply(message, f"❌ {_e(result.error or 'в ленте нет записей')}")
            return

        entry = result.entries[-1]
        was_on = st.get("claude_mode")
        st.set("claude_mode", "1")   # build_post смотрит на это состояние
        try:
            post = await publisher.build_post(entry, feed)
        except LLMError as exc:
            await _reply(message, f"❌ Claude вернул ошибку: <code>{_e(exc)}</code>")
            return
        finally:
            st.set("claude_mode", was_on)

        picture = f"картинок: {len(post.images)}" if post.images else "без картинок"
        await _reply(message, f"⬇️ Предпросмотр через Claude ({picture}, "
                              f"<b>не</b> опубликовано)")
        if not await _preview_post(message, post):
            await message.answer(post.text, parse_mode="HTML",
                                 link_preview_options=NO_PREVIEW)
        return

    state = "включён ✅" if publisher.claude_mode else "выключен"
    key = "задан" if claude and claude.api_key else "❌ не задан"
    model = claude.model if claude else "—"
    await _reply(
        message,
        f"Режим Claude: {state}\nКлюч: {key}\nМодель: <code>{_e(model)}</code>\n\n" + CLAUDE_HELP,
    )


GEMINI_HELP = """<b>Режим Gemini</b>

Вместо обычного режима, обычно бесплатно (свой тариф Google, не через
OpenRouter). Взаимоисключимо с Claude: включение одного гасит другой.

<b>Нужно</b>: <code>GEMINI_API_KEY=...</code> (Google AI Studio) в
<code>.env</code>, при желании <code>GEMINI_MODEL</code> (по умолчанию
<code>gemini-3.6-flash</code>), затем перезапуск бота.

Промпт и формат поста — общие (/template, /format). В /usage не
считается — свои лимиты смотрите в Google AI Studio.

<b>Команды</b>
/gemini — статус
/gemini on · off — включить/выключить
/gemini check — проверить ключ
/gemini test &lt;id&gt; — прогнать новость ленты, показать в личке"""


@router.message(Command("gemini"))
async def cmd_gemini(message: Message, command: CommandObject, st: Storage,
                     publisher: Publisher) -> None:
    args = (command.args or "").split()
    action = args[0].lower() if args else ""
    gemini = publisher.gemini

    if action in ("on", "вкл", "1"):
        st.set("gemini_mode", "1")
        st.set("claude_mode", "0")
        if not publisher.gemini_mode:
            await _reply(message, "Включил, но работать пока не выйдет: "
                                  "не хватает GEMINI_API_KEY в .env.\n\n" + GEMINI_HELP)
            return
        await _reply(message, f"✅ Обрабатываю через Gemini (<code>{_e(gemini.model)}</code>).")
        return

    if action in ("off", "выкл", "0"):
        st.set("gemini_mode", "0")
        await _reply(message, "⏸ Вернул обычный режим "
                              f"(<code>{_e(publisher.llm.model)}</code>). "
                              "Включить снова — <code>/gemini on</code>")
        return

    if action == "check":
        if gemini is None or not gemini.api_key:
            await _reply(message, "GEMINI_API_KEY не задан.\n\n" + GEMINI_HELP)
            return
        await _reply(message, "Спрашиваю Gemini…")
        try:
            reply = await gemini.complete("Ответь одним словом: работает")
        except LLMError as exc:
            await _reply(message, f"❌ Gemini ответил ошибкой: <code>{_e(exc)}</code>\n\n"
                                  f"Чаще всего дело в неверном ключе или названии модели "
                                  f"(<code>GEMINI_MODEL</code>).")
            return
        await _reply(message, f"✅ Ключ рабочий, модель <code>{_e(gemini.model)}</code> "
                              f"ответила: «{_e(reply[:200])}»")
        return

    if action == "test":
        feed_id = _parse_id(" ".join(args[1:]))
        feed = st.feed(feed_id) if feed_id is not None else None
        if feed is None:
            await _reply(message, "Как использовать: <code>/gemini test 1</code> "
                                  "(id ленты из /list)")
            return
        if gemini is None or not gemini.api_key:
            await _reply(message, "GEMINI_API_KEY не задан.\n\n" + GEMINI_HELP)
            return
        await _reply(message, "Забираю ленту и прогоняю через Gemini…")
        result = await fetch(feed["url"])
        if result.error or not result.entries:
            await _reply(message, f"❌ {_e(result.error or 'в ленте нет записей')}")
            return

        entry = result.entries[-1]
        was_on = st.get("gemini_mode")
        st.set("gemini_mode", "1")   # build_post смотрит на это состояние
        try:
            post = await publisher.build_post(entry, feed)
        except LLMError as exc:
            await _reply(message, f"❌ Gemini вернул ошибку: <code>{_e(exc)}</code>")
            return
        finally:
            st.set("gemini_mode", was_on)

        picture = f"картинок: {len(post.images)}" if post.images else "без картинок"
        await _reply(message, f"⬇️ Предпросмотр через Gemini ({picture}, "
                              f"<b>не</b> опубликовано)")
        if not await _preview_post(message, post):
            await message.answer(post.text, parse_mode="HTML",
                                 link_preview_options=NO_PREVIEW)
        return

    state = "включён ✅" if publisher.gemini_mode else "выключен"
    key = "задан" if gemini and gemini.api_key else "❌ не задан"
    model = gemini.model if gemini else "—"
    await _reply(
        message,
        f"Режим Gemini: {state}\nКлюч: {key}\nМодель: <code>{_e(model)}</code>\n\n" + GEMINI_HELP,
    )


@router.message(Command("usage"))
async def cmd_usage(message: Message, st: Storage, publisher: Publisher) -> None:
    if publisher.quota is None:
        await _reply(message, "Учёт расхода недоступен.")
        return
    await _reply(message, "Запрашиваю сведения о лимите…")
    info = await publisher.quota.snapshot(force=True)

    lines = [
        f"<b>Расход за {info.day}</b> (сутки UTC)",
        f"Запросов: {info.requests}"
        + (f" из {info.request_limit} — <b>{info.request_pct:.0f}%</b>"
           if info.request_limit else ""),
        f"Токенов: {info.tokens_in} вход / {info.tokens_out} выход",
    ]
    if info.request_limit:
        lines.append(f"Источник лимита: {info.limit_source}")
        lines.append(f"Обнуление через {until_reset()} (00:00 UTC)")
    if info.cost:
        lines.append(f"Стоимость запросов: {info.cost:.4f} кредита")

    lines.append("")
    lines.append(f"Модель: <code>{_e(info.model)}</code>"
                 + (" (бесплатная)" if info.is_free_model else ""))
    if info.credit_limit is not None:
        lines.append(f"Лимит кредитов на ключе: {info.credit_limit:.4f}, "
                     f"осталось {info.credit_remaining:.4f}"
                     + (f" — <b>{info.credit_pct:.0f}%</b> израсходовано"
                        if info.credit_pct is not None else ""))
    elif info.credits_used_total is not None:
        lines.append(f"Лимит трат на ключе не задан; всего израсходовано "
                     f"{info.credits_used_total:.4f} кредита")
    if info.credits_used_today is not None:
        lines.append(f"Кредитов за сутки: {info.credits_used_today:.4f}")
    if info.is_free_tier is not None:
        lines.append("Кредиты покупались: " + ("нет" if info.is_free_tier else "да"))

    thresholds = publisher.quota.thresholds()
    lines.append("")
    lines.append("Предупреждения при: "
                 + (", ".join(f"{t}%" for t in thresholds) if thresholds else "отключены")
                 + " — <code>/set alert_thresholds 70,90</code>")
    if not info.request_limit:
        lines.append("Суточный лимит запросов можно задать вручную: "
                     "<code>/set free_daily_limit 50</code>")
    await _reply(message, "\n".join(lines))


@router.message(Command("duplicates"))
async def cmd_duplicates(message: Message, st: Storage) -> None:
    """Список читать можно и тут, а смотреть картинки, публиковать или
    удалять — удобнее в веб-панели, раздел «Ленты», там же вся карточка."""
    rows = st.dedup_candidates(10)
    if not rows:
        await _reply(message, "Дублей на разбор нет.")
        return
    lines = [f"<b>Похожие на дубли</b> ({st.count_dedup_candidates()})", ""]
    for r in rows:
        lines.append(f"· {_e(r['title'][:80])}\n"
                     f"  {_e(r['source'] or 'без ленты')} · похоже на пост #{r['matched_post_id']} "
                     f"({r['score']:.0%})")
    lines.append("\nПосмотреть картинки, опубликовать или удалить — в веб-панели, раздел «Ленты».")
    await _reply(message, "\n".join(lines))


@router.message(Command("status"))
async def cmd_status(message: Message, st: Storage, publisher: Publisher) -> None:
    feeds = st.feeds()
    active = sum(1 for f in feeds if f["enabled"])
    errors = [f for f in feeds if f["last_error"]]
    paused = st.get("paused") == "1"
    llm = publisher.llm
    keys = ("interval", "max_per_cycle", "post_delay", "backfill",
            "max_age_days", "flood_guard", "on_llm_error",
            "require_russian", "disable_preview", "images", "og_image",
            "max_images",
            "keep_seen", "alert_thresholds", "free_daily_limit",
            "dedup_enabled", "dedup_window_days", "dedup_threshold")
    dupes = st.count_dedup_candidates()
    multi_feeds = sum(1 for f in feeds if f["multi_images"])
    mode = "⏸ на паузе" if paused else "▶️ работает"
    if publisher.debug:
        mode = "🔧 отладка — посты в личку, /debug off чтобы публиковать"
    claude = publisher.claude
    gemini = publisher.gemini
    await _reply(
        message,
        f"<b>Состояние</b>\n"
        f"Публикация: {mode}\n"
        f"Сейчас обрабатывает: <code>{_e(publisher.active_backend_label)}</code>\n"
        f"Канал: <code>{_e(publisher.channel or 'не задан')}</code>\n"
        f"VK: " + (f"сообщество <code>{_e(publisher.vk_group)}</code>"
                   if publisher.vk_on else "выключен") + "\n"
        f"Claude: " + (f"включён, <code>{_e(claude.model)}</code>"
                       if publisher.claude_mode else "выключен") + "\n"
        f"Gemini: " + (f"включён, <code>{_e(gemini.model)}</code>"
                       if publisher.gemini_mode else "выключен") + "\n"
        f"Ленты: {active} активных из {len(feeds)}"
        + (f", с ошибками: {len(errors)}" if errors else "") + "\n"
        + (f"Несколько картинок: у {multi_feeds} из {len(feeds)} лент — /feedimages <id>\n"
           if multi_feeds else "")
        + (f"Дублей на разбор: {dupes} — /duplicates или веб-панель\n" if dupes else "")
        + f"Модель: <code>{_e(llm.model)}</code>\n"
        f"Endpoint: <code>{_e(llm.endpoint)}</code>\n"
        f"Ключ LLM: {'задан' if llm.api_key else '❌ не задан'}\n\n"
        + "\n".join(f"· <code>{k}</code> = {_e(st.get(k))}" for k in keys),
    )


@router.message(F.text.startswith("/"))
async def cmd_unknown(message: Message) -> None:
    await _reply(message, "Не знаю такой команды. Список — /help")


def _parse_id(args: str | None) -> int | None:
    try:
        return int((args or "").split()[0])
    except (ValueError, IndexError):
        return None


def _tail(text: str | None) -> str:
    """Всё после первой команды, включая переносы строк."""
    if not text:
        return ""
    _, _, rest = text.partition(" ")
    if not rest:
        _, nl, after = text.partition("\n")
        rest = after if nl else ""
    return rest.strip()
