"""Опрос лент, обработка через LLM и публикация в канал."""
from __future__ import annotations

import asyncio
import ctypes
import html
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.exceptions import (TelegramAPIError, TelegramBadRequest,
                                TelegramRetryAfter)
from aiogram.types import (BufferedInputFile, InputMediaPhoto,
                            LinkPreviewOptions)

from .claude import ClaudeClient
from .db import Storage, entry_key
from .llm import LLMClient, LLMError
from .quota import Quota
from .rss import Entry, download_image, fetch, page_image, page_images, strip_html
from .vk import VKClient, VKError, to_plain

log = logging.getLogger(__name__)

TG_LIMIT = 4096
TG_CAPTION_LIMIT = 1024   # подпись к фото Telegram обрезает жёстче текста
ELLIPSIS = "…"
MAINTENANCE_EVERY = 6 * 3600   # уборка в базе, секунды
ALERT_EVERY = 3600             # как часто напоминать об отложенных новостях
RU_ATTEMPTS = 2                # попыток добиться от модели русского текста


def release_memory() -> None:
    """Отдаёт системе память, освобождённую после разбора лент.

    Разбор ленты идёт в отдельном потоке, и glibc держит его арену за
    процессом: RSS остаётся на пике, хотя данные давно не нужны.
    """
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass          # не glibc — не беда, просто пропускаем

# Теги, которые понимает Telegram в parse_mode=HTML.
TG_TAGS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre",
    "a", "blockquote", "span", "tg-spoiler", "tg-emoji", "br",
}
_TAG_RE = re.compile(r"<(/?)([a-zA-Z0-9-]+)([^>]*?)(/?)>")
_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def tg_len(text: str) -> int:
    """Telegram считает длину в кодовых единицах UTF-16: эмодзи весит 2."""
    return len(text.encode("utf-16-le")) // 2


_EXT_BY_CTYPE = {
    "image/jpeg": "jpg", "image/png": "png",
    "image/webp": "webp", "image/gif": "gif",
}


def _ext_for(ctype: str) -> str:
    return _EXT_BY_CTYPE.get((ctype or "").lower().split(";")[0].strip(), "jpg")


def open_tags(text: str) -> list[str]:
    """Теги, остающиеся незакрытыми в конце фрагмента."""
    stack: list[str] = []
    for m in _TAG_RE.finditer(text):
        closing, tag, selfclose = m.group(1), m.group(2).lower(), m.group(4)
        if tag == "br" or selfclose:
            continue
        if closing:
            if stack and stack[-1] == tag:
                stack.pop()
        else:
            stack.append(tag)
    return stack


def html_problem(text: str) -> str | None:
    """Проверяет разметку так же строго, как парсер Telegram.

    Нужна, чтобы не дать сохранить формат поста, из-за которого потом
    сломается вся публикация.
    """
    stack: list[str] = []
    for m in _TAG_RE.finditer(text):
        closing, tag, selfclose = m.group(1), m.group(2).lower(), m.group(4)
        if tag not in TG_TAGS:
            return f"Telegram не знает тег &lt;{html.escape(tag)}&gt;"
        if tag == "br" or selfclose:
            continue
        if closing:
            if not stack:
                return f"закрывающий &lt;/{tag}&gt; без открывающего"
            if stack[-1] != tag:
                return (f"&lt;/{tag}&gt; закрывает не тот тег — "
                        f"ожидался &lt;/{stack[-1]}&gt;")
            stack.pop()
        else:
            stack.append(tag)
    if stack:
        return "не закрыты теги: " + ", ".join(f"&lt;{t}&gt;" for t in stack)
    return None


def _safe_cut(text: str, budget: int) -> int:
    """Позиция разреза, не попадающая внутрь тега или HTML-сущности.

    Длину считаем в единицах UTF-16, поэтому позицию ищем бинарным поиском:
    арифметика «минус столько-то символов» на эмодзи промахивается вдвое.
    """
    budget = max(0, budget)
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if tg_len(text[:mid]) <= budget:
            lo = mid
        else:
            hi = mid - 1
    idx = lo
    lt, gt = text.rfind("<", 0, idx), text.rfind(">", 0, idx)
    if lt > gt:                      # разрез внутри тега
        idx = lt
    amp, semi = text.rfind("&", 0, idx), text.rfind(";", 0, idx)
    if amp > semi and idx - amp <= 10:   # разрез внутри сущности вроде &amp;
        idx = amp
    return idx


