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
                                TelegramNetworkError, TelegramRetryAfter)
from aiogram.types import (BufferedInputFile, InlineKeyboardButton,
                            InlineKeyboardMarkup, InputMediaPhoto,
                            LinkPreviewOptions, Message, WebAppInfo)

from .claude import ClaudeClient
from .db import Storage, entry_key
from .llm import LLMClient, LLMError, LLMQuotaExceeded
from .quota import Quota
from .rss import (Entry, FetchResult, download_image, fetch,
                  fetch_article_entry, image_dedup_key, page_image,
                  page_images, strip_html)
from .search import (BingNewsClient, SearchClient, domain_of,
                     merge_search_results, site_query)
from .vk import VKClient, VKError, to_plain

log = logging.getLogger(__name__)

TG_LIMIT = 4096
TG_CAPTION_LIMIT = 1024   # подпись к фото Telegram обрезает жёстче текста
ELLIPSIS = "…"
MAINTENANCE_EVERY = 6 * 3600   # уборка в базе, секунды
ALERT_EVERY = 3600             # как часто напоминать об отложенных новостях
# Захват карточки очереди согласования под публикацию (claim_moderation)
# "протухает" через столько секунд — запас на скачивание альбома (до 10
# картинок) и ретраи _send (до 3 попыток с задержками), с учётом медленной
# сети до Telegram. Раньше было 180 — этого впритык хватает при обычной
# скорости, но при заметно просевшей сети/большом альбоме claim мог истечь
# ДО завершения ещё идущей отправки, и повторный клик (или следующий
# scheduled-проход) захватил бы карточку по новой, отправив дубль. Не
# наткнуться на dead lock, если процесс упал между отправкой и удалением
# карточки из очереди.
PUBLISH_CLAIM_TTL = 600
RU_ATTEMPTS = 2                # попыток добиться от модели русского текста
# Сколько картинок для одной новости качать параллельно (см. _images_of_page).
# Сайт-источник и его CDN — общие: слишком широкий веер бьёт по ним не хуже,
# чем по нам, а выигрыш по времени после ~4 параллельных запросов уже плоский.
IMAGE_DOWNLOAD_CONCURRENCY = 4
# Схожесть дублей (см. _find_duplicate) — заголовок и summary должны совпасть
# хотя бы настолько каждый по отдельности, иначе на общих словах вроде
# «анонсировала», «представила» дублями считалось бы что попало.
DEDUP_MIN_SIGNAL = 0.25
_DEDUP_WORD_RE = re.compile(r"[а-яёa-z0-9]+", re.I)

# Franchise/газетные слова — сами по себе не удостоверяют, что новость об
# одном и том же товаре: «New Warhammer Age of Sigmar ...», «New Space
# Marine ...» — общий префикс десятков заголовков о совершенно разных
# моделях. См. _shares_named_run: значимыми при подсчёте совпадения
# считаются только слова НЕ из этого набора.
_DEDUP_STOPWORDS = frozenset({
    "a", "an", "the", "new", "is", "are", "was", "were", "for", "with",
    "and", "or", "in", "on", "into", "of", "to", "from", "by", "at",
    "warhammer", "games", "workshop", "age", "sigmar", "40k", "40", "000",
    "space", "marine", "marines",
    "reveal", "reveals", "revealed", "announce", "announces", "announced",
    "unveil", "unveils", "unveiled", "preview", "previewed", "review",
    "reviewed", "first", "look", "coming", "soon", "here", "box", "boxed",
    "game", "mini", "kit", "model", "miniature", "miniatures",
})
# Порог для _shares_named_run: сколько подряд идущих слов заголовка должно
# совпасть, и сколько из них — значимых (не из _DEDUP_STOPWORDS).
_NAMED_RUN_MIN_LEN = 3
_NAMED_RUN_MIN_SIGNAL = 2


def _dedup_words(text: str) -> set[str]:
    return set(_DEDUP_WORD_RE.findall(text.lower()))


def _shares_named_run(title_a: str, title_b: str) -> bool:
    """Общее для двух заголовков название продукта/модели — сильнее, чем
    доля общих слов (_dedup_similarity): разные сайты почти всегда
    переписывают заголовок и описание своими словами, но собственное имя
    анонса («Captain on Bike», «Brawlers of Behemat») повторяют дословно.

    Ищем самую длинную ОБЩУЮ ПОДРЯД ИДУЩУЮ последовательность слов в двух
    заголовках (не просто пересечение множеств — порядок важен, иначе
    «набор одних и тех же общих слов» ловил бы то же самое, для чего уже
    есть DEDUP_MIN_SIGNAL и его проблема с непохожими summary). Найденный
    случай реально сработал: warhammer-community.com и ontabletop.com
    дали заголовкам «New Space Marine Captain On Bike Rides Into
    Warhammer 40,000» и «New Space Marine Captain on Bike revealed» дают
    схожесть по словам всего 0.5, а их описания — вообще другими словами
    (0.12), ниже DEDUP_MIN_SIGNAL — связка не считалась дублем вовсе.
    """
    wa = _DEDUP_WORD_RE.findall(title_a.lower())
    wb = _DEDUP_WORD_RE.findall(title_b.lower())
    best: list[str] = []
    for i in range(len(wa)):
        if len(wa) - i <= len(best):
            break
        for j in range(len(wb)):
            k = 0
            while i + k < len(wa) and j + k < len(wb) and wa[i + k] == wb[j + k]:
                k += 1
            if k > len(best):
                best = wa[i:i + k]
    if len(best) < _NAMED_RUN_MIN_LEN:
        return False
    return sum(1 for w in best if w not in _DEDUP_STOPWORDS) >= _NAMED_RUN_MIN_SIGNAL


def _dedup_similarity(a: str, b: str) -> float:
    """Доля общих слов от объединения (индекс Жаккара) — не идеально точно,
    зато не тянет новых зависимостей и ловит «то же событие, другими
    словами» гораздо лучше точного совпадения ссылки/guid."""
    wa, wb = _dedup_words(a), _dedup_words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


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
    # Несколько картинок (/feedimages <id> on) — уже скачанные байты, не
    # ссылки: Telegram лучше принимает файл, чем адрес за нестабильным CDN
    # источника.
    images: list[tuple[bytes, str]] = field(default_factory=list)
    link: str = ""      # адрес новости — вложение для VK, если фото нечем грузить


