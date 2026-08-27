"""Загрузка и разбор RSS/Atom-лент."""
from __future__ import annotations

import asyncio
import calendar
import html
import logging
import re
import urllib.error
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser

import aiohttp
import feedparser
from urllib.parse import urljoin, urlsplit

USER_AGENT = "rss-deepseek-bot/1.0 (+https://github.com/)"
FETCH_TIMEOUT = 20

# Дочитывание картинки со страницы новости — для лент, где её нет в самой
# записи. Сайты кладут og:image в <head>, поэтому дальше него не читаем.
PAGE_TIMEOUT = 12
PAGE_LIMIT = 192 * 1024
# Под видом бота многие CMS отдают урезанную страницу без метатегов.
PAGE_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

log = logging.getLogger(__name__)


class _TextExtractor(HTMLParser):
    """Выдирает текст из HTML-описания записи."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("p", "br", "div", "li"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def strip_html(raw: str) -> str:
    if not raw:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(raw)
        parser.close()
        text = "".join(parser.parts)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", raw)
        text = html.unescape(text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


@dataclass(slots=True)
class Entry:
    key_parts: tuple[str, ...]
    title: str
    link: str
    summary: str
    published: str
    published_ts: float
    image: str = ""

    @property
    def is_empty(self) -> bool:
        return not (self.title or self.summary)


@dataclass(slots=True)
class FetchResult:
    entries: list[Entry]          # от старых к новым
    feed_title: str = ""
    etag: str | None = None
    modified: str | None = None
    not_modified: bool = False
    error: str | None = None


def _published(entry) -> tuple[str, float]:
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(field)
        if parsed:
            try:
                # feedparser отдаёт struct_time уже в UTC — timegm, не mktime.
                ts = float(calendar.timegm(parsed))
                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                return dt.strftime("%Y-%m-%d %H:%M UTC"), ts
            except (OverflowError, ValueError):
                continue
    for field in ("published", "updated"):
        if entry.get(field):
            return str(entry[field]), 0.0
    return "", 0.0


def _summary(entry, limit: int = 4000) -> str:
    candidates: list[str] = []
    content = entry.get("content")
    if content:
        candidates.extend(c.get("value", "") for c in content)
    candidates.append(entry.get("summary", ""))
    candidates.append(entry.get("description", ""))
    best = max((strip_html(c) for c in candidates if c), key=len, default="")
    return best[:limit]


_IMG_RE = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.I)
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")
# Счётчики и разделители попадаются в теле записи первым <img> и уезжали бы
# в пост вместо иллюстрации.
TRACKER_HINTS = ("pixel", "1x1", "spacer", "blank.gif", "doubleclick",
                 "feedburner", "/ad/", "counter", "stat.", "beacon")
MIN_IMAGE_SIDE = 100


def _is_image(url: str, mime: str | None = None, medium: str | None = None) -> bool:
    if not url.lower().startswith(("http://", "https://")):
        return False
    if any(hint in url.lower() for hint in TRACKER_HINTS):
        return False
    if mime:
        return mime.lower().startswith("image/")
    if medium:
        return medium.lower() == "image"
    path = url.split("?", 1)[0].split("#", 1)[0].lower()
    return path.endswith(IMAGE_EXT)


def _too_small(item: dict) -> bool:
    """Отсекает объявленные в ленте миниатюры-счётчики."""
    for side in ("width", "height"):
        raw = str(item.get(side) or "").strip().rstrip("px")
        if raw.isdigit() and int(raw) < MIN_IMAGE_SIDE:
            return True
    return False


def _image(entry) -> str:
    """Первая пригодная картинка записи: media:* → enclosure → <img> в теле."""
    for field in ("media_content", "media_thumbnail"):
        for item in entry.get(field) or []:
            url = html.unescape(str(item.get("url") or "").strip())
            if url and not _too_small(item) and _is_image(
                url, item.get("type"), item.get("medium")
            ):
                return url

    for enc in list(entry.get("enclosures") or []) + list(entry.get("links") or []):
        if enc.get("rel") not in (None, "enclosure"):
            continue
        url = html.unescape(str(enc.get("href") or enc.get("url") or "").strip())
        if url and _is_image(url, enc.get("type")):
            return url

    image = entry.get("image")
    if isinstance(image, dict):
        url = html.unescape(str(image.get("href") or image.get("url") or "").strip())
        if url and _is_image(url):
            return url

    # Последняя попытка — разметка описания: <img> там почти всегда есть.
    bodies = [c.get("value", "") for c in (entry.get("content") or [])]
    bodies += [entry.get("summary", ""), entry.get("description", "")]
    for body in bodies:
        if not body:
            continue
        for match in _IMG_RE.finditer(body):
            url = html.unescape(match.group(1).strip())
            # У <img> тип не объявлен, поэтому берём и адреса без расширения:
            # CMS часто отдают картинки через /image/12345 без суффикса.
            if _is_image(url) or url.lower().startswith(("http://", "https://")):
                if not any(hint in url.lower() for hint in TRACKER_HINTS):
                    return url
    return ""


def _parse_sync(url: str, etag: str | None, modified: str | None) -> FetchResult:
    try:
        parsed = feedparser.parse(
            url, etag=etag or None, modified=modified or None, agent=USER_AGENT
        )
    except Exception as exc:  # сеть/парсер — не роняем цикл
        return FetchResult(entries=[], error=f"{type(exc).__name__}: {exc}")

    status = parsed.get("status")
    if status == 304:
        return FetchResult(entries=[], not_modified=True)
    if status and status >= 400:
        return FetchResult(entries=[], error=f"HTTP {status}")
    if parsed.get("bozo") and not parsed.entries:
        exc = parsed.get("bozo_exception")
        if isinstance(exc, (urllib.error.URLError, OSError)):
            return FetchResult(entries=[], error=f"лента недоступна: {exc}")
        return FetchResult(entries=[], error=f"не удалось разобрать ленту: {exc}")

    entries: list[Entry] = []
    for raw in parsed.entries:
        link = (raw.get("link") or "").strip()
        title = strip_html(raw.get("title", "")) or "(без заголовка)"
        published, ts = _published(raw)
        # Ключ строим по guid, если он есть: заголовки новостных лент правят
        # уже после публикации, и любая правка означала бы повторный пост.
        guid = str(raw.get("id") or raw.get("guid") or "").strip()
        entries.append(
            Entry(
                key_parts=(guid,) if guid else (link, title),
                title=title,
                link=link,
                summary=_summary(raw),
                published=published,
                published_ts=ts,
                image=_image(raw),
            )
        )

    # Свежие записи в фидах идут первыми — переворачиваем, чтобы публиковать по порядку.
    if any(e.published_ts for e in entries):
        entries.sort(key=lambda e: e.published_ts)
    else:
        entries.reverse()

    return FetchResult(
        entries=entries,
        feed_title=strip_html(parsed.feed.get("title", "")) if parsed.get("feed") else "",
        etag=parsed.get("etag"),
        modified=parsed.get("modified"),
    )


_META_RE = re.compile(r"<meta\b[^>]*>", re.I)
_ATTR_RE = re.compile(r"""(property|name|content)\s*=\s*("([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)
_LINK_IMG_RE = re.compile(
    r"""<link\b[^>]*rel\s*=\s*["']?image_src["']?[^>]*>""", re.I)
_HREF_RE = re.compile(r"""href\s*=\s*("([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)
# Порядок важен: og:image — то, что показывают соцсети, остальное запасное.
META_KEYS = ("og:image:secure_url", "og:image:url", "og:image",
             "twitter:image", "twitter:image:src")

_session: aiohttp.ClientSession | None = None


def _http() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=PAGE_TIMEOUT),
            headers={"User-Agent": PAGE_UA},
        )
    return _session


async def close_http() -> None:
    global _session
    if _session and not _session.closed:
        await _session.close()
    _session = None


# Заголовок и описание статьи для источников без RSS (см. fetch_article_entry
# ниже) — те же самые метатеги, которыми соцсети рисуют превью ссылки.
TITLE_KEYS = ("og:title", "twitter:title")
DESCRIPTION_KEYS = ("og:description", "twitter:description", "description")


def _meta_tags(head: str, keys: tuple[str, ...] = META_KEYS) -> dict[str, str]:
    """Метатеги страницы, отфильтрованные по нужным ключам — страницу
    разбираем за один проход, какой бы набор ключей ни спрашивали."""
    found: dict[str, str] = {}
    for tag in _META_RE.finditer(head):
        attrs: dict[str, str] = {}
        for m in _ATTR_RE.finditer(tag.group(0)):
            value = m.group(3) or m.group(4) or m.group(5) or ""
            attrs[m.group(1).lower()] = value
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        if key in keys and attrs.get("content") and key not in found:
            found[key] = attrs["content"]
    return found


def _meta_images(head: str) -> dict[str, str]:
    return _meta_tags(head, META_KEYS)


async def page_image(url: str) -> str | None:
    """Картинка со страницы новости: og:image и родня.

    Вызывается только для записей, где ленты картинку не дали, и только для
    тех, что вот-вот уйдут в канал — не для всей выдачи. Читаем максимум
    PAGE_LIMIT байт и обрываемся на </head>, дальше в странице искать нечего.

    Возвращает адрес картинки; "" — страницу прочли, картинки на ней нет;
    None — прочесть не удалось (сайт ответил ошибкой, притормозил нас или
    не отозвался). Разница важна для кэша: «нет картинки» запоминаем
    навсегда, а неудачу — нет, иначе один 429 лишил бы ленту картинок.
    """
    if not url.lower().startswith(("http://", "https://")):
        return ""
    try:
        async with _http().get(url) as resp:
            if resp.status != 200:
                log.debug("страница %s ответила HTTP %s", url[:80], resp.status)
                return None
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if ctype and "html" not in ctype:
                return ""
            buf = bytearray()
            async for chunk in resp.content.iter_chunked(16 * 1024):
                buf += chunk
                if b"</head" in buf.lower() or len(buf) >= PAGE_LIMIT:
                    break
    except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeDecodeError):
        return None
    except Exception:                      # разбор чужого HTML — дело мутное
        log.debug("не удалось прочитать страницу %s", url, exc_info=True)
        return None

    head = buf.decode("utf-8", "replace")
    metas = _meta_images(head)
    candidates = [metas[k] for k in META_KEYS if k in metas]
    if link := _LINK_IMG_RE.search(head):
        if href := _HREF_RE.search(link.group(0)):
            candidates.append(href.group(2) or href.group(3) or href.group(4) or "")

    for raw in candidates:
        candidate = urljoin(url, html.unescape(raw.strip()))
        if _is_image(candidate) or candidate.lower().startswith(("http://", "https://")):
            if not any(hint in candidate.lower() for hint in TRACKER_HINTS):
                return candidate
    return ""


# Все картинки со страницы новости — режим «несколько картинок» (см.
# publisher.py, Publisher.multi_images_for): в отличие от page_image() читаем
# страницу целиком, а не только <head>, и собираем несколько картинок вместо
# одной. Дороже по времени и трафику, чем обычный путь, поэтому включается
# отдельным тумблером, а не работает всегда.
ARTICLE_LIMIT = 1024 * 1024
MAX_ARTICLE_IMAGES = 6

# «Тело» страницы кроме самой статьи содержит меню сайта, виджет «похожие
# статьи», аватар автора и т.п. — без фильтрации в пост уходят чужие
# картинки, никак не связанные с новостью. Три независимых сигнала:
#
# 1. LANDMARK_TAGS — <header>/<nav>/<footer>/<aside> размечают чужеродные
#    для статьи блоки семантически, это самый надёжный сигнал.
# 2. Картинка внутри ссылки на ДРУГУЮ страницу (не картинку-первоисточник
#    для лайтбокса и не саму статью) — почти всегда карточка «читать
#    также», а не иллюстрация текущей новости.
# 3. CHROME_CONTEXT_HINTS — точечные подстраховки под конкретные названия
#    классов виджетов, которых не бывает у landmark-тегов и ссылок.
LANDMARK_TAGS = ("header", "nav", "footer", "aside")
CHROME_URL_HINTS = (
    "/wp-content/themes/", "gravatar.com", "/wp-includes/",
    # warhammer-community.com: карточки виджета «читать также по теме» —
    # ссылка на них уже закрыта (</a>) к моменту <img>, анкорную проверку
    # не проходит (см. комментарий в _body_images), только по URL и ловится.
    "explore-more-cards",
    # То же самое: логотипы разделов сайта (уголок каждой карточки «читать
    # также», плюс флаг локали в шапке) — общие статичные картинки в корне
    # assets-хоста, лежат не под /articles/, ни к одной статье не относятся.
    "assets.warhammer-community.com/uk-flag.png",
    "assets.warhammer-community.com/warhammer40000.png",
    "assets.warhammer-community.com/warhammerageofsigmar.png",
    # warhammer-community.com: баннер подписки на рассылку в подвале статьи —
    # адрес картинки сам содержит "newsletter", по URL ловится надёжнее, чем
    # по контексту (до него от текста "Sign up to the newsletter" дальше,
    # чем _CONTEXT_WINDOW).
    "newsletter",
)
CHROME_CONTEXT_HINTS = (
    "breaking-news", "breaking-thumb", "entry-preview", "post-preview",
    "article-card", "blog__post", "related-post", "related_post",
    "you-might-also-like", "you-may-also-like", "recommended-post",
    "trending", "popular-post", "widget", "sidebar", "mega-menu",
    "comment-respond", "author-bio", "author-box", "newsletter",
    "social-share", "share-buttons",
    # warhammer-community.com: декоративный баннер раздела (Age of Sigmar /
    # 40k / Kill Team...) в шапке статьи — тот же для всех статей раздела,
    # не имеет отношения к конкретной новости.
    "gamesystemshero",
    # warhammer-community.com: промо-карточки «в продаже сейчас» / подписка
    # на Warhammer+ в подвале статьи (class="shared-ctaCard"/"article-ctaCards") —
    # витрина магазина, не иллюстрация новости.
    "ctacard",
    # warhammer-community.com: за карточкой explore-more-cards/*.jpg (уже
    # ловится по URL выше) сразу идёт лого игровой системы того же виджета
    # (killteam.png, necromunda....png и т.п.) — у самого лого в URL этой
    # подстроки уже нет, только в соседнем теге чуть выше по разметке.
    "explore-more-cards",
    # warhammer-community.com: тот же виджет "исследуйте другие игровые
    # системы", но не у всех слайдов фоновая картинка лежит под
    # /explore-more-cards/ — например Warhammer Underworlds отдаёт
    # /whuw_exploremore-oct2024-*.jpg прямо в корне assets-хоста, и хинт
    # по URL выше его не ловит. Зато class="...aspect-[725/350] object-cover..."
    # на самом теге <img> — общий для фона ЛЮБОГО слайда этого виджета
    # независимо от того, под какой путь легла картинка конкретной системы.
    "aspect-[725/350]",
    # То же самое для лого игровой системы поверх фона слайда (killteam.png,
    # necromunda....png, whuw_700wide...png) — общий class этой карточки,
    # не завязан на конкретное имя файла лого.
    "h-110 object-contain",
    # warhammer-community.com: класс на <img> в любой карточке-превью статьи
    # (виджеты "More from...", "More Warhammer 40,000 news" и т.п., рендерятся
    # компонентом <Image> Next.js). Проверка по дате-папке (_date_folder) не
    # спасает, когда виджет "последние новости" включает саму текущую статью —
    # у её карточки в разметке та же неделя wcDD-MM, что и у настоящих
    # иллюстраций. Собственные картинки статьи из тела всегда обычный
    # <img src=...> без class вообще (см. _body_images) — этот признак их
    # не задевает.
    "object-cover-absolute",
)
# warhammer-community.com переиспользует одну и ту же React-компоненту
# карточки для РАЗНЫХ виджетов — "shared-articleCard" (похожие статьи),
# "shared-ctaCard" (промо магазина), "shared-gameSystemCard" (карусель
# Swiper "другие игровые системы") — и явно каждый новый находить себе
# дороже, чем поймать саму конвенцию имени класса один раз. Тело статьи
# такой class никогда не носит (см. комментарий у object-cover-absolute).
_SHARED_CARD_RE = re.compile(r"\bshared-\w*card\b", re.I)
# Было 500 — карточка внутри карточки (лого поверх фона слайда внутри
# shared-gameSystemCard) на warhammer-community.com отодвигает открывающий
# тег с классом на 700+ символов назад лишними обёрточными <div>. Проверено:
# у настоящих иллюстраций статьи (обычный <img src=...> в вёрстке
# WYSIWYG-тела) даже окно в 1000 символов назад не подхватывает ни одного
# из хинтов CHROME_CONTEXT_HINTS/_SHARED_CARD_RE — ложных срабатываний не даёт.
_CONTEXT_WINDOW = 1000

_TOKEN_RE = re.compile(
    r"<a\b[^>]*?\bhref\s*=\s*[\"']([^\"']*)[\"'][^>]*>"
    r"|</a\s*>"
    r"|<(header|nav|footer|aside)\b"
    r"|</(header|nav|footer|aside)\s*>"
    r"|<img\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"']",
    re.I,
)
# CDN часто отдают одно и то же фото в нескольких размерах (обычный
# WordPress-суффикс -1024x627 перед расширением) — без нормализации такие
# варианты дублируются в посте как будто это разные картинки.
_RESIZE_SUFFIX_RE = re.compile(r"-\d{2,5}x\d{2,5}(?=\.\w+$)")
# /2026/08/ в пути — почти всегда папка загрузки CMS по дате публикации:
# у статей из виджета «похожие материалы» дата (и потому папка) почти
# всегда другая, у картинок текущей статьи — та же.
_DATE_FOLDER_RE = re.compile(r"/(\d{4}/\d{1,2})/")
# warhammer-community.com не использует /YYYY/MM/ — вместо этого папка вида
# /0-2026/august/wc24-08/ ("week commencing"). Без этого второго паттерна
# _date_folder() возвращал None для ЛЮБОЙ картинки этого сайта, primary_folder
# ни разу не выставлялся, и весь фильтр по дате-папке ниже был выключен —
# из-за этого в пост вместе со своими попадали три-четыре картинки из виджета
# «More from Warhammer 40,000» (другие статьи, другой wcDD-MM).
_WC_FOLDER_RE = re.compile(r"/(wc\d{2}-\d{2})/", re.I)


def image_dedup_key(url: str) -> str:
    """Ключ для сравнения «на самом деле одна и та же картинка»."""
    path = url.split("?", 1)[0].split("#", 1)[0]
    path = re.sub(r"^https?://", "", path, flags=re.I)
    path = _RESIZE_SUFFIX_RE.sub("", path)
    return path.lower()


def _date_folder(url: str) -> str | None:
    m = _DATE_FOLDER_RE.search(url)
    if m:
        return m.group(1)
    m = _WC_FOLDER_RE.search(url)
    return m.group(1).lower() if m else None


def _same_page(href_abs: str, article_url: str) -> bool:
    a, b = urlsplit(href_abs), urlsplit(article_url)
    return a.path.rstrip("/") == b.path.rstrip("/")


def _body_images(page: str, article_url: str, primary_folder: str | None) -> list[str]:
    """Картинки из <img> тела страницы, в порядке появления, без дублей и
    без чужеродных (см. комментарий перед LANDMARK_TAGS выше)."""
    seen: set[str] = set()
    out: list[str] = []
    anchor_stack: list[str] = []
    landmark_depth = 0
    for m in _TOKEN_RE.finditer(page):
        if m.group(1) is not None:            # <a href=...>
            anchor_stack.append(m.group(1))
            continue
        if m.group(0).startswith("</a"):       # </a>
            if anchor_stack:
                anchor_stack.pop()
            continue
        if m.group(2) is not None:             # <header|nav|footer|aside>
            landmark_depth += 1
            continue
        if m.group(3) is not None:             # закрывающий тег
            landmark_depth = max(0, landmark_depth - 1)
            continue
        if landmark_depth > 0:
            continue

        raw = html.unescape(m.group(4).strip())
        # JS-шаблон вида {{ data.image.url }}, не отрендерился на сервере.
        if not raw or "{" in raw or "}" in raw:
            continue
        url = urljoin(article_url, raw)

        if anchor_stack and not anchor_stack[-1].strip().lower().startswith("javascript:"):
            # javascript: — открывает лайтбокс через обработчик клика, а не
            # переход на другую страницу; urljoin() иначе резолвил бы такой
            # href как отдельную «чужую» страницу и ронял свою же картинку.
            href_abs = urljoin(article_url, anchor_stack[-1])
            href_path = href_abs.split("#")[0]
            # Ссылка на полноразмерную версию той же картинки (лайтбокс) —
            # не признак карточки «читать также», это своя иллюстрация.
            if (href_path and not _is_image(href_path)
                    and not _same_page(href_abs, article_url)):
                continue

        if any(hint in url.lower() for hint in TRACKER_HINTS):
            continue
        if any(hint in url.lower() for hint in CHROME_URL_HINTS):
            continue
        if not (_is_image(url) or url.lower().startswith(("http://", "https://"))):
            continue
        if primary_folder and _date_folder(url) not in (None, primary_folder):
            continue

        # context — текст СТРОГО ДО <img (родительские теги вида
        # <figure class="..."> или <article class="shared-articleCard">).
        # own_attrs — атрибуты самого тега ДО src (например class="..." перед
        # src="..." в одном <img>) — m.start() стоит на "<img", а не на "src",
        # поэтому свой собственный class тега в context не попадает вообще,
        # только в own_attrs. Без own_attrs карточка виджета "последние
        # новости", которая включает саму текущую статью (та же неделя
        # wcDD-MM, дата-фильтр не спасает), проходила бы неотличимо от
        # настоящей иллюстрации: у обеих <img> нет ни оборачивающего тега,
        # ни ссылки-предка в пределах _CONTEXT_WINDOW.
        context = page[max(0, m.start() - _CONTEXT_WINDOW):m.start()].lower()
        own_attrs = m.group(0).lower()
        if any(hint in context or hint in own_attrs for hint in CHROME_CONTEXT_HINTS):
            continue
        if _SHARED_CARD_RE.search(context) or _SHARED_CARD_RE.search(own_attrs):
            continue

        key = image_dedup_key(url)
        if key in seen:
            continue
        seen.add(key)
        out.append(url)
    return out


async def page_images(url: str, limit: int = MAX_ARTICLE_IMAGES) -> list[str]:
    """Все подходящие картинки со страницы новости: в приоритете свои
    иллюстрации из тела статьи — это буквально то, что видел читатель.
    og:image — запасной вариант, только если в теле не нашлось ничего:
    на части сайтов (например ontabletop.com/beastsofwar.com) og:image —
    отдельно смонтированная обложка для соцсетей, которой в самом тексте
    статьи нет вообще, и смешивать её с настоящими иллюстрациями значит
    добавлять в пост картинку, которой не было в новости.
    """
    if not url.lower().startswith(("http://", "https://")):
        return []
    try:
        async with _http().get(url) as resp:
            if resp.status != 200:
                log.debug("страница %s ответила HTTP %s", url[:80], resp.status)
                return []
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if ctype and "html" not in ctype:
                return []
            buf = bytearray()
            async for chunk in resp.content.iter_chunked(32 * 1024):
                buf += chunk
                if len(buf) >= ARTICLE_LIMIT:
                    break
    except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeDecodeError):
        return []
    except Exception:
        log.debug("не удалось прочитать страницу %s", url, exc_info=True)
        return []

    page = buf.decode("utf-8", "replace")
    head_end = page.lower().find("</head")
    head = page[:head_end] if head_end != -1 else page

    metas = _meta_images(head)
    meta_ordered: list[str] = []
    meta_seen: set[str] = set()
    primary_folder: str | None = None
    for key in META_KEYS:
        if key not in metas:
            continue
        candidate = urljoin(url, html.unescape(metas[key].strip()))
        if any(h in candidate.lower() for h in TRACKER_HINTS):
            continue
        if not (_is_image(candidate) or candidate.lower().startswith(("http://", "https://"))):
            continue
        dedup_key = image_dedup_key(candidate)
        if dedup_key in meta_seen:
            continue
        meta_seen.add(dedup_key)
        meta_ordered.append(candidate)
        if primary_folder is None:
            primary_folder = _date_folder(candidate)

    # <link rel="image_src"> — тот же источник обложки, что и в page_image(),
    # но там отдельная (более дешёвая, только <head>) функция для режима
    # «одна картинка»; здесь читаем всю страницу и без этой проверки сайты,
    # где обложка объявлена только через <link>, без og:image/twitter:image,
    # в режиме «несколько картинок» её бы не находили вовсе.
    if link := _LINK_IMG_RE.search(head):
        if href := _HREF_RE.search(link.group(0)):
            raw = href.group(2) or href.group(3) or href.group(4) or ""
            candidate = urljoin(url, html.unescape(raw.strip()))
            if (not any(h in candidate.lower() for h in TRACKER_HINTS)
                    and (_is_image(candidate) or candidate.lower().startswith(("http://", "https://")))):
                dedup_key = image_dedup_key(candidate)
                if dedup_key not in meta_seen:
                    meta_seen.add(dedup_key)
                    meta_ordered.append(candidate)
                    if primary_folder is None:
                        primary_folder = _date_folder(candidate)

    # primary_folder нужен _body_images ещё до решения, чьи картинки в итоге
    # пойдут в пост — он отсекает чужие статьи из виджета «похожие материалы».
    body_ordered: list[str] = []
    seen: set[str] = set()
    for candidate in _body_images(page, url, primary_folder):
        if len(body_ordered) >= limit:
            break
        dedup_key = image_dedup_key(candidate)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        body_ordered.append(candidate)

    return body_ordered or meta_ordered[:limit]


# Telegram сам режет фото крупнее 10 МБ при отправке (sendPhoto/sendMediaGroup);
# качать больше этого — тратить трафик на то, что всё равно не уйдёт в канал.
MAX_IMAGE_BYTES = 10 * 1024 * 1024


async def download_image(url: str, referer: str = "") -> tuple[bytes, str] | None:
    """Скачивает картинку по прямой ссылке. None — не удалось (сеть, не картинка,
    слишком большая). Заголовки браузера и Referer нужны так же, как в vk.py —
    часть CDN без них отвечает 403/451.
    """
    headers = {
        "User-Agent": PAGE_UA,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    try:
        async with _http().get(url, headers=headers) as resp:
            if resp.status != 200:
                return None
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
            if ctype and not ctype.startswith("image/"):
                return None
            chunks: list[bytes] = []
            size = 0
            async for chunk in resp.content.iter_chunked(64 * 1024):
                size += len(chunk)
                if size > MAX_IMAGE_BYTES:
                    return None
                chunks.append(chunk)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None
    data = b"".join(chunks)
    if not data:
        return None
    return data, ctype or "image/jpeg"


async def fetch(url: str, etag: str | None = None, modified: str | None = None) -> FetchResult:
    """feedparser блокирующий — уносим в тред."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_parse_sync, url, etag, modified), timeout=FETCH_TIMEOUT + 10
        )
    except asyncio.TimeoutError:
        return FetchResult(entries=[], error="таймаут загрузки ленты")


# ─── Источники без RSS: обнаружение через веб-поиск ────────────────────────
#
# У сайта нет RSS/Atom — а собственные средства сайта узнать «что нового»
# (sitemap.xml, страница списка новостей) могут отдавать устаревший снимок
# из-за кэша CDN, который не обходится обычными HTTP-приёмами (см. историю
# в SETUP.md — на warhammer-community.com sitemap.xml оказался закэширован
# на 50+ часов, и ни параметры против кэша, ни заголовки no-cache, ни
# служебные заголовки Next.js это не обходят). Обнаружение самих новых
# адресов теперь на bot/search.py (веб-поиск, не зависит от кэша сайта);
# здесь остаётся только дочитывание заголовка/описания/картинки со страницы
# уже найденной статьи — отдельные страницы статей у таких сайтов, в
# отличие от их списков, как правило кэшируются нормально и открываются
# свежими.
ARTICLE_FETCH_TIMEOUT = 20
_TITLE_TAG_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
# datePublished из JSON-LD Article (schema.org) — общепринятая разметка у
# новостных сайтов, не специфична для конкретного источника. Ищем по всей
# странице, не только в <head>: на практике этот блок нередко лежит в теле
# страницы (проверено на warhammer-community.com).
_JSONLD_DATE_RE = re.compile(r'"datePublished"\s*:\s*"([^"]+)"', re.I)


def _parse_iso_date(raw: str) -> float | None:
    """ISO 8601 (только дата или дата+время, с таймзоной или без) → unix ts,
    либо None, если формат не разобрать. Дата без времени — предполагаем
    полночь UTC: не идеально точно, но для сравнения «что новее» между
    статьями одного дня разница в часы неважна."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


async def _fetch_text(url: str, headers: dict | None = None) -> tuple[int, str] | None:
    """(статус, тело) или None — сеть подвела."""
    try:
        async with _http().get(url, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=ARTICLE_FETCH_TIMEOUT)) as resp:
            status = resp.status
            buf = bytearray()
            async for chunk in resp.content.iter_chunked(64 * 1024):
                buf += chunk
                if len(buf) >= ARTICLE_LIMIT:
                    break
    except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeDecodeError):
        return None
    return status, buf.decode("utf-8", "replace")


async def fetch_article_entry(url: str, published_ts: float, published: str) -> Entry | None:
    """Заголовок, описание и картинка со страницы статьи — источник без RSS
    даёт только адрес (см. bot/search.py), остальное только на странице
    самой новости. None — страница не прочиталась или на ней нет заголовка
    (не статья — например снятая с публикации страница).

    `published_ts`/`published` — то, что передал вызывающий код (обычно
    синтетическая метка по порядку выдачи поиска, см. Publisher._fetch_search
    и его комментарий). Если на странице статьи нашёлся datePublished —
    подменяем на настоящую дату: поиск ранжирует по релевантности, а не по
    свежести, и первый результат выдачи может оказаться вчерашней, но более
    popular статьёй, а не сегодняшней — так уже бывало на практике.
    """
    got = await _fetch_text(url, {"User-Agent": PAGE_UA})
    if got is None:
        return None
    status, page = got
    if status != 200:
        return None

    if m := _JSONLD_DATE_RE.search(page):
        real_ts = _parse_iso_date(m.group(1))
        if real_ts is not None:
            published_ts = real_ts

    head_end = page.lower().find("</head")
    head = page[:head_end] if head_end != -1 else page
    metas = _meta_tags(head, TITLE_KEYS + DESCRIPTION_KEYS + META_KEYS)

    title = ""
    for key in TITLE_KEYS:
        if metas.get(key):
            title = html.unescape(metas[key]).strip()
            break
    if not title:
        m = _TITLE_TAG_RE.search(head)
        title = strip_html(m.group(1)) if m else ""
    if not title:
        return None

    summary = ""
    for key in DESCRIPTION_KEYS:
        if metas.get(key):
            summary = html.unescape(metas[key]).strip()
            break

    image = ""
    for key in META_KEYS:
        if not metas.get(key):
            continue
        candidate = urljoin(url, html.unescape(metas[key].strip()))
        if _is_image(candidate) or candidate.lower().startswith(("http://", "https://")):
            image = candidate
            break

    return Entry(key_parts=(url,), title=title, link=url, summary=summary,
                published=published, published_ts=published_ts, image=image)