def _shorten(text: str, limit: int = TG_LIMIT) -> str:
    """Обрезает пост до лимита Telegram, не ломая разметку.

    Наивная обрезка отрывала закрывающий тег, Telegram отвечал 400, пост
    не уходил — и повторялся каждый проход, впустую расходуя лимит ИИ.
    """
    if tg_len(text) <= limit:
        return text
    reserve = 0
    for _ in range(4):
        cut = _safe_cut(text, limit - tg_len(ELLIPSIS) - reserve)
        closing = "".join(f"</{t}>" for t in reversed(open_tags(text[:cut])))
        if tg_len(closing) <= reserve:
            break
        reserve = tg_len(closing)
    return text[:cut].rstrip() + ELLIPSIS + closing


_CYRILLIC_RE = re.compile(r"[а-яёА-ЯЁ]")
_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)
MIN_CYRILLIC_SHARE = 0.3


def looks_russian(text: str) -> bool:
    """Похоже ли на русский текст.

    Грубая, но надёжная проверка: если модель промолчала о переводе и вернула
    исходник, кириллицы в ответе почти не будет. Названия и хештеги латиницей
    допустимы, поэтому порог низкий — треть букв.
    """
    letters = _LETTER_RE.findall(text)
    if len(letters) < 20:            # слишком короткий кусок, чтобы судить
        return True
    cyrillic = _CYRILLIC_RE.findall(text)
    return len(cyrillic) / len(letters) >= MIN_CYRILLIC_SHARE


def render(template: str, values: dict[str, str], escape: bool) -> str:
    """Подстановка {placeholder} за один проход.

    Один проход принципиален: при последовательных replace() значение из
    ленты, содержащее «{ai}», подменялось бы ответом модели.
    """
    def one(match: re.Match) -> str:
        key = match.group(1)
        if key not in values:
            return match.group(0)      # незнакомый ключ оставляем как есть
        value = values[key]
        value = "" if value is None else str(value)
        return html.escape(value) if escape else value

    return _PLACEHOLDER_RE.sub(one, template)


@dataclass(slots=True)
class Post:
    text: str
    image: str = ""
    # Несколько картинок (режим Claude) — уже скачанные байты, не ссылки:
    # Telegram лучше принимает файл, чем адрес за нестабильным CDN источника.
    images: list[tuple[bytes, str]] = field(default_factory=list)
    link: str = ""      # адрес новости — вложение для VK, если фото нечем грузить