class Publisher:
    def __init__(self, bot: Bot, storage: Storage, llm: LLMClient, default_channel: str,
                 admin_ids: set[int] | None = None, quota: "Quota | None" = None,
                 vk: "VKClient | None" = None, claude: "ClaudeClient | None" = None,
                 gemini: "LLMClient | None" = None, search: "SearchClient | None" = None,
                 bing: "BingNewsClient | None" = None, panel_url: str = ""):
        self.bot = bot
        self.st = storage
        self.llm = llm
        self.claude = claude
        self.gemini = gemini
        self.default_channel = default_channel
        self.admin_ids = set(admin_ids or ())
        self.quota = quota
        self.vk = vk
        self.search = search
        # Для кнопки в уведомлении о новых карточках на согласование
        # (открывает веб-панель сразу на /queue) — пусто, если панель выключена.
        self.panel_url = panel_url.rstrip("/")
        self.bing = bing
        self._wake = asyncio.Event()
        self._running = False
        # /checknow и фоновый цикл не должны опрашивать ленты одновременно —
        # иначе одна и та же новость успеет уйти в канал дважды.
        self._lock = asyncio.Lock()
        self._vk_posted = 0
        self._postponed: list[tuple[str, str]] = []
        self._postponed_flood: list[tuple[int, int]] = []
        self._postponed_dupes: list[tuple[int, str, int, float]] = []
        self._queued: list[tuple[str, str, int]] = []
        self._queue_overflow = False
        # Первую уборку делаем не сразу после старта, а через MAINTENANCE_EVERY.
        self._last_maintenance = time.time()
        # Взводится, когда Telegram отказал по причине, которую повтором не
        # исправить (нет прав, канал не найден). Тогда дальше в этом проходе
        # посты не генерируем — иначе лимит модели уходит на недоставляемое.
        self._blocked = False
        # Защита от двойного клика/повторной отправки формы для publish_now
        # и retry_postponed — у них, в отличие от согласования, нет claim в
        # БД (нечего claim'ить: строка не в очереди согласования, а в
        # dedup_candidates/postponed). Обычный set с проверкой-и-добавлением
        # без await между ними безопасен под GIL одного процесса.
        self._manual_publish_locks: set[str] = set()

    # --- публичный API ---------------------------------------------------
    @property
    def channel(self) -> str:
        return self.st.get("channel_id") or self.default_channel

    @property
    def debug(self) -> bool:
        return self.st.get("debug") == "1"

    @property
    def moderation(self) -> bool:
        """Ручное согласование (см. /manual): готовые посты не публикуются
        сами, ждут одобрения в веб-панели/Telegram. Названо не "manual" —
        этим словом уже занят параметр run_once(manual=...) («запущено
        командой /checknow»), смысл другой, разные имена — чтобы не путать."""
        return self.st.get("moderation") == "1"

    def _moderation_queue_full(self) -> bool:
        limit = self.st.get_int("moderation_max_queue")
        return limit > 0 and self.st.count_moderation() >= limit

    @property
    def claude_mode(self) -> bool:
        """Включён и есть чем работать — иначе тихо остаёмся на обычном LLM."""
        return (self.st.get("claude_mode") == "1"
                and self.claude is not None and bool(self.claude.api_key))

    @property
    def gemini_mode(self) -> bool:
        return (self.st.get("gemini_mode") == "1"
                and self.gemini is not None and bool(self.gemini.api_key))

    @staticmethod
    def multi_images_for(feed: sqlite3.Row | None) -> bool:
        """Несколько картинок со страницы новости альбомом вместо одной —
        свойство конкретной ленты (/feedimages), не завязано на то, какой
        бэкенд обрабатывает текст (обычный, Claude или Gemini). Без ленты
        (например при ручной публикации найденного дубля) — одна картинка,
        как раньше по умолчанию."""
        return bool(feed is not None and feed["multi_images"])

    @property
    def _active_llm(self) -> "LLMClient | ClaudeClient":
        # Claude и Gemini включаются командами /claude on и /gemini on, которые
        # сами гасят друг друга — но на случай, если оба флага всё же оказались
        # взведены разом (например, ручная правка базы), берём Claude: он
        # платный, и молча подменить его на бесплатный Gemini было бы более
        # заметным сюрпризом, чем наоборот.
        if self.claude_mode and self.claude:
            return self.claude
        if self.gemini_mode and self.gemini:
            return self.gemini
        return self.llm

    @property
    def backend_key(self) -> str:
        """Ключ активного сейчас бэкенда для Quota ('default'/'claude'/'gemini') —
        расход каждого считается отдельно (см. bot/quota.py)."""
        if self.claude_mode and self.claude:
            return "claude"
        if self.gemini_mode and self.gemini:
            return "gemini"
        return "default"

    @property
    def active_backend_label(self) -> str:
        """Имя реально активного сейчас бэкенда — для статусов и сообщений
        вместо жёстко зашитого «DeepSeek», который бэкендом быть не обязан
        (LLM_* в .env может указывать на любую OpenAI-совместимую модель)."""
        if self.claude_mode and self.claude:
            return f"Claude ({self.claude.model})"
        if self.gemini_mode and self.gemini:
            return f"Gemini ({self.gemini.model})"
        return self.llm.model

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
            await self._housekeeping()
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

    async def _housekeeping(self) -> None:
        """После прохода: вернуть память системе и раз в несколько часов
        подчистить базу. Обе операции дешёвые и делаются между проходами,
        чтобы не задерживать публикацию."""
        release_memory()
        now = time.time()
        if now - self._last_maintenance < MAINTENANCE_EVERY:
            await self._check_moderation_reminder()
            return
        self._last_maintenance = now
        try:
            sizes = self.st.maintain(
                max(50, self.st.get_int("keep_seen")),
                keep_moderation_days=max(1, self.st.get_int("moderation_keep_days")),
            )
        except Exception:
            log.exception("уборка в базе не удалась")
            return
        log.info("уборка в базе: %.0f → %.0f КБ",
                 sizes["before"] / 1024, sizes["after"] / 1024)
        if sizes.get("moderation_pruned"):
            await self._report_moderation_pruned(sizes["moderation_pruned"])
        await self._check_moderation_reminder()

    async def run_once(self, manual: bool = False) -> dict[str, int]:
        """Один проход по всем активным лентам. Возвращает сводку.

        `manual=True` — запуск командой /checknow. В режиме отладки
        автоматический проход публикацию не делает: новости не помечаются
        прочитанными, и одни и те же посты приходили бы в личку каждый цикл.
        """
        stats = {"feeds": 0, "published": 0, "queued": 0, "errors": 0, "vk": 0,
                 "postponed": 0, "scheduled": 0, "debug": int(self.debug)}
        # До проверки паузы/отладки и независимо от них: расписание — такое
        # же явное решение админа, как клик «Опубликовать» (которому тоже
        # не мешают пауза/отладка, см. publish_moderated), а не часть
        # автоматического конвейера, который они призваны останавливать.
        try:
            stats["scheduled"] = await self.run_scheduled_publishes()
        except Exception:
            log.exception("сбой при публикации карточек по расписанию")
        if self.st.get("paused") == "1":
            log.info("публикация на паузе — пропускаем проход")
            return stats
        if self.debug and not manual:
            log.info("режим отладки — автоматический проход пропущен, ждём /checknow")
            return stats

        async with self._lock:
            # Внутри лока, а не до него: иначе второй параллельный run_once
            # (фоновый цикл + ручной /checknow), уже стоящий в очереди на
            # тот же лок, мог бы обнулить эти списки прямо во время того,
            # как первый вызов их ещё заполняет — искажая его же отчёт
            # (_report_postponed/_report_dupes/_report_flood) вплоть до нуля,
            # хотя сама публикация под локом остаётся корректной.
            self._vk_posted = 0
            self._postponed = []
            self._postponed_flood = []
            self._postponed_dupes = []
            self._queued = []
            self._queue_overflow = False
            self._blocked = False
            for feed in self.st.feeds(only_enabled=True):
                stats["feeds"] += 1
                try:
                    published, queued = await self._process_feed(feed)
                    stats["published"] += published
                    stats["queued"] += queued
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
        if self._postponed_dupes:
            await self._report_dupes()
        if self._queued and self.st.get("moderation_notify") == "1":
            await self._report_queued()
        if self._queue_overflow:
            await self._report_queue_overflow()
        if (stats["published"] or stats["queued"]) and self.quota:
            await self.quota.check_and_alert(self.backend_key)
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

    async def _report_dupes(self) -> None:
        """Сообщить админам про новости, отложенные как похожие на уже
        опубликованные — они не в канале, а в очереди на ручной разбор
        (веб-панель, раздел «Ленты»), на случай если совпадение ложное."""
        if not self.admin_ids:
            return
        lines = [f"🔁 Похоже на дубли: {len(self._postponed_dupes)} — не "
                 f"опубликовано, ждёт разбора в веб-панели («Ленты»).", ""]
        for feed_id, title, matched_post_id, score in self._postponed_dupes[:5]:
            feed = self.st.feed(feed_id)
            feed_title = (feed["title"] if feed else "") or f"лента #{feed_id}"
            lines.append(f"· {html.escape(feed_title[:30])}: {html.escape(title[:60])} "
                         f"— похоже на пост #{matched_post_id} ({score:.0%})")
        lines += ["", "Совпадение ложное — новость просто интересная, а не "
                  "дубль? Откройте «Ленты» в веб-панели и опубликуйте вручную."]
        text = "\n".join(lines)
        for admin_id in sorted(self.admin_ids):
            try:
                await self.bot.send_message(chat_id=admin_id, text=text,
                                            parse_mode="HTML")
            except TelegramAPIError as exc:
                log.warning("не удалось предупредить админа %s о дублях: %s", admin_id, exc)

    async def _report_gemini_quota(self) -> None:
        """Сообщить, что Gemini исчерпал квоту и бот сам переключился на
        основной LLM — иначе выглядит так, будто /gemini off нажал кто-то
        другой, и админ не поймёт, откуда взялась смена модели.

        Вызывается прямо из _complete() в момент обнаружения — не только
        из run_once(), но и из ручных команд (/test, /gemini test, /regen
        и т.п.), которые дёргают build_post в обход обычного цикла и раньше
        оставляли админа без объяснения. Раз gemini_mode гасится сразу же,
        второй раз само по себе не сработает — троттлинг только на случай
        редкой гонки (параллельный ручной тест во время автопрохода).
        """
        if not self.admin_ids:
            return
        now = int(time.time())
        try:
            last = int(self.st.get("gemini_quota_alert_at") or 0)
        except ValueError:
            last = 0
        if now - last < ALERT_EVERY:
            return
        self.st.set("gemini_quota_alert_at", now)
        text = (
            f"⚠️ У Gemini кончилась квота (HTTP 429) — бот автоматически "
            f"переключился на основной LLM ({html.escape(self.llm.model)}), "
            f"режим Gemini выключен (<code>/gemini off</code>).\n\n"
            f"Публикация продолжается без остановки. Когда квота обновится "
            f"(обычно на следующие сутки) — включить Gemini обратно можно "
            f"командой <code>/gemini on</code>."
        )
        for admin_id in sorted(self.admin_ids):
            try:
                await self.bot.send_message(chat_id=admin_id, text=text,
                                            parse_mode="HTML")
            except TelegramAPIError as exc:
                log.warning("не удалось предупредить админа %s о квоте Gemini: %s",
                            admin_id, exc)

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

    async def _report_queued(self) -> None:
        """Сообщить о новых карточках на согласование.

        Без троттлинга, в отличие от _report_postponed/_report_gemini_quota:
        там повторяется один и тот же отказ каждый проход, а здесь каждая
        карточка уведомляется РОВНО ОДИН РАЗ за свою жизнь (mark_seen уже
        стоит при постановке в очередь) — объём сообщений равен объёму
        новых новостей, троттлинг только прятал бы часть из них.
        """
        if not self.admin_ids:
            return
        total = self.st.count_moderation()
        lines = [f"🖐 На согласование: {len(self._queued)} (в очереди всего {total})", ""]
        for feed_title, title, _item_id in self._queued[:5]:
            lines.append(f"· {html.escape(feed_title[:30])}: {html.escape(title[:60])}")
        lines += ["", "Открыть очередь — /queue или веб-панель, раздел «Согласование»."]
        text = "\n".join(lines)
        kb = None
        if self.panel_url:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text="🖐 Открыть очередь", web_app=WebAppInfo(url=f"{self.panel_url}/queue"))]])
        for admin_id in sorted(self.admin_ids):
            try:
                await self.bot.send_message(chat_id=admin_id, text=text,
                                            parse_mode="HTML", reply_markup=kb)
            except TelegramAPIError as exc:
                log.warning("не удалось уведомить админа %s об очереди согласования: %s",
                            admin_id, exc)

    async def _report_queue_overflow(self) -> None:
        """Очередь согласования заполнена — новые новости не обрабатываются
        вовсе, пока админ её не разберёт. Троттлинг как у _report_postponed:
        иначе застрявшая переполненная очередь слала бы это на каждом проходе."""
        if not self.admin_ids:
            return
        now = int(time.time())
        try:
            last = int(self.st.get("moderation_overflow_alert_at") or 0)
        except ValueError:
            last = 0
        if now - last < ALERT_EVERY:
            return
        self.st.set("moderation_overflow_alert_at", now)
        limit = self.st.get_int("moderation_max_queue")
        text = (f"⚠️ Очередь согласования заполнена ({limit}) — новые новости не "
               f"обрабатываются, пока не разберёте текущие. /queue")
        for admin_id in sorted(self.admin_ids):
            try:
                await self.bot.send_message(chat_id=admin_id, text=text, parse_mode="HTML")
            except TelegramAPIError as exc:
                log.warning("не удалось предупредить админа %s о переполнении очереди: %s",
                            admin_id, exc)

    async def _report_moderation_pruned(self, count: int) -> None:
        """В отличие от prune_postponed/prune_dedup_candidates (чистят молча)
        — здесь выбрасываются ГОТОВЫЕ посты, которые никто не разобрал за
        moderation_keep_days; молчать об этом нельзя."""
        if not self.admin_ids:
            return
        text = (f"🗑 Автоматически отклонено {count} карточек на согласование — "
               f"не разобрали дольше {self.st.get_int('moderation_keep_days')} дней.")
        for admin_id in sorted(self.admin_ids):
            try:
                await self.bot.send_message(chat_id=admin_id, text=text, parse_mode="HTML")
            except TelegramAPIError as exc:
                log.warning("не удалось сообщить админу %s об уборке очереди: %s",
                            admin_id, exc)

    async def _check_moderation_reminder(self) -> None:
        """Напоминание, если очередь согласования не разбирают долго (см.
        moderation_remind_hours) — иначе фича молча превращается в «канал
        перестал публиковать», и не сразу понятно, почему."""
        hours = self.st.get_int("moderation_remind_hours")
        if hours <= 0 or not self.admin_ids:
            return
        oldest = self.st.oldest_moderation_at()
        if oldest is None:
            return
        age = time.time() - oldest
        if age < hours * 3600:
            return
        now = int(time.time())
        try:
            last = int(self.st.get("moderation_remind_at") or 0)
        except ValueError:
            last = 0
        if now - last < ALERT_EVERY:
            return
        self.st.set("moderation_remind_at", now)
        total = self.st.count_moderation()
        text = (f"⏰ В очереди согласования {total} — самая старая карточка "
               f"ждёт {age / 3600:.0f} ч. Открыть — /queue.")
        for admin_id in sorted(self.admin_ids):
            try:
                await self.bot.send_message(chat_id=admin_id, text=text, parse_mode="HTML")
            except TelegramAPIError as exc:
                log.warning("не удалось напомнить админу %s об очереди согласования: %s",
                            admin_id, exc)

    # --- внутреннее ------------------------------------------------------
    async def _process_feed(self, feed: sqlite3.Row) -> tuple[int, int]:
        feed_id = feed["id"]
        kind = feed["kind"]
        debug = self.debug
        # Условный GET обходим в двух случаях:
        #  * отладка — иначе на 304 нельзя прогнать ту же новость повторно;
        #  * с прошлого раза остался хвост непубликованных новостей — на 304
        #    мы бы его не увидели до следующего обновления ленты.
        skip_cache = debug or bool(feed["pending"])
        if kind == "search":
            result = await self._fetch_search(feed)
        else:
            result = await fetch(
                feed["url"],
                None if skip_cache else feed["etag"],
                None if skip_cache else feed["modified"],
            )
        now = int(time.time())

        if result.error:
            log.warning("лента #%s: %s", feed_id, result.error)
            self.st.update_feed(feed_id, last_check=now, last_error=result.error[:300])
            return 0, 0

        if result.not_modified:
            self.st.update_feed(feed_id, last_check=now, last_error=None)
            return 0, 0

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
        # внутри самой выдачи. У search-записей на этом этапе ещё нет
        # заголовка (см. ниже) — is_empty их забраковал бы все разом,
        # поэтому для kind='search' проверку пропускаем: пустые «на самом
        # деле» отсеются на hydrate.
        fresh: list[tuple[str, Entry]] = []
        batch_keys: set[str] = set()
        for entry in result.entries:
            if entry.is_empty and kind != "search":
                continue
            key = entry_key(*entry.key_parts)
            if key in batch_keys or self.st.is_seen(feed_id, key):
                continue
            batch_keys.add(key)
            fresh.append((key, entry))

        first_poll = self.st.seen_count(feed_id) == 0

        def backfill_slice(fresh: list[tuple[str, Entry]]) -> list[tuple[str, Entry]]:
            """Первый опрос новой ленты: не заваливаем канал историей —
            помечаем архив прочитанным и берём только последние backfill
            записей. Обязательно вызывать по УЖЕ настоящей хронологии (для
            search — после hydrate, см. ниже), иначе «оставить последние N»
            режет не по дате, а по тому порядку, что был в fresh на тот
            момент — для search это раньше был порядок выдачи поиска
            (релевантность/популярность), и раскрученная, но более старая
            статья вытесняла объективно более новую, которая просто ниже
            ранжируется у поисковика. Отрезанная так статья потом навсегда
            остаётся отмеченной прочитанной и никогда не публикуется —
            баг был не разовым сбоем, а тихой потерей конкретных новостей."""
            if not (first_poll and fresh):
                return fresh
            backfill = max(0, self.st.get_int("backfill"))
            keep = fresh[len(fresh) - backfill:] if backfill else []
            skipped = fresh[: len(fresh) - len(keep)]
            if skipped:
                self.st.mark_seen(feed_id, [k for k, _ in skipped])
            log.info("лента #%s: первый опрос, пропущено %s записей", feed_id, len(skipped))
            return keep

        if kind != "search":
            fresh = backfill_slice(fresh)
            fresh = self._drop_stale(feed_id, fresh)
        else:
            # flood_guard — до дочитывания страниц, не после: иначе сбитая
            # выдача поиска обернулась бы лишними запросами к сайту-
            # источнику ради записей, которые всё равно отсеются. На этом
            # этапе published_ts у записей ещё синтетический (по порядку
            # выдачи, см. _fetch_search) — flood_guard смотрит только на
            # количество, не на дату, ему это не мешает.
            if not first_poll:
                fresh = self._guard_flood(feed_id, fresh)
            # После дочитывания страниц у записей появляется настоящая дата
            # публикации (fetch_article_entry достаёт datePublished со
            # страницы статьи) и список пересортирован по ней — только
            # теперь backfill/max_age_days можно применять осмысленно,
            # раньше (по синтетическому порядку выдачи) оба были бы по сути
            # лотереей вместо «оставить настоящие последние N».
            fresh = await self._hydrate_search_entries(fresh)
            fresh = self._drop_stale(feed_id, fresh)
            fresh = backfill_slice(fresh)
        fresh = self._drop_duplicates(feed, fresh)
        if not first_poll and kind != "search":
            fresh = self._guard_flood(feed_id, fresh)

        # Берём не больше max_per_cycle за проход; остаток — на следующем.
        to_post = fresh[: max(1, self.st.get_int("max_per_cycle"))]

        published = 0
        queued = 0
        # Ручное согласование не действует в отладке — у них несовместимые
        # контракты: debug обязан ничего не менять в состоянии (seen/etag не
        # пишутся), а постановка в очередь согласования, наоборот, обязана
        # пометить запись прочитанной (см. _queue_for_review) — иначе один
        # /checknow в отладке плодил бы в очереди дубли карточек.
        moderation_now = self.moderation and not debug
        delay = max(0, self.st.get_int("post_delay"))
        for i, (key, entry) in enumerate(to_post):
            if moderation_now and self._moderation_queue_full():
                log.warning("лента #%s: очередь согласования заполнена — "
                            "новости не обрабатываю, пока не разберут", feed_id)
                self._queue_overflow = True
                break
            try:
                if moderation_now:
                    text = await self.build_post_text(entry, feed)
                else:
                    post = await self.build_post(entry, feed)
            except LLMError as exc:
                log.warning("лента #%s: LLM отказала (%s)", feed_id, exc)
                if self.st.get("on_llm_error") != "raw":
                    # Новость остаётся непрочитанной: лучше опубликовать её
                    # позже обработанной, чем сейчас — сырой. Пишем и в БД
                    # (не только в self._postponed, который живёт только до
                    # конца этого прохода) — иначе в веб-панели админ не видит
                    # вообще ничего, пока сам не откроет журнал. Отказ модели
                    # обрабатывается ДО постановки в очередь согласования —
                    # это две разные очереди с разным смыслом («бот не
                    # смог» и «бот смог, ждём человека»), пересекаться им незачем.
                    self._postponed.append((feed["title"] or f"лента #{feed_id}",
                                            str(exc)))
                    self.st.add_postponed(
                        feed_id=feed_id, key=key, title=entry.title, summary=entry.summary,
                        link=entry.link, published=entry.published, image=entry.image,
                        error=str(exc),
                    )
                    continue
                if moderation_now:
                    text = self._fallback_text(entry, feed)
                else:
                    post = await self._fallback_post(entry, feed)

            if debug:
                # Прочитанным не помечаем: после /debug off новость уйдёт в канал.
                if await self._send_debug(post, feed):
                    published += 1
            elif moderation_now:
                if await self._queue_for_review(feed, key, entry, text):
                    queued += 1
            else:
                sent = await self._send(post.text, image=post.image, images=post.images)
                if sent:
                    self.st.mark_seen(feed_id, key)
                    # Могла раньше отказать и попасть в очередь «Отложенные» —
                    # теперь прошла (автоматически или после того как админ
                    # починил бэкенд), убираем оттуда.
                    self.st.remove_postponed(feed_id, key)
                    published += 1
                    self._record_post(feed_id, entry, feed, post, sent)
                    # После отметки о прочтении: новость уже не повторится, значит
                    # и в VK не задвоится, даже если он сейчас недоступен.
                    await self.send_vk(post)

            if self._blocked:
                break

            # post_delay защищает канал от пачки сообщений подряд — при
            # постановке в очередь согласования в канал ничего не уходит,
            # ждать между записями незачем.
            if i < len(to_post) - 1 and delay and not moderation_now:
                await asyncio.sleep(delay)

        handled = published + queued
        if not debug:
            # Остались необработанные новости — обрезали по max_per_cycle, не
            # смогли доставить, пропустили из-за отказа модели или очередь
            # согласования заполнена. На следующем проходе обойдём условный
            # GET, иначе 304 спрячет их до тех пор, пока лента сама не обновится.
            updates["pending"] = int(handled < len(fresh))

        self.st.update_feed(feed_id, **updates)
        if handled and not debug:
            self.st.prune_seen(feed_id, max(50, self.st.get_int("keep_seen")))
        return published, queued

    async def build_post_text(self, entry: Entry, feed: sqlite3.Row | None = None,
                              force_backend: "LLMClient | ClaudeClient | None" = None) -> str:
        """Прогоняет запись через шаблон + LLM — только текст, без картинок.

        Вынесено из build_post отдельно ради очереди согласования
        (_queue_for_review): в ней текст нужен сразу, а байты картинок —
        только после одобрения (см. _image_candidates/_download_candidates) —
        не тратить трафик на новости, которые ещё могут отклонить."""
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
        ai_text = await self._ask_model(prompt, force_backend=force_backend)

        post_format = self.st.get("post_format")
        return _shorten(render(post_format, {**raw_values, "ai": ai_text}, escape=True))

    async def build_post(self, entry: Entry, feed: sqlite3.Row | None = None,
                         force_backend: "LLMClient | ClaudeClient | None" = None) -> Post:
        """Прогоняет запись через шаблон + LLM и собирает готовое сообщение.

        force_backend — см. _complete: используется /claude test и /gemini
        test, чтобы прогнать запись именно через тестируемый бэкенд, не
        трогая общую настройку claude_mode/gemini_mode."""
        text = await self.build_post_text(entry, feed, force_backend=force_backend)
        if self.multi_images_for(feed):
            image_url, images = await self._images_of_page(entry)
            return Post(text=text, image=image_url, images=images, link=entry.link)
        return Post(text=text, image=await self._image_of(entry), link=entry.link)

    async def rebuild_post_text(self, row: sqlite3.Row, extra: str = "",
                                limit: int | None = None) -> str:
        """Заново прогоняет уже опубликованную новость через модель — /regen.

        Источник тот же, что был при первой публикации (строка из таблицы
        posts, а не свежий запрос к ленте: заголовок мог с тех пор
        измениться на сайте, а редактируем мы историю, а не текущую версию).
        Картинки не трогаем — /regen меняет только текст.

        `limit` — явный лимит длины вместо вывода по row["kind"]: строки
        moderation (очередь согласования) такого столбца не имеют вовсе.
        """
        raw_values = {
            "title": row["title"],
            "summary": row["summary"] or row["title"],
            "link": row["link"],
            "source": row["source"] or "RSS",
            "published": row["published"],
        }
        feed = self.st.feed(row["feed_id"]) if row["feed_id"] else None
        template = (feed["template"] if feed and feed["template"] else "") or self.st.get("template")
        prompt = render(template, raw_values, escape=False)
        if extra.strip():
            prompt += f"\n\nДополнительно учти: {extra.strip()}"
        ai_text = await self._ask_model(prompt)

        post_format = self.st.get("post_format")
        if limit is None:
            limit = TG_CAPTION_LIMIT if row["kind"] in ("photo", "album") else TG_LIMIT
        return _shorten(render(post_format, {**raw_values, "ai": ai_text}, escape=True), limit)

    async def set_model_tested(self, name: str) -> str | None:
        """/setmodel: переключает модель обычного LLM, только если она
        ответила на пробный запрос — иначе откатывает и возвращает текст
        ошибки (None при успехе).

        Своп+тест+откат идут под тем же self._lock, что и run_once — иначе
        два быстрых /setmodel (с двух устройств) или /setmodel во время
        идущего автопрохода могут перечитать/затереть чужое временное
        значение self.llm.model (см. историю гонки, которую это чинит):
        один тест успевает откатить на "previous", снятое ДО того, как
        второй его сменил, теряя чужое уже подтверждённое переключение."""
        async with self._lock:
            previous = self.llm.model
            self.llm.model = name
            try:
                await self.llm.complete("Ответь одним словом: работает")
            except LLMError as exc:
                self.llm.model = previous
                return str(exc)
            return None

    async def delete_post_image(self, post_id: int, message_id: int) -> str | None:
        """Удаляет одну картинку из уже опубликованного альбома (>1 картинки).

        Первая картинка (с подписью-текстом поста) так не удаляется — без
        неё пост потерял бы текст, а перенести подпись на другую картинку
        Telegram не даёт. Убрать можно только вторую и далее.

        Возвращает None при успехе, иначе текст ошибки для показа админу.
        """
        row = self.st.post(post_id)
        if row is None:
            return "Пост не найден."
        if message_id not in self.st.post_extra_ids(post_id):
            return "Такой картинки в этом посте нет — возможно, уже удалена."
        try:
            await self.bot.delete_message(chat_id=row["chat_id"], message_id=message_id)
        except TelegramBadRequest as exc:
            if "message to delete not found" not in str(exc).lower():
                return f"Telegram отказал: {exc}"
            # Сообщение и так уже не существует (удалили вручную) — у себя всё равно чистим.
        except TelegramAPIError as exc:
            return f"Ошибка Telegram: {exc}"
        self.st.remove_post_extra_id(post_id, message_id)
        return None

    def _record_post(self, feed_id: int, entry: Entry, feed: sqlite3.Row | None,
                     post: Post, sent: "Message | list[Message]") -> None:
        """Запоминает опубликованный пост — чтобы его можно было найти и
        отредактировать (/posts, /edit, /setpost, /regen)."""
        messages = sent if isinstance(sent, list) else [sent]
        first = messages[0]
        if len(messages) > 1:
            kind = "album"
        elif first.photo:
            kind = "photo"
        else:
            kind = "text"
        self.st.add_post(
            feed_id=feed_id,
            chat_id=str(first.chat.id),
            message_id=first.message_id,
            kind=kind,
            title=entry.title,
            summary=entry.summary,
            link=entry.link,
            source=(feed["title"] if feed and feed["title"] else "") or "RSS",
            published=entry.published,
            text=post.text,
            # Остальные картинки альбома — только их message_id, у подписи
            # (в тексте поста) есть лишь первая; хранится для точечного
            # удаления отдельной картинки (/delimage, веб-панель).
            extra_message_ids=",".join(str(m.message_id) for m in messages[1:]),
        )

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

    async def search_articles(self, domain: str, path: str) -> tuple[list[dict], str | None]:
        """Кандидаты в статьи с сайта без RSS — от ВСЕХ настроенных
        источников поиска разом, слитые в один список без дублей по ссылке
        (см. search.merge_search_results). Используют оба места, которым
        нужен список статей сайта: автоцикл (_fetch_search ниже) и ручные
        команды (handlers._last_entry для /test и др., cmd_addsite,
        web.feeds_add_search) — раньше туда ходил только Serper, теперь то
        же самое видят все точки входа, а не только автопубликация.

        Serper (google.serper.dev, платный после бесплатного лимита) и Bing
        News RSS (bing.com/news/search, бесплатный публичный RSS без ключа)
        запрашиваются оба — они индексируют сайты независимо и с разной
        задержкой, проверено на практике: конкретную свежую статью на
        warhammer-community.com Serper/Google ещё не видел вообще, а Bing
        News уже отдавал первой в выдаче с точной датой публикации. Один
        источник может не знать сайт вовсе, ошибиться или оказаться
        недоступен — ошибка возвращается, только если ОБА источника
        подвели; если хотя бы один дал результат, до него дело не доходит.
        """
        query = site_query(domain, path)
        errors: list[str] = []
        results: list[list[dict]] = []

        if self.search is not None and self.search.configured:
            items, error = await self.search.search(query)
            if error:
                errors.append(f"Serper: {error}")
            else:
                results.append(items)

        if self.bing is not None:
            items, error = await self.bing.search(query)
            if error:
                errors.append(f"Bing: {error}")
            else:
                results.append(items)

        if not results:
            return [], "; ".join(errors) or "поиск не настроен"
        return merge_search_results(*results), None

    async def _fetch_search(self, feed: sqlite3.Row) -> FetchResult:
        """Источник без RSS: новые статьи ищем через веб-поиск (bot/search.py)
        вместо разбора ленты — сайт может отдавать устаревший кэш на
        собственных страницах со списком новостей, поиск от этого не
        зависит (см. SETUP.md, раздел «Сайты без RSS», история вопроса).

        Часть источников поиска (Bing) отдаёт настоящую дату публикации
        прямо в выдаче — её используем как есть. У кого нет (Serper) —
        раздаём псевдо-даты по порядку самой выдачи (первая строго новее
        второй и т.д.), просто чтобы список был хоть как-то отсортирован до
        дочитывания страниц. Выдача поисковика ранжирована по релевантности,
        а не по свежести — эти псевдо-даты ненадёжны как признак «что
        новее» (вчерашняя, но более популярная статья может обойти
        сегодняшнюю). Настоящую дату _hydrate_search_entries() достаёт со
        страницы самой статьи (fetch_article_entry, datePublished) и
        пересортировывает список по ней — псевдо-даты отсюда живут только
        до этого момента. is_seen решает вопрос повторов сам по себе, без
        опоры на даты вообще.
        """
        path = feed["article_path"]
        items, error = await self.search_articles(domain_of(feed["url"]), path)
        if error:
            return FetchResult(entries=[], error=error)

        now = time.time()
        entries: list[Entry] = []
        seen_links: set[str] = set()
        # Порядок исходной выдачи — от самого релевантного к наименее (для
        # голого site:-запроса это обычно свежее/популярнее сперва), а Entry
        # везде в этом коде идут от старых к новым, поэтому разворачиваем.
        for i, item in enumerate(reversed(items)):
            link = item.get("link") or ""
            if not link or (path and path not in link):
                continue
            if link in seen_links:
                continue
            seen_links.add(link)
            ts = item.get("published_ts")
            if ts is None:
                ts = now - (len(items) - i)
            entries.append(Entry(key_parts=(link,), title="", link=link, summary="",
                                 published="", published_ts=ts, image=""))
        return FetchResult(entries=entries)

    async def _hydrate_search_entries(self, fresh: list[tuple[str, Entry]]
                                      ) -> list[tuple[str, Entry]]:
        """Источник без RSS (search) даёт только адрес — заголовок, описание
        и картинку дочитываем со страницы самой статьи, по одной странице
        за раз (не параллельно — это чужой сайт, не наш CDN, незачем бить
        по нему пачкой запросов). К этому моменту список уже прорежен
        is_seen/backfill/max_age/flood_guard, так что за проход это обычно
        считанные страницы, а не вся выдача поиска.

        fetch_article_entry() при этом подменяет published_ts на настоящую
        дату со страницы статьи (если нашлась) — входной порядок list всё
        ещё по релевантности поисковой выдачи, не по свежести, поэтому
        досортировываем по факту, чтобы дальше по конвейеру (max_per_cycle,
        «последняя запись» и т.д.) действительно бралась самая новая, а не
        просто первая по выдаче.
        """
        out: list[tuple[str, Entry]] = []
        for key, entry in fresh:
            full = await fetch_article_entry(entry.link, entry.published_ts, entry.published)
            if full is None:
                log.info("статья не прочиталась, отложена: %s", entry.link[:90])
                continue
            out.append((key, full))
        out.sort(key=lambda pair: pair[1].published_ts)
        return out

    def _find_duplicate(self, entry: Entry, candidates: "list[sqlite3.Row | dict]"
                        ) -> tuple[int | None, float] | None:
        """Из уже опубликованных за окно дедупа постов (и уже принятых в
        этом же проходе, см. _drop_duplicates) — тот, что больше всего
        похож на entry по заголовку и summary разом, или None, если ничего
        не дотягивает до dedup_threshold. id у результата — None, если
        совпадение нашлось с записью из этого же прохода, а не с реальным
        постом (matched_post_id ссылаться там не на что). Оба сигнала должны пройти
        DEDUP_MIN_SIGNAL по отдельности, иначе общие слова вроде
        «анонсировала» жёстко завышали бы схожесть у пары никак не
        связанных новостей.

        Это правило само по себе пропускало настоящие дубли: разные сайты
        почти всегда пишут summary своими словами, и его схожесть падает
        ниже DEDUP_MIN_SIGNAL даже когда заголовки явно об одном и том же
        товаре (проверено на практике — warhammer-community.com и
        ontabletop.com про один и тот же «Captain on Bike»: заголовки
        совпали на 0.5, а summary — только на 0.12, запись не отсеивалась
        и публиковалась дважды). Поэтому вдобавок к порогу по обоим
        сигналам считаем дублем и то, что прошло _shares_named_run —
        общее собственное название анонса в заголовках, не требуя
        похожести summary вовсе.

        `candidates` — результат recent_posts(), считанный один раз в
        _drop_duplicates на всю пачку fresh, а не заново на каждую запись:
        при первом опросе ленты (десятки записей разом) это было N
        одинаковых SQL-запросов вместо одного.
        """
        threshold = max(1, min(100, self.st.get_int("dedup_threshold"))) / 100
        best: tuple[int | None, float] | None = None
        best_named: tuple[int | None, float] | None = None
        for row in candidates:
            title_sim = _dedup_similarity(entry.title, row["title"])
            summary_sim = _dedup_similarity(entry.summary, row["summary"])
            named = _shares_named_run(entry.title, row["title"])
            if (title_sim < DEDUP_MIN_SIGNAL or summary_sim < DEDUP_MIN_SIGNAL) and not named:
                continue
            score = (title_sim + summary_sim) / 2
            if best is None or score > best[1]:
                best = (row["id"], score)
            if named and (best_named is None or score > best_named[1]):
                best_named = (row["id"], score)
        if best is not None and best[1] >= threshold:
            return best
        if best_named is not None:
            # Общее название анонса надёжнее среднего по словам — считаем
            # дублем, даже если сам score ниже настроенного порога, но
            # показываем его как минимум на уровне порога, чтобы не сбивать
            # админа заниженным процентом в очереди на разбор.
            return (best_named[0], max(best_named[1], threshold))
        return None

    def _drop_duplicates(self, feed: sqlite3.Row, fresh: list[tuple[str, Entry]]
                         ) -> list[tuple[str, Entry]]:
        """Отсеивает записи, похожие на уже опубликованный пост — с другой
        ленты или под другим guid этой же.

        Разные источники часто пишут об одном и том же анонсе своими
        словами, а точное совпадение ссылки/guid (is_seen) такое не ловит.
        Найденный дубль не публикуется сам — уходит в очередь на ручной
        разбор (веб-панель, раздел «Ленты»): можно опубликовать, если
        совпадение ложное, или удалить, если дубль настоящий.
        """
        if self.st.get("dedup_enabled") != "1" or not fresh:
            return fresh
        feed_id = feed["id"]
        days = max(1, self.st.get_int("dedup_window_days"))
        since = int(time.time() - days * 86400)
        candidates = list(self.st.recent_posts(since))
        # Очередь согласования (если включена) — тоже кандидаты, с id=None,
        # той же семантикой, что и kept_in_batch ниже: карточка ещё не
        # реальный пост (matched_post_id сослаться некуда), совпадение просто
        # откладывает запись на следующий проход, не публикуя её вторично.
        # Раньше дедуп сверялся только с уже ОПУБЛИКОВАННЫМИ постами — та же
        # новость с другой ленты, пока первая ещё только ждёт согласования
        # (не обязательно секунды — модерация может стоять часами), дедуп не
        # ловил вовсе, и она либо второй раз вставала на согласование, либо
        # (без согласования) уходила в канал дублем.
        if self.moderation:
            candidates += [{"id": None, "title": r["title"], "summary": r["summary"]}
                          for r in self.st.moderation_titles()]
        keep: list[tuple[str, Entry]] = []
        # Записи, уже принятые в этом же проходе, тоже кандидаты на дубль —
        # иначе две похожие записи, разом пришедшие с одной ленты за один
        # опрос (например, анонс и апдейт того же события через /addsite),
        # обе проходили бы мимо dedup: ни одна из них ещё не in posts.
        # id=None у таких кандидатов отличает «дубль внутри пачки» (нет
        # реального опубликованного поста, на который можно сослаться) от
        # обычного матча по posts — см. ветку ниже.
        kept_in_batch: list[dict] = []
        for key, entry in fresh:
            match = self._find_duplicate(entry, candidates + kept_in_batch)
            if match is None:
                keep.append((key, entry))
                kept_in_batch.append({"id": None, "title": entry.title, "summary": entry.summary})
                continue
            post_id, score = match
            if post_id is None:
                # Дубль внутри пачки — сослаться в очереди /duplicates не на
                # что (сама «оригинальная» запись ещё не опубликована, id
                # поста появится только после этого прохода). НЕ помечаем
                # прочитанной: если сходство ложное (два разных анонса с
                # похожими заголовками), запись просто вернётся на следующем
                # опросе и сравнится уже против реального опубликованного
                # поста — тогда матч попадёт в очередь на разбор нормально,
                # с рабочей кнопкой «опубликовать всё же», а не потеряется
                # безвозвратно.
                log.info("лента #%s: %r — похоже на дубль другой записи из этой же "
                         "пачки, откладываю на следующий проход", feed_id, entry.title[:80])
                continue
            self.st.mark_seen(feed_id, key)
            self.st.add_dedup_candidate(
                feed_id=feed_id, title=entry.title, summary=entry.summary,
                link=entry.link, source=feed["title"] or "", published=entry.published,
                image=entry.image, matched_post_id=post_id, score=score,
            )
            self._postponed_dupes.append((feed_id, entry.title, post_id, score))
        if len(keep) < len(fresh):
            log.info("лента #%s: похоже на дубль — %s записей отправлено на "
                     "ручной разбор", feed_id, len(fresh) - len(keep))
        return keep

    async def publish_now(self, entry: Entry, feed: sqlite3.Row | None) -> str | None:
        """Публикует запись немедленно, в обход обычного цикла — для дублей,
        которые админ решил опубликовать вручную (веб-панель, раздел «Ленты»).

        Если включено ручное согласование — не публикует напрямую, а ставит
        в очередь на согласование (см. _queue_for_review): «опубликовать
        всё же» у дубля значит «совпадение ложное, обработай как обычную
        новость», а не «пропусти согласование», которое админ явно включил
        для всех новостей разом. Публикует напрямую только если feed
        неизвестен (лента-источник дубля уже удалена) — у moderation.feed_id
        нет смысла без реальной ленты, а это достаточно редкий случай, чтобы
        не усложнять ради него схему таблицы.

        Если включена отладка (/debug) — действие целиком отключено: раньше
        `not self.debug` в условии moderation_now означало, что при
        одновременно включённых отладке и согласовании функция тихо уходила
        в ветку прямой публикации — то есть ПРЯМО В КАНАЛ, а не в личку и не
        в очередь, вопреки обоим режимам разом. Отдельная ветка «превью в
        личку вместо канала», как у обычного цикла (_send_debug), сюда не
        подходит: она рассчитана на непрочитанные записи основного цикла, а
        здесь запись уже помечена прочитанной/удалена из очереди дублей —
        то есть надо не превью показать, а именно ничего не публиковать.
        Возвращает None при успехе, иначе текст ошибки.
        """
        if self.debug:
            return ("Сейчас включена отладка (/debug) — публикация отсюда выключена, "
                     "чтобы не уйти в канал мимо неё по ошибке. Выключите отладку "
                     "(/debug off) и повторите.")
        lock_key = f"dedup:{entry_key(*entry.key_parts)}"
        if lock_key in self._manual_publish_locks:
            return "Уже публикуется — подождите и обновите страницу."
        self._manual_publish_locks.add(lock_key)
        try:
            moderation_now = self.moderation and feed is not None
            try:
                if moderation_now:
                    text = await self.build_post_text(entry, feed)
                else:
                    post = await self.build_post(entry, feed)
            except LLMError as exc:
                return f"Модель вернула ошибку: {exc}"
            if moderation_now:
                key = entry_key(*entry.key_parts)
                item_id = await self._queue_for_review(feed, key, entry, text)
                if item_id is None:
                    return "Уже в очереди согласования — обновите страницу."
                return None
            sent = await self._send(post.text, image=post.image, images=post.images)
            if not sent:
                return "Не удалось опубликовать — канал недоступен или не задан."
            self._record_post(feed["id"] if feed else None, entry, feed, post, sent)
            await self.send_vk(post)
            return None
        finally:
            self._manual_publish_locks.discard(lock_key)

    async def retry_postponed(self, item_id: int) -> str | None:
        """Повторная попытка одной записи из очереди «Отложенные» (веб-панель,
        раздел «Ленты») — не дожидаясь следующего автоматического цикла.

        Если включено ручное согласование — успешный результат идёт не в
        канал напрямую, а в очередь на согласование (см. _queue_for_review):
        "Повторить сейчас" на карточке отказа модели означает "обработай
        новость ещё раз", а не "и обойди согласование, которое включено
        для всех новостей". Раньше эта функция всегда публиковала напрямую,
        из-за чего одобренная модель после смены (например, /setmodel
        сразу после отказа) публиковала новость в канал мимо очереди.

        Если включена отладка (/debug) — действие целиком отключено (см.
        подробное объяснение в publish_now — та же причина: раньше
        одновременно включённые отладка и согласование тихо публиковали
        прямо в канал, обходя оба режима).

        Возвращает None при успехе, иначе текст ошибки — запись остаётся в
        очереди с обновлённым счётчиком попыток."""
        if self.debug:
            return ("Сейчас включена отладка (/debug) — публикация отсюда выключена, "
                     "чтобы не уйти в канал мимо неё по ошибке. Выключите отладку "
                     "(/debug off) и повторите.")
        lock_key = f"postponed:{item_id}"
        if lock_key in self._manual_publish_locks:
            return "Уже публикуется — подождите и обновите страницу."
        self._manual_publish_locks.add(lock_key)
        try:
            row = self.st.postponed_item(item_id)
            if row is None:
                return "Запись не найдена — возможно, уже обработана."
            feed_id, key = row["feed_id"], row["key"]
            feed = self.st.feed(feed_id)
            entry = Entry(key_parts=(key,), title=row["title"], link=row["link"],
                          summary=row["summary"], published=row["published"], published_ts=0,
                          image=row["image"])
            moderation_now = self.moderation and feed is not None
            try:
                if moderation_now:
                    text = await self.build_post_text(entry, feed)
                else:
                    post = await self.build_post(entry, feed)
            except LLMError as exc:
                self.st.add_postponed(
                    feed_id=feed_id, key=key, title=row["title"], summary=row["summary"],
                    link=row["link"], published=row["published"], image=row["image"],
                    error=str(exc),
                )
                return f"Модель снова отказала: {exc}"
            if moderation_now:
                # _queue_for_review сам делает mark_seen/remove_postponed —
                # отдельно здесь их вызывать не нужно.
                new_item_id = await self._queue_for_review(feed, key, entry, text)
                if new_item_id is None:
                    return "Уже в очереди согласования — обновите страницу."
                return None
            sent = await self._send(post.text, image=post.image, images=post.images)
            if not sent:
                return "Не удалось опубликовать — канал недоступен или не задан."
            self.st.mark_seen(feed_id, key)
            self.st.remove_postponed(feed_id, key)
            self._record_post(feed_id, entry, feed, post, sent)
            await self.send_vk(post)
            return None
        finally:
            self._manual_publish_locks.discard(lock_key)

    # --- очередь ручного согласования (self.moderation) --------------------
    async def _queue_for_review(self, feed: sqlite3.Row, key: str, entry: Entry,
                                text: str) -> int | None:
        """Ставит готовый пост в очередь согласования вместо публикации.
        Возвращает id новой карточки, либо None при гонке (уже стояла —
        см. Storage.add_moderation, UNIQUE(feed_id, key))."""
        feed_id = feed["id"]
        multi = self.multi_images_for(feed)
        if multi:
            limit = max(1, min(10, self.st.get_int("max_images")))
            candidates = await self._image_candidates(entry)
            image, extra = (candidates[0], candidates[1:limit]) if candidates else ("", [])
        else:
            image, extra = await self._image_of(entry), []
        item_id = self.st.add_moderation(
            feed_id=feed_id, key=key, title=entry.title, summary=entry.summary,
            link=entry.link, source=feed["title"] or "", published=entry.published,
            text=text, image=image, extra_images="\n".join(extra), multi=multi,
        )
        if item_id is None:
            # Гонка (уже стояла с тем же feed_id+key) — но раньше здесь просто
            # выходили, НЕ помечая прочитанной. Обычно это неважно: запись и
            # так уже помечена прочитанной с первой постановки в очередь.
            # Но prune_seen чистит старые отметки по времени отдельно от
            # moderation_keep_days — если карточка провисела в очереди дольше
            # окна prune_seen (админ надолго забыл про неё), запись «протухает»
            # в seen, следующий опрос ленты видит её снова как новую, тратит
            # вызов модели заново и снова упирается в этот же UNIQUE-конфликт
            # — и так каждый проход, пока карточку не разберут. Помечаем
            # прочитанной здесь тоже, чтобы разорвать этот цикл.
            self.st.mark_seen(feed_id, key)
            return None
        # Главное отличие от postponed: помечаем прочитанной СРАЗУ — текст
        # уже сгенерирован моделью, повторная оценка на следующем проходе
        # только зря потратила бы лимит и завела бы вторую карточку.
        self.st.mark_seen(feed_id, key)
        self.st.remove_postponed(feed_id, key)
        self._queued.append((feed["title"] or f"лента #{feed_id}", entry.title, item_id))
        return item_id

    @staticmethod
    def _moderation_entry(row: sqlite3.Row) -> Entry:
        """Восстанавливает Entry из строки moderation — для _record_post
        (дедуп между лентами сравнивает по posts, которые строятся из Entry).
        key_parts ни на что не влияет: mark_seen уже сделан при постановке
        в очередь, повторно эта запись не понадобится."""
        return Entry(key_parts=(f"moderation:{row['id']}",), title=row["title"],
                     link=row["link"], summary=row["summary"], published=row["published"],
                     published_ts=0, image=row["image"])

    async def publish_moderated(self, item_id: int, actor: str = "web") -> str | None:
        """Одобряет и публикует карточку очереди согласования. Текст берётся
        как есть из очереди (в т.ч. отредактированный админом) — заново
        модель НЕ дёргаем, админ одобрял именно то, что видел.
        Возвращает None при успехе, иначе текст ошибки."""
        if not self.st.claim_moderation(item_id, actor, PUBLISH_CLAIM_TTL):
            return "Эту новость уже публикует или опубликовал кто-то другой."
        row = self.st.moderation_item(item_id)
        if row is None:
            return "Запись не найдена — возможно, уже обработана."
        feed = self.st.feed(row["feed_id"])
        image, images = row["image"], []
        if row["multi"]:
            urls = [u for u in [row["image"], *row["extra_images"].split("\n")] if u]
            limit = max(1, min(10, self.st.get_int("max_images")))
            image, images = await self._download_candidates(urls, row["link"], limit)
        post = Post(text=row["text"], image=image, images=images, link=row["link"])
        sent = await self._send(post.text, image=post.image, images=post.images)
        if not sent:
            self.st.release_moderation(item_id, "канал недоступен или не задан")
            return "Не удалось опубликовать — канал недоступен или не задан."
        entry = self._moderation_entry(row)
        self._record_post(row["feed_id"], entry, feed, post, sent)
        await self.send_vk(post)
        self.st.delete_moderation(item_id)
        return None

    async def run_scheduled_publishes(self) -> int:
        """Публикует карточки очереди согласования, для которых наступило
        время, заданное админом («Сегодня/Завтра» + время на карточке, см.
        веб-панель). Вызывается из run_once — до проверки паузы/отладки
        и на каждом проходе (включая /checknow), поэтому расписание
        соблюдается с точностью до интервала опроса, не только раз в сутки.
        Возвращает число реально опубликованных."""
        due_ids = self.st.moderation_due_ids(int(time.time()))
        published = 0
        failed: list[tuple[int, str]] = []
        for item_id in due_ids:
            error = await self.publish_moderated(item_id, actor="scheduled")
            if error:
                log.warning("отложенная публикация карточки #%s не удалась: %s", item_id, error)
                failed.append((item_id, error))
            else:
                published += 1
        if failed:
            # Раньше только в journalctl — админ узнавал о провале отложенной
            # публикации, только если сам догадался открыть очередь: карточка
            # остаётся в 'queued' с прошедшим scheduled_at, но НИКАК не
            # напоминает о себе (пока не наступит следующий _check_moderation_
            # reminder по общим правилам очереди, что может быть часами позже).
            await self._report_scheduled_publish_failed(failed)
        return published

    async def _report_scheduled_publish_failed(self, failed: list[tuple[int, str]]) -> None:
        """Троттлинг как у _report_queue_overflow: постоянная ошибка (канал
        недоступен) иначе повторялась бы на каждом проходе опроса и
        заваливала бы админа сообщениями чаще, чем раз в ALERT_EVERY."""
        if not self.admin_ids:
            return
        now = int(time.time())
        try:
            last = int(self.st.get("scheduled_fail_alert_at") or 0)
        except ValueError:
            last = 0
        if now - last < ALERT_EVERY:
            return
        self.st.set("scheduled_fail_alert_at", now)
        lines = [f"• #{item_id} — {error}" for item_id, error in failed[:10]]
        more = f"\n…и ещё {len(failed) - 10}" if len(failed) > 10 else ""
        text = (f"⚠️ Не удалось опубликовать по расписанию {len(failed)} карточек — "
               f"остались в очереди согласования, план снят автоматически не будет "
               f"(попробуют снова на следующем проходе, если ошибка временная):\n"
               + "\n".join(lines) + more)
        for admin_id in sorted(self.admin_ids):
            try:
                await self.bot.send_message(chat_id=admin_id, text=text, parse_mode="HTML")
            except TelegramAPIError as exc:
                log.warning("не удалось сообщить админу %s о провале отложенной публикации: %s",
                            admin_id, exc)

    async def regen_moderated(self, item_id: int, extra: str = "") -> str | None:
        """Перегенерировать текст карточки в очереди через ИИ — в отличие от
        /posts/{id}/regen у уже опубликованных, пишет прямо в moderation.text
        и не нуждается в черновике-в-памяти с TTL: карточка ещё не
        опубликована, сохранять «случайно» нечего."""
        row = self.st.moderation_item(item_id)
        if row is None:
            return "Запись не найдена — возможно, уже обработана."
        try:
            text = await self.rebuild_post_text(row, extra, limit=TG_LIMIT)
        except LLMError as exc:
            return f"Модель вернула ошибку: {exc}"
        self.st.update_moderation_text(item_id, text)
        return None

    async def preview_moderated(self, item_id: int) -> str | None:
        """Присылает пост из очереди согласования в личку админам «как есть»
        — посмотреть, как он будет выглядеть в канале, до одобрения. Ничего
        не публикует и не меняет карточку."""
        row = self.st.moderation_item(item_id)
        if row is None:
            return "Запись не найдена — возможно, уже обработана."
        if not self.admin_ids:
            return "Нет ни одного администратора для предпросмотра."
        image, images = row["image"], []
        if row["multi"]:
            urls = [u for u in [row["image"], *row["extra_images"].split("\n")] if u]
            limit = max(1, min(10, self.st.get_int("max_images")))
            image, images = await self._download_candidates(urls, row["link"], limit)
        header = "👁 <b>Предпросмотр</b> — так пост будет выглядеть в канале, пока не опубликовано."
        for admin_id in sorted(self.admin_ids):
            await self._send(header, chat_id=admin_id)
            await self._send(row["text"], chat_id=admin_id, image=image, images=images)
        return None

    async def _complete(self, prompt: str,
                        force_backend: "LLMClient | ClaudeClient | None" = None) -> str:
        """Один запрос через активный бэкенд — с автопереключением на
        основной LLM, если у Gemini кончилась квота (HTTP 429). Без этого
        каждая новость откладывалась бы до ручного /gemini off: квота
        освобождается только на следующие сутки, а посты вставали в очередь
        прямо сейчас.

        force_backend — для /claude test и /gemini test: конкретный клиент
        вместо активного сейчас бэкенда, без переключения общей настройки
        claude_mode/gemini_mode. Настройка — общее состояние, которое видит
        и параллельный автопроход, и другой админ; временно дёргать её ради
        одного тестового вызова означало бы, что реальные новости в этом же
        окне уйдут через тестируемый (платный) бэкенд без ведома админа, а
        отмена по finally могла бы затереть чужое изменение той же настройки,
        сделанное как раз в этот момент. Раз бэкенд задан явно — это тест,
        а не публикация, и автопереключение с Gemini тоже ни к чему: ошибку
        нужно показать как есть, а не тихо подменить результат основной моделью."""
        llm = force_backend or self._active_llm
        try:
            return await llm.complete(prompt)
        except LLMQuotaExceeded:
            if force_backend is not None or llm is not self.gemini:
                raise
            self.st.set("gemini_mode", "0")
            log.warning("Gemini: квота исчерпана (HTTP 429) — переключаюсь "
                        "на основной LLM (%s) автоматически", self.llm.model)
            await self._report_gemini_quota()
            return await self.llm.complete(prompt)

    async def _ask_model(self, prompt: str,
                         force_backend: "LLMClient | ClaudeClient | None" = None) -> str:
        """Ответ модели, пригодный к публикации.

        Модель иногда возвращает исходник как есть, проигнорировав просьбу
        перевести. Публиковать такое — всё равно что не обработать новость,
        поэтому даём ей второй заход с прямым указанием, и лишь потом
        признаём отказ.
        """
        if self.st.get("require_russian") != "1":
            return await self._complete(prompt, force_backend=force_backend)

        nudge = ("\n\nВажно: ответ должен быть на русском языке. "
                 "Не копируй исходный текст.")
        for attempt in range(1, RU_ATTEMPTS + 1):
            text = await self._complete(prompt if attempt == 1 else prompt + nudge,
                                        force_backend=force_backend)
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

    async def _image_candidates(self, entry: Entry) -> list[str]:
        """Адреса картинок-кандидатов со страницы новости, БЕЗ скачивания
        байт — общая первая половина _images_of_page, вынесена отдельно ради
        очереди согласования (_queue_for_review): там до одобрения нужны
        только адреса (см. _download_candidates — скачивание происходит
        отдельно, только когда/если новость реально публикуется)."""
        if self.st.get("images") != "1" or not entry.link:
            return []
        limit = max(1, min(10, self.st.get_int("max_images")))
        # Кандидатов берём с запасом сверх limit: часть ссылок не скачается
        # (сайт не ответил, оказалось не картинкой, CDN отдал 403) — без
        # запаса такие неудачи оставили бы пост с картинками меньше, чем
        # настроено, хотя на странице их хватало с избытком.
        pool = limit + IMAGE_DOWNLOAD_CONCURRENCY
        candidates = [entry.image] if entry.image else []
        seen_keys = {image_dedup_key(u) for u in candidates}
        for u in await page_images(entry.link, limit=pool):
            key = image_dedup_key(u)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            candidates.append(u)
        return candidates

    @staticmethod
    async def _download_candidates(candidates: list[str], referer: str, limit: int
                                   ) -> tuple[str, list[tuple[bytes, str]]]:
        """Скачивает картинки-кандидаты пачками, возвращает адрес первой
        (для VK, см. ниже) и байты успешно скачанных, не больше limit."""
        out: list[tuple[bytes, str]] = []
        first_url = ""
        # Качаем пачками по IMAGE_DOWNLOAD_CONCURRENCY штук параллельно, а не
        # все сразу и не строго по одной: параллель внутри пачки ощутимо
        # быстрее последовательной загрузки, а остановка сразу по достижении
        # limit не тратит трафик на кандидатов, которые уже не понадобятся.
        for i in range(0, len(candidates), IMAGE_DOWNLOAD_CONCURRENCY):
            if len(out) >= limit:
                break
            batch = candidates[i:i + IMAGE_DOWNLOAD_CONCURRENCY]
            # return_exceptions=True: одна оборвавшаяся закачка (например,
            # CancelledError при остановке бота) не должна валить остальные
            # параллельные закачки этой же пачки и прерывать обработку записи.
            results = await asyncio.gather(
                *(download_image(url, referer=referer) for url in batch),
                return_exceptions=True,
            )
            for url, downloaded in zip(batch, results):
                if downloaded is None or isinstance(downloaded, BaseException):
                    log.info("картинка не скачалась, пропускаю: %s", url[:100])
                    continue
                out.append(downloaded)
                if not first_url:
                    first_url = url
        return first_url, out[:limit]

    async def _images_of_page(self, entry: Entry) -> tuple[str, list[tuple[bytes, str]]]:
        """Несколько картинок со страницы новости — режим «несколько картинок»
        включается для конкретной ленты (/feedimages <id> on), работает при
        любом активном бэкенде текста.

        В отличие от _image_of (одна картинка, может остаться просто ссылкой),
        здесь качаем сами: несколько ссылок из разных источников надёжнее
        отправлять байтами, чем адресами за нестабильными CDN. Возвращает
        ещё и адрес первой картинки отдельной строкой — VK грузит фото по
        ссылке, а не байтами (см. VKClient.post), и без этого в режиме
        нескольких картинок публикация в VK оставалась совсем без фото.
        """
        if self.st.get("images") != "1" or not entry.link:
            return "", []
        limit = max(1, min(10, self.st.get_int("max_images")))
        candidates = await self._image_candidates(entry)
        return await self._download_candidates(candidates, entry.link, limit)

    def _fallback_text(self, entry: Entry, feed: sqlite3.Row | None) -> str:
        """Если LLM недоступна — аккуратная заготовка без обработки, только текст."""
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
        return _shorten(render(self.st.get("post_format"), values, escape=True))

    async def _fallback_post(self, entry: Entry, feed: sqlite3.Row | None) -> Post:
        text = self._fallback_text(entry, feed)
        if self.multi_images_for(feed):
            image_url, images = await self._images_of_page(entry)
            return Post(text=text, image=image_url, images=images, link=entry.link)
        return Post(text=text, image=await self._image_of(entry), link=entry.link)

    async def send_vk(self, post: Post) -> bool:
        """Дублирует пост в сообщество VK. Ошибку только логируем.

        VK — второй адресат, а не главный: если он отвалился, публикация в
        Telegram уже состоялась и новость помечена прочитанной. Картинки
        альбома (post.images) переиспользуем как есть — уже скачаны для
        Telegram, повторно с CDN источника их не тянем.
        """
        if not self.vk_on:
            return False
        self.vk.group_id = self.vk_group
        try:
            post_id = await self.vk.post(to_plain(post.text), post.image, post.link,
                                         images=post.images)
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
                    image: str = "", images: list[tuple[bytes, str]] | None = None
                    ) -> "Message | list[Message] | None":
        """Возвращает отправленное сообщение (список — для альбома) или None.

        Тип возврата не bool, чтобы вызывающий код мог записать post_id для
        дальнейшего редактирования (/edit, /setpost, /regen) — но истинность
        (truthy/falsy) та же, что и раньше, так что весь код вида
        `if await self._send(...)` продолжает работать без изменений.
        """
        target = chat_id if chat_id is not None else self.channel
        if not target:
            log.error("канал не задан: укажите CHANNEL_ID или /setchannel")
            self._blocked = True
            return None

        images = images or []
        fits_caption = tg_len(text) <= TG_CAPTION_LIMIT

        if len(images) >= 2:
            sent = await self._send_media_group(text if fits_caption else "", target, images[:10])
            if sent:
                if fits_caption:
                    return sent
                # Подпись длиннее лимита медиагруппы — картинки уже ушли,
                # текст отдельным сообщением следом. Если это отдельное
                # сообщение не отправится — альбом всё равно живой, публикацию
                # считаем состоявшейся (иначе следующий проход прислал бы
                # тот же альбом повторно).
                tail = await self._send(text, chat_id)
                return tail or sent
            if self._blocked:
                return None
            # Альбом не отправился (например Telegram отверг один из файлов) —
            # пробуем хотя бы первую картинку одиночным фото.
            images = images[:1]

        single: str | tuple[bytes, str] | None = images[0] if images else (image or None)
        # Пост с картинкой уходит фото с подписью — но подпись у Telegram
        # вчетверо короче сообщения, поэтому длинные посты отправляем текстом,
        # а картинку прикладываем превью-ссылкой, чтобы не резать содержимое.
        if single and fits_caption:
            sent = await self._send_photo(text, target, single)
            if sent:
                return sent
            if self._blocked:
                return None
        preview = self._preview(image)
        for _ in range(3):
            try:
                return await self.bot.send_message(
                    chat_id=target,
                    text=text,
                    parse_mode="HTML",
                    link_preview_options=preview,
                )
            except TelegramRetryAfter as exc:
                log.warning("лимит Telegram, ждём %ss", exc.retry_after)
                await asyncio.sleep(exc.retry_after + 1)
            except TelegramNetworkError as exc:
                # Таймаут/обрыв соединения — не то же самое, что отказ
                # доступа: Telegram мог и получить сообщение, просто ответ до
                # нас не дошёл. Не считаем канал недоступным (_blocked) и не
                # обрываем весь проход из-за одного сетевого сбоя — пробуем
                # ещё раз в пределах уже существующего бюджета попыток.
                log.warning("сетевая ошибка при отправке в %s (%s), пробую ещё раз",
                            target, exc)
                await asyncio.sleep(2)
            except TelegramBadRequest as exc:
                if not self._is_markup_error(exc):
                    log.error("не удалось отправить в %s: %s", target, exc)
                    self._blocked = True
                    return None
                # Разметку Telegram не принял (обычно из-за /setformat).
                # Публикуем текстом, иначе новость зависнет и будет
                # переобрабатываться моделью каждый проход.
                log.error("Telegram отверг разметку (%s) — отправляю без "
                          "форматирования; проверьте /format", exc)
                return await self._send_plain(strip_html(text), target)
            except TelegramAPIError as exc:
                log.error("не удалось отправить в %s: %s", target, exc)
                self._blocked = True
                return None
        log.error("не удалось отправить в %s — повторяющиеся сетевые ошибки; "
                  "новость останется непрочитанной и будет обработана заново "
                  "на следующем проходе (если предыдущая попытка на самом "
                  "деле дошла до Telegram, возможен дубль в канале)", target)
        return None

    async def _send_photo(self, text: str, target: int | str,
                          image: str | tuple[bytes, str]) -> "Message | None":
        """Фото по URL или уже скачанным байтам. None — картинка не подошла,
        пост уйдёт отдельно."""
        photo = image if isinstance(image, str) else BufferedInputFile(
            image[0], filename=f"image.{_ext_for(image[1])}")
        label = image if isinstance(image, str) else f"{len(image[0])} байт, {image[1]}"
        for _ in range(3):
            try:
                return await self.bot.send_photo(
                    chat_id=target,
                    photo=photo,
                    caption=text,
                    parse_mode="HTML",
                )
            except TelegramRetryAfter as exc:
                log.warning("лимит Telegram, ждём %ss", exc.retry_after)
                await asyncio.sleep(exc.retry_after + 1)
            except TelegramNetworkError as exc:
                # См. комментарий в _send — сетевой сбой не значит отказ,
                # пробуем ещё раз, а не сразу падаем на текстовый фолбэк
                # (который иначе рискует задвоить уже доставленное фото).
                log.warning("сетевая ошибка при отправке фото в %s (%s), пробую ещё раз",
                            target, exc)
                await asyncio.sleep(2)
            except TelegramBadRequest as exc:
                if self._is_markup_error(exc):
                    # Разметка сломана — картинка ни при чём, пусть общий путь
                    # разбирается и публикует текстом.
                    return None
                if self._is_delivery_error(exc):
                    log.error("не удалось отправить в %s: %s", target, exc)
                    self._blocked = True
                    return None
                log.warning("Telegram не принял картинку %s (%s) — публикую "
                            "пост без фото", label, exc)
                return None
            except TelegramAPIError as exc:
                log.warning("ошибка отправки фото в %s (%s) — публикую пост "
                            "без фото", target, exc)
                return None
        return None

    async def _send_media_group(self, caption: str, target: int | str,
                                images: list[tuple[bytes, str]]) -> "list[Message] | None":
        """Альбом из 2-10 картинок (режим Claude). Подпись — только на первой,
        так её показывает сам Telegram. None — не отправился целиком, пост
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
                sent = await self.bot.send_media_group(chat_id=target, media=media)
                return list(sent)
            except TelegramRetryAfter as exc:
                log.warning("лимит Telegram, ждём %ss", exc.retry_after)
                await asyncio.sleep(exc.retry_after + 1)
            except TelegramNetworkError as exc:
                # См. комментарий в _send — сетевой сбой не значит отказ,
                # пробуем ещё раз, а не сразу падаем на фолбэк (который
                # иначе рискует задвоить уже доставленный альбом).
                log.warning("сетевая ошибка при отправке альбома в %s (%s), пробую ещё раз",
                            target, exc)
                await asyncio.sleep(2)
            except TelegramBadRequest as exc:
                if self._is_delivery_error(exc):
                    log.error("не удалось отправить в %s: %s", target, exc)
                    self._blocked = True
                    return None
                log.warning("Telegram не принял альбом (%s) — пробую иначе", exc)
                return None
            except TelegramAPIError as exc:
                log.warning("ошибка отправки альбома в %s (%s) — пробую иначе",
                            target, exc)
                return None
        return None

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

    async def _send_plain(self, text: str, target: int | str) -> "Message | None":
        try:
            return await self.bot.send_message(chat_id=target, text=_shorten(text),
                                                parse_mode=None)
        except TelegramAPIError as exc:
            log.error("не удалось отправить даже без разметки в %s: %s", target, exc)
            return None