class Publisher:
    def __init__(self, bot: Bot, storage: Storage, llm: LLMClient, default_channel: str,
                 admin_ids: set[int] | None = None, quota: "Quota | None" = None,
                 vk: "VKClient | None" = None, claude: "ClaudeClient | None" = None):
        self.bot = bot
        self.st = storage
        self.llm = llm
        self.claude = claude
        self.default_channel = default_channel
        self.admin_ids = set(admin_ids or ())
        self.quota = quota
        self.vk = vk
        self._wake = asyncio.Event()
        self._running = False
        # /checknow и фоновый цикл не должны опрашивать ленты одновременно —
        # иначе одна и та же новость успеет уйти в канал дважды.
        self._lock = asyncio.Lock()
        self._vk_posted = 0
        self._postponed: list[tuple[str, str]] = []
        self._postponed_flood: list[tuple[int, int]] = []
        # Первую уборку делаем не сразу после старта, а через MAINTENANCE_EVERY.
        self._last_maintenance = time.time()
        # Взводится, когда Telegram отказал по причине, которую повтором не
        # исправить (нет прав, канал не найден). Тогда дальше в этом проходе
        # посты не генерируем — иначе лимит модели уходит на недоставляемое.
        self._blocked = False

    # --- публичный API ---------------------------------------------------
    @property
    def channel(self) -> str:
        return self.st.get("channel_id") or self.default_channel

    @property
    def debug(self) -> bool:
        return self.st.get("debug") == "1"

    @property
    def claude_mode(self) -> bool:
        """Включён и есть чем работать — иначе тихо остаёмся на обычном LLM."""
        return (self.st.get("claude_mode") == "1"
                and self.claude is not None and bool(self.claude.api_key))

    @property
    def _active_llm(self) -> "LLMClient | ClaudeClient":
        return self.claude if self.claude_mode and self.claude else self.llm

    @property
    def vk_group(self) -> str:
        """id сообщества из команды важнее значения из .env."""
        return self.st.get("vk_group_id") or (self.vk.group_id if self.vk else "")

    @property
    def vk_on(self) -> bool:
        """VK включён, если есть чем и куда публиковать и его не выключили."""
        if self.vk is None or not self.vk.token or self.st.get("vk_enabled") == "0":
            return False
        return self.vk_group.isdigit()

    def wake(self) -> None:
        """Разбудить цикл, не дожидаясь интервала (для /checknow)."""
        self._wake.set()

    async def run_forever(self) -> None:
        self._running = True
        log.info("цикл опроса запущен")
        while self._running:
            try:
                await self.run_once()
            except Exception:
                log.exception("сбой в цикле опроса")
            self._housekeeping()
            interval = max(1, self.st.get_int("interval")) * 60
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=interval)
                log.info("внеплановая проверка лент")
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._running = False
        self._wake.set()

    def _housekeeping(self) -> None:
        """После прохода: вернуть память системе и раз в несколько часов
        подчистить базу. Обе операции дешёвые и делаются между проходами,
        чтобы не задерживать публикацию."""
        release_memory()
        now = time.time()
        if now - self._last_maintenance < MAINTENANCE_EVERY:
            return
        self._last_maintenance = now
        try:
            sizes = self.st.maintain(max(50, self.st.get_int("keep_seen")))
        except Exception:
            log.exception("уборка в базе не удалась")
            return
        log.info("уборка в базе: %.0f → %.0f КБ",
                 sizes["before"] / 1024, sizes["after"] / 1024)

    async def run_once(self, manual: bool = False) -> dict[str, int]:
        """Один проход по всем активным лентам. Возвращает сводку.

        `manual=True` — запуск командой /checknow. В режиме отладки
        автоматический проход публикацию не делает: новости не помечаются
        прочитанными, и одни и те же посты приходили бы в личку каждый цикл.
        """
        stats = {"feeds": 0, "published": 0, "errors": 0, "vk": 0,
                 "postponed": 0, "debug": int(self.debug)}
        self._vk_posted = 0
        self._postponed = []
        self._postponed_flood = []
        if self.st.get("paused") == "1":
            log.info("публикация на паузе — пропускаем проход")
            return stats
        if self.debug and not manual:
            log.info("режим отладки — автоматический проход пропущен, ждём /checknow")
            return stats

        async with self._lock:
            self._blocked = False
            for feed in self.st.feeds(only_enabled=True):
                stats["feeds"] += 1
                try:
                    stats["published"] += await self._process_feed(feed)
                except Exception:
                    stats["errors"] += 1
                    log.exception("лента #%s (%s) — необработанная ошибка",
                                  feed["id"], feed["url"])
                if self._blocked:
                    stats["blocked"] = 1
                    log.error("доставка невозможна — обрываю проход, чтобы не "
                              "тратить лимит модели впустую")
                    break

        stats["vk"] = self._vk_posted
        stats["postponed"] = len(self._postponed)
        if self._postponed:
            await self._report_postponed()
        if self._postponed_flood:
            await self._report_flood()
        if stats["published"] and self.quota:
            await self.quota.check_and_alert()
        return stats

    async def _report_flood(self) -> None:
        """Предупредить, что лента разом выдала слишком много «нового».

        Молчать нельзя: возможно, записи и правда новые, и тогда админ
        захочет поднять flood_guard, чтобы их не потерять.
        """
        if not self.admin_ids:
            return
        lines = ["⚠️ Лента разом выдала много новых записей — публикую только "
                 "свежие, остальное помечено прочитанным.", ""]
        for feed_id, dropped in self._postponed_flood:
            feed = self.st.feed(feed_id)
            title = (feed["title"] if feed else "") or f"лента #{feed_id}"
            lines.append(f"· {html.escape(title[:40])}: пропущено {dropped}")
        lines += ["", "Так бот защищается от лент, у которых сбилась выдача. "
                  "Если записи и правда новые — поднимите порог: "
                  "<code>/set flood_guard 40</code>."]
        text = "\n".join(lines)
        for admin_id in sorted(self.admin_ids):
            try:
                await self.bot.send_message(chat_id=admin_id, text=text,
                                            parse_mode="HTML")
            except TelegramAPIError as exc:
                log.warning("не удалось предупредить админа %s: %s", admin_id, exc)

    async def _report_postponed(self) -> None:
        """Сообщить админам, что новости отложены из-за модели.

        Отложенная новость не публикуется и не помечается прочитанной, то
        есть канал просто замолкает. Без предупреждения это выглядит как
        «лент нет», поэтому пишем в личку — но не чаще раза в час, чтобы
        затянувшийся сбой не превратился в поток сообщений.
        """
        if not self.admin_ids:
            return
        now = int(time.time())
        try:
            last = int(self.st.get("llm_alert_at") or 0)
        except ValueError:
            last = 0
        if now - last < ALERT_EVERY:
            return
        self.st.set("llm_alert_at", now)

        lines = [f"⏳ Отложено новостей: {len(self._postponed)} — "
                 f"модель не дала пригодного текста.", ""]
        for feed_title, reason in self._postponed[:3]:
            lines.append(f"· {html.escape(feed_title[:40])}: "
                         f"{html.escape(reason[:120])}")
        lines += ["", "Новости не потеряны: они не помечены прочитанными и "
                  "уйдут в канал, как только модель ответит.",
                  "Проверить модель — /usage и /setmodel. Публиковать без "
                  "обработки — <code>/set on_llm_error raw</code>."]
        text = "\n".join(lines)
        for admin_id in sorted(self.admin_ids):
            try:
                await self.bot.send_message(chat_id=admin_id, text=text,
                                            parse_mode="HTML")
            except TelegramAPIError as exc:
                log.warning("не удалось предупредить админа %s: %s", admin_id, exc)

    # --- внутреннее ------------------------------------------------------
    async def _process_feed(self, feed: sqlite3.Row) -> int:
        feed_id = feed["id"]
        debug = self.debug
        # Условный GET обходим в двух случаях:
        #  * отладка — иначе на 304 нельзя прогнать ту же новость повторно;
        #  * с прошлого раза остался хвост непубликованных новостей — на 304
        #    мы бы его не увидели до следующего обновления ленты.
        skip_cache = debug or bool(feed["pending"])
        result = await fetch(
            feed["url"],
            None if skip_cache else feed["etag"],
            None if skip_cache else feed["modified"],
        )
        now = int(time.time())

        if result.error:
            log.warning("лента #%s: %s", feed_id, result.error)
            self.st.update_feed(feed_id, last_check=now, last_error=result.error[:300])
            return 0

        if result.not_modified:
            self.st.update_feed(feed_id, last_check=now, last_error=None)
            return 0

        updates: dict[str, object] = {"last_check": now, "last_error": None}
        if not debug:
            # Метки условного GET в отладке не сохраняем — см. выше.
            if result.etag:
                updates["etag"] = result.etag
            if result.modified:
                updates["modified"] = result.modified
        if result.feed_title and not feed["title"]:
            updates["title"] = result.feed_title[:120]

        # Пары (ключ, запись): ключ считаем один раз и попутно убираем дубли
        # внутри самой выдачи — ленты иногда повторяют один guid дважды.
        fresh: list[tuple[str, Entry]] = []
        batch_keys: set[str] = set()
        for entry in result.entries:
            if entry.is_empty:
                continue
            key = entry_key(*entry.key_parts)
            if key in batch_keys or self.st.is_seen(feed_id, key):
                continue
            batch_keys.add(key)
            fresh.append((key, entry))

        first_poll = self.st.seen_count(feed_id) == 0
        if first_poll and fresh:
            # Первый опрос новой ленты: не заваливаем канал историей —
            # помечаем архив прочитанным и берём только последние backfill записей.
            backfill = max(0, self.st.get_int("backfill"))
            keep = fresh[len(fresh) - backfill:] if backfill else []
            skipped = fresh[: len(fresh) - len(keep)]
            if skipped:
                self.st.mark_seen(feed_id, [k for k, _ in skipped])
            log.info("лента #%s: первый опрос, пропущено %s записей", feed_id, len(skipped))
            fresh = keep

        fresh = self._drop_stale(feed_id, fresh)
        if not first_poll:
            fresh = self._guard_flood(feed_id, fresh)

        # Берём не больше max_per_cycle за проход; остаток — на следующем.
        to_post = fresh[: max(1, self.st.get_int("max_per_cycle"))]

        published = 0
        delay = max(0, self.st.get_int("post_delay"))
        for i, (key, entry) in enumerate(to_post):
            try:
                post = await self.build_post(entry, feed)
            except LLMError as exc:
                log.warning("лента #%s: LLM отказала (%s)", feed_id, exc)
                if self.st.get("on_llm_error") != "raw":
                    # Новость остаётся непрочитанной: лучше опубликовать её
                    # позже обработанной, чем сейчас — сырой.
                    self._postponed.append((feed["title"] or f"лента #{feed_id}",
                                            str(exc)))
                    continue
                post = await self._fallback_post(entry, feed)

            if debug:
                # Прочитанным не помечаем: после /debug off новость уйдёт в канал.
                if await self._send_debug(post, feed):
                    published += 1
            elif await self._send(post.text, image=post.image, images=post.images):
                self.st.mark_seen(feed_id, key)
                published += 1
                # После отметки о прочтении: новость уже не повторится, значит
                # и в VK не задвоится, даже если он сейчас недоступен.
                await self.send_vk(post)

            if self._blocked:
                break

            if i < len(to_post) - 1 and delay:
                await asyncio.sleep(delay)

        if not debug:
            # Остались непубликованные новости — обрезали по max_per_cycle, не
            # смогли доставить или пропустили из-за отказа модели. На следующем
            # проходе обойдём условный GET, иначе 304 спрячет их до тех пор,
            # пока лента сама не обновится.
            updates["pending"] = int(published < len(fresh))

        self.st.update_feed(feed_id, **updates)
        if published and not debug:
            self.st.prune_seen(feed_id, max(50, self.st.get_int("keep_seen")))
        return published

    async def build_post(self, entry: Entry, feed: sqlite3.Row | None = None) -> Post:
        """Прогоняет запись через шаблон + LLM и собирает готовое сообщение."""
        source = (feed["title"] if feed and feed["title"] else "") or "RSS"
        raw_values = {
            "title": entry.title,
            "summary": entry.summary or entry.title,
            "link": entry.link,
            "source": source,
            "published": entry.published,
        }

        template = (feed["template"] if feed and feed["template"] else "") or self.st.get("template")
        prompt = render(template, raw_values, escape=False)
        ai_text = await self._ask_model(prompt)

        post_format = self.st.get("post_format")
        text = _shorten(render(post_format, {**raw_values, "ai": ai_text}, escape=True))

        if self.claude_mode:
            return Post(text=text, images=await self._images_of_claude(entry),
                       link=entry.link)
        return Post(text=text, image=await self._image_of(entry), link=entry.link)

    def _drop_stale(self, feed_id: int, fresh: list[tuple[str, Entry]]
                    ) -> list[tuple[str, Entry]]:
        """Выбрасывает всё, что давно не новость.

        Ленты иногда возвращают в выдачу старые статьи — после сбоя на
        стороне сайта, смены движка или просто потому, что окно выдачи
        сместилось. Такие записи бот раньше не видел и считал новыми, а в
        канал уходили новости годичной давности. Дату публикации знает сама
        запись, ей и верим; записи без даты судить не берёмся.
        """
        max_age = self.st.get_int("max_age_days")
        if max_age <= 0 or not fresh:
            return fresh
        cutoff = time.time() - max_age * 86400
        stale = [(k, e) for k, e in fresh if e.published_ts and e.published_ts < cutoff]
        if not stale:
            return fresh
        # Помечаем прочитанными, иначе они будут всплывать каждый проход.
        self.st.mark_seen(feed_id, [k for k, _ in stale])
        oldest = max((time.time() - e.published_ts) / 86400 for _, e in stale)
        log.info("лента #%s: пропущено %s записей старше %s дней "
                 "(самой старой %.0f дн): %s", feed_id, len(stale), max_age,
                 oldest, stale[0][1].title[:60])
        keys = {k for k, _ in stale}
        return [(k, e) for k, e in fresh if k not in keys]

    def _guard_flood(self, feed_id: int, fresh: list[tuple[str, Entry]]
                     ) -> list[tuple[str, Entry]]:
        """Страховка на случай, когда лента разом «обновила» всю выдачу.

        Такое бывает при смене схемы идентификаторов: ключи перестают
        совпадать с архивом, и вся лента выглядит новой. Публиковать её
        целиком нельзя, поэтому оставляем только самые свежие записи, а
        остальные молча помечаем прочитанными.
        """
        limit = self.st.get_int("flood_guard")
        if limit <= 0 or len(fresh) <= limit:
            return fresh
        keep_n = max(1, self.st.get_int("backfill"))
        keep = fresh[len(fresh) - keep_n:]
        dropped = fresh[: len(fresh) - keep_n]
        self.st.mark_seen(feed_id, [k for k, _ in dropped])
        log.warning("лента #%s: разом %s новых записей — похоже, выдача "
                    "сбилась. Публикую %s свежих, остальные помечаю "
                    "прочитанными", feed_id, len(fresh), len(keep))
        self._postponed_flood.append((feed_id, len(dropped)))
        return keep

    async def _ask_model(self, prompt: str) -> str:
        """Ответ модели, пригодный к публикации.

        Модель иногда возвращает исходник как есть, проигнорировав просьбу
        перевести. Публиковать такое — всё равно что не обработать новость,
        поэтому даём ей второй заход с прямым указанием, и лишь потом
        признаём отказ.
        """
        llm = self._active_llm
        if self.st.get("require_russian") != "1":
            return await llm.complete(prompt)

        nudge = ("\n\nВажно: ответ должен быть на русском языке. "
                 "Не копируй исходный текст.")
        for attempt in range(1, RU_ATTEMPTS + 1):
            text = await llm.complete(prompt if attempt == 1 else prompt + nudge)
            if looks_russian(text):
                return text
            log.warning("модель ответила не по-русски (попытка %s из %s): %r",
                        attempt, RU_ATTEMPTS, text[:60])
        raise LLMError(f"модель не перевела новость на русский: {text[:80]!r}")

    async def _image_of(self, entry: Entry) -> str:
        """Картинка новости: из ленты, а если её там нет — со страницы.

        Страницу открываем только здесь, то есть лишь для тех записей, что
        уже отобраны к публикации, и результат запоминаем — включая «нет
        картинки», иначе на каждую неудачу приходился бы новый запрос.
        """
        if self.st.get("images") != "1":
            return ""
        if entry.image:
            return entry.image
        if self.st.get("og_image") != "1" or not entry.link:
            return ""

        cached = self.st.page_image(entry.link)
        if cached is not None:
            return cached
        found = await page_image(entry.link)
        if found is None:
            # Страница не открылась — в кэш не пишем, чтобы разовый сбой
            # или 429 не лишил ленту картинок навсегда.
            log.info("страница новости не прочиталась: %s", entry.link[:90])
            return ""
        self.st.set_page_image(entry.link, found)
        log.info("картинка со страницы %s: %s", entry.link[:80],
                 found[:80] or "на странице её нет")
        return found

    async def _images_of_claude(self, entry: Entry) -> list[tuple[bytes, str]]:
        """Несколько картинок со страницы новости — режим Claude.

        В отличие от _image_of (одна картинка, может остаться просто ссылкой),
        здесь качаем сами: несколько ссылок из разных источников надёжнее
        отправлять байтами, чем адресами за нестабильными CDN.
        """
        if self.st.get("images") != "1" or not entry.link:
            return []
        limit = max(1, min(10, self.st.get_int("claude_max_images")))
        candidates = [entry.image] if entry.image else []
        candidates += [u for u in await page_images(entry.link, limit=limit) if u not in candidates]

        out: list[tuple[bytes, str]] = []
        for url in candidates:
            if len(out) >= limit:
                break
            downloaded = await download_image(url, referer=entry.link)
            if downloaded is None:
                log.info("Claude: картинка не скачалась, пропускаю: %s", url[:100])
                continue
            out.append(downloaded)
        return out

    async def _fallback_post(self, entry: Entry, feed: sqlite3.Row | None) -> Post:
        """Если LLM недоступна — публикуем аккуратную заготовку без обработки."""
        source = (feed["title"] if feed and feed["title"] else "") or "RSS"
        summary = (entry.summary or "")[:600]
        values = {
            "title": entry.title,
            "summary": summary,
            "link": entry.link,
            "source": source,
            "published": entry.published,
            "ai": summary,
        }
        text = _shorten(render(self.st.get("post_format"), values, escape=True))
        if self.claude_mode:
            return Post(text=text, images=await self._images_of_claude(entry),
                       link=entry.link)
        return Post(text=text, image=await self._image_of(entry), link=entry.link)

    async def send_vk(self, post: Post) -> bool:
        """Дублирует пост в сообщество VK. Ошибку только логируем.

        VK — второй адресат, а не главный: если он отвалился, публикация в
        Telegram уже состоялась и новость помечена прочитанной.
        """
        if not self.vk_on:
            return False
        self.vk.group_id = self.vk_group
        try:
            post_id = await self.vk.post(to_plain(post.text), post.image, post.link)
        except VKError as exc:
            log.error("VK: пост не опубликован — %s", exc)
            return False
        except Exception:
            log.exception("VK: непредвиденная ошибка при публикации")
            return False
        log.info("VK: опубликовано wall-%s_%s", self.vk.group_id, post_id or "?")
        self._vk_posted += 1
        return True

    async def _send_debug(self, post: Post, feed: sqlite3.Row) -> bool:
        """Отладка: показываем пост админам в личке вместо канала."""
        if not self.admin_ids:
            log.error("режим отладки включён, но ADMIN_IDS пуст — некому показывать")
            return False
        header = (
            f"🔧 <b>ОТЛАДКА</b> · лента #{feed['id']} "
            f"{html.escape(feed['title'] or '')}\n"
            f"Так пост выглядел бы в канале. В канал не отправлено, "
            f"новость останется непрочитанной."
            + (" В VK в отладке тоже не публикуем." if self.vk_on else "")
        )
        delivered = False
        for admin_id in sorted(self.admin_ids):
            if await self._send(header, chat_id=admin_id) and \
                    await self._send(post.text, chat_id=admin_id, image=post.image,
                                    images=post.images):
                delivered = True
        return delivered

    def _preview(self, image: str = "") -> LinkPreviewOptions:
        """Картинку к длинному посту показываем превью-ссылкой над текстом."""
        if image:
            # is_disabled задаём явно: иначе значение возьмётся из умолчаний
            # бота и превью (а вместе с ним картинка) может не показаться.
            return LinkPreviewOptions(url=image, is_disabled=False,
                                      prefer_large_media=True, show_above_text=True)
        return LinkPreviewOptions(is_disabled=self.st.get("disable_preview") == "1")

    async def _send(self, text: str, chat_id: int | str | None = None,
                    image: str = "", images: list[tuple[bytes, str]] | None = None) -> bool:
        target = chat_id if chat_id is not None else self.channel
        if not target:
            log.error("канал не задан: укажите CHANNEL_ID или /setchannel")
            self._blocked = True
            return False

        images = images or []
        fits_caption = tg_len(text) <= TG_CAPTION_LIMIT

        if len(images) >= 2:
            if await self._send_media_group(text if fits_caption else "", target, images[:10]):
                if fits_caption:
                    return True
                # Подпись длиннее лимита медиагруппы — картинки уже ушли,
                # текст отдельным сообщением следом.
                return await self._send(text, chat_id)
            if self._blocked:
                return False
            # Альбом не отправился (например Telegram отверг один из файлов) —
            # пробуем хотя бы первую картинку одиночным фото.
            images = images[:1]

        single: str | tuple[bytes, str] | None = images[0] if images else (image or None)
        # Пост с картинкой уходит фото с подписью — но подпись у Telegram
        # вчетверо короче сообщения, поэтому длинные посты отправляем текстом,
        # а картинку прикладываем превью-ссылкой, чтобы не резать содержимое.
        if single and fits_caption:
            if await self._send_photo(text, target, single):
                return True
            if self._blocked:
                return False
        preview = self._preview(image)
        for _ in range(3):
            try:
                await self.bot.send_message(
                    chat_id=target,
                    text=text,
                    parse_mode="HTML",
                    link_preview_options=preview,
                )
                return True
            except TelegramRetryAfter as exc:
                log.warning("лимит Telegram, ждём %ss", exc.retry_after)
                await asyncio.sleep(exc.retry_after + 1)
            except TelegramBadRequest as exc:
                if not self._is_markup_error(exc):
                    log.error("не удалось отправить в %s: %s", target, exc)
                    self._blocked = True
                    return False
                # Разметку Telegram не принял (обычно из-за /setformat).
                # Публикуем текстом, иначе новость зависнет и будет
                # переобрабатываться моделью каждый проход.
                log.error("Telegram отверг разметку (%s) — отправляю без "
                          "форматирования; проверьте /format", exc)
                return await self._send_plain(strip_html(text), target)
            except TelegramAPIError as exc:
                log.error("не удалось отправить в %s: %s", target, exc)
                self._blocked = True
                return False
        return False

    async def _send_photo(self, text: str, target: int | str,
                          image: str | tuple[bytes, str]) -> bool:
        """Фото по URL или уже скачанным байтам. False — картинка не подошла,
        пост уйдёт отдельно."""
        photo = image if isinstance(image, str) else BufferedInputFile(
            image[0], filename=f"image.{_ext_for(image[1])}")
        label = image if isinstance(image, str) else f"{len(image[0])} байт, {image[1]}"
        for _ in range(3):
            try:
                await self.bot.send_photo(
                    chat_id=target,
                    photo=photo,
                    caption=text,
                    parse_mode="HTML",
                )
                return True
            except TelegramRetryAfter as exc:
                log.warning("лимит Telegram, ждём %ss", exc.retry_after)
                await asyncio.sleep(exc.retry_after + 1)
            except TelegramBadRequest as exc:
                if self._is_markup_error(exc):
                    # Разметка сломана — картинка ни при чём, пусть общий путь
                    # разбирается и публикует текстом.
                    return False
                if self._is_delivery_error(exc):
                    log.error("не удалось отправить в %s: %s", target, exc)
                    self._blocked = True
                    return False
                log.warning("Telegram не принял картинку %s (%s) — публикую "
                            "пост без фото", label, exc)
                return False
            except TelegramAPIError as exc:
                log.warning("ошибка отправки фото в %s (%s) — публикую пост "
                            "без фото", target, exc)
                return False
        return False

    async def _send_media_group(self, caption: str, target: int | str,
                                images: list[tuple[bytes, str]]) -> bool:
        """Альбом из 2-10 картинок (режим Claude). Подпись — только на первой,
        так её показывает сам Telegram. False — не отправился целиком, пост
        попробует уйти другим путём (см. _send)."""
        media = []
        for i, (data, ctype) in enumerate(images):
            file = BufferedInputFile(data, filename=f"image{i}.{_ext_for(ctype)}")
            if i == 0 and caption:
                media.append(InputMediaPhoto(media=file, caption=caption, parse_mode="HTML"))
            else:
                media.append(InputMediaPhoto(media=file))

        for _ in range(3):
            try:
                await self.bot.send_media_group(chat_id=target, media=media)
                return True
            except TelegramRetryAfter as exc:
                log.warning("лимит Telegram, ждём %ss", exc.retry_after)
                await asyncio.sleep(exc.retry_after + 1)
            except TelegramBadRequest as exc:
                if self._is_delivery_error(exc):
                    log.error("не удалось отправить в %s: %s", target, exc)
                    self._blocked = True
                    return False
                log.warning("Telegram не принял альбом (%s) — пробую иначе", exc)
                return False
            except TelegramAPIError as exc:
                log.warning("ошибка отправки альбома в %s (%s) — пробую иначе",
                            target, exc)
                return False
        return False

    @staticmethod
    def _is_delivery_error(exc: TelegramBadRequest) -> bool:
        """Отказ по адресату: повтор и отправка без фото тоже не помогут."""
        text = str(exc).lower()
        return any(s in text for s in (
            "chat not found", "chat_id", "not enough rights", "forbidden",
            "bot was kicked", "have no rights", "chat_write_forbidden",
        ))

    @staticmethod
    def _is_markup_error(exc: TelegramBadRequest) -> bool:
        text = str(exc).lower()
        return "entit" in text or "tag" in text or "parse" in text

    async def _send_plain(self, text: str, target: int | str) -> bool:
        try:
            await self.bot.send_message(chat_id=target, text=_shorten(text),
                                        parse_mode=None)
            return True
        except TelegramAPIError as exc:
            log.error("не удалось отправить даже без разметки в %s: %s", target, exc)
            return False
