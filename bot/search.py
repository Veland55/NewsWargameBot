"""Обнаружение свежих статей через веб-поиск — для источников без RSS, у
которых собственный кэш сайта (sitemap.xml, страница списка новостей и
подобное) отдаёт устаревший снимок, никак не обходимый обычными HTTP-
средствами (см. историю в SETUP.md, раздел «Сайты без RSS»: на
warhammer-community.com sitemap.xml оказался закэширован на CDN на 54+
часа, ни параметры против кэша, ни заголовки no-cache, ни служебные
заголовки Next.js это не обходят — а поисковик индексирует те же страницы
куда быстрее).

Поиск через Serper.dev (google.serper.dev) — обёртка над выдачей Google,
проще в настройке, чем официальный Google Custom Search JSON API: тот
закрыт для новых клиентов (не появляется в списке API Google Cloud даже
после включения) и вовсе отключается 1 января 2027. У Serper — обычная
регистрация по email, один ключ API, 2500 бесплатных запросов без карты,
дальше ~$1 за 1000 запросов. Нужен ключ API, см. SETUP.md.
"""
from __future__ import annotations

import asyncio
import calendar
import logging
from urllib.parse import parse_qs, unquote, urlsplit

import aiohttp

log = logging.getLogger(__name__)

API_URL = "https://google.serper.dev/search"
TIMEOUT = 15


def domain_of(url: str) -> str:
    """Домен ленты для поискового запроса. Фолбэк на голый url на случай
    экзотического значения без схемы — сама лента уже когда-то прошла
    валидацию при добавлении (см. cmd_addsite/feeds_add_search), так что
    пустая строка тут практически не встречается."""
    return urlsplit(url).netloc or url.strip("/")


def site_query(domain: str, article_path: str) -> str:
    """Запрос вида `site:домен[путь]` — общий для добавления источника
    (/addsite, веб-панель) и последующих опросов (Publisher._fetch_search,
    _last_entry для /test и др.), чтобы не разъезжались в четырёх местах."""
    return f"site:{domain}{article_path}" if article_path else f"site:{domain}"


class SearchClient:
    def __init__(self, api_key: str = "", *, timeout: int = TIMEOUT):
        self.api_key = (api_key or "").strip()
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def search(self, query: str, date_restrict: str = "qdr:w",
                     num: int = 10) -> tuple[list[dict], str | None]:
        """(результаты, ошибка). Результат — {title, link, snippet}, в том
        порядке, в котором их вернул поиск (примерно по релевантности —
        для запроса вида site:домен/путь без других слов это на практике
        означает «недавно проиндексированное и популярное» сперва).

        date_restrict — фильтр свежести на стороне поиска (параметр `tbs`
        у Serper): 'qdr:d' — сутки, 'qdr:w' — неделя, 'qdr:m' — месяц. Не
        даёт точную дату публикации, только предфильтрует совсем старое,
        чтобы не тратить запросы к странице статьи на заведомо старое.
        """
        if not self.configured:
            return [], "поиск не настроен: нет ключа API (SERPER_API_KEY)"
        body = {"q": query, "num": max(1, min(10, num))}
        if date_restrict:
            body["tbs"] = date_restrict
        headers = {"X-API-KEY": self.api_key, "Content-Type": "application/json"}
        try:
            session = await self._get_session()
            async with session.post(API_URL, json=body, headers=headers) as resp:
                try:
                    data = await resp.json()
                except (aiohttp.ContentTypeError, ValueError):
                    data = {}
                if resp.status != 200:
                    msg = data.get("message") or data.get("error") or f"HTTP {resp.status}"
                    return [], str(msg)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            return [], f"{type(exc).__name__}: {exc}"

        items = data.get("organic") or []
        # published_ts=None — Serper не даёт даты в самой выдаче вообще;
        # настоящую дату достаём позже со страницы статьи (см.
        # rss.fetch_article_entry, JSON-LD datePublished). Поле здесь только
        # для единообразия формата с BingNewsClient.search() ниже — там
        # дата есть уже в самой выдаче.
        out = [
            {"title": it.get("title", "") or "", "link": it.get("link", "") or "",
             "snippet": it.get("snippet", "") or "", "published_ts": None}
            for it in items if it.get("link")
        ]
        return out, None


BING_NEWS_URL = "https://www.bing.com/news/search"
BING_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _unwrap_bing_link(raw: str) -> str:
    """<link> в Bing News RSS — редирект через apiclick.aspx с настоящим
    адресом статьи в параметре url= (percent-encoded); без разворачивания
    в базу лёг бы адрес трекера, а не статьи."""
    query = urlsplit(raw).query
    real = parse_qs(query).get("url", [""])[0]
    return unquote(real) if real else raw


def _parse_bing_rss(body: str) -> list[dict]:
    """Разбор RSS 2.0 от Bing News — свой мини-парсер, не feedparser: тут
    нужны только title/link/pubDate/description, а Bing иногда отдаёт
    невалидный (не по спеке) XML в description, на котором feedparser
    иногда спотыкается сильнее, чем терпимый к неточностям regex-разбор."""
    import html as _html
    import re

    out: list[dict] = []
    for m in re.finditer(r"<item>(.*?)</item>", body, re.S):
        chunk = m.group(1)

        def tag(name: str) -> str:
            mm = re.search(rf"<{name}>(.*?)</{name}>", chunk, re.S)
            if not mm:
                return ""
            text = mm.group(1).strip()
            if text.startswith("<![CDATA[") and text.endswith("]]>"):
                text = text[9:-3]
            # XML экранирует спецсимволы в тексте (&amp; и т.п.) — без
            # unescape строка вида "...&amp;url=..." не режется на
            # query-параметры: parse_qs ищёт буквальный "&", а не "&amp;".
            return _html.unescape(text)

        link = _unwrap_bing_link(tag("link"))
        if not link:
            continue
        ts: float | None = None
        pub_date = tag("pubDate")
        if pub_date:
            try:
                import email.utils
                parsed = email.utils.parsedate_tz(pub_date)
                if parsed is not None:
                    ts = email.utils.mktime_tz(parsed)
            except (TypeError, ValueError):
                ts = None
        out.append({
            "title": tag("title"),
            "link": link,
            "snippet": tag("description"),
            "published_ts": ts,
        })
    return out


class BingNewsClient:
    """Bing News RSS-поиск (bing.com/news/search?...&format=RSS) — тот же
    смысл, что у SearchClient (обнаружение новых статей для сайтов без
    RSS), но: бесплатно и без ключа (обычный публичный RSS-эндпоинт, не
    официальный платный API), и на практике индексирует некоторые статьи
    заметно быстрее обычной веб-выдачи Google/Serper — проверено на живом
    случае: свежую статью на warhammer-community.com, которую Serper ещё не
    видел вообще, Bing News уже отдавал первой в выдаче, с точной датой
    публикации прямо в pubDate. Используется ВМЕСТЕ с SearchClient
    (Publisher._fetch_search сливает оба источника), не вместо — Bing может
    не индексировать какой-то конкретный сайт вовсе или сам оказаться
    временно недоступен, а Serper для него уже настроен и оплачен.

    Не документированный официально API, а страница, которую отдаёт обычный
    браузер — расплата за бесплатность: может измениться формат ответа или
    начать банить по User-Agent/частоте без предупреждения. Если это
    случится, бот просто продолжит работать на одном Serper, как раньше
    (см. обработку ошибок в _fetch_search — сбой одного источника не роняет
    весь опрос ленты).
    """

    def __init__(self, *, timeout: int = TIMEOUT):
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    @property
    def configured(self) -> bool:
        return True

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def search(self, query: str) -> tuple[list[dict], str | None]:
        """(результаты, ошибка). Результат — {title, link, snippet,
        published_ts} — published_ts берётся из pubDate самой выдачи, в
        отличие от Serper тут не нужно ждать хидрации страницы статьи,
        чтобы узнать настоящую дату (хидрация всё равно происходит дальше
        по конвейеру за заголовком/картинкой, и JSON-LD со страницы, если
        найдётся, всё равно её уточнит/переопределит — см.
        rss.fetch_article_entry)."""
        headers = {"User-Agent": BING_UA}
        try:
            session = await self._get_session()
            async with session.get(BING_NEWS_URL, params={"q": query, "format": "RSS"},
                                   headers=headers) as resp:
                if resp.status != 200:
                    return [], f"HTTP {resp.status}"
                body = await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            return [], f"{type(exc).__name__}: {exc}"

        items = await asyncio.to_thread(_parse_bing_rss, body)
        return items, None


def merge_search_results(*sources: list[dict]) -> list[dict]:
    """Сливает результаты нескольких источников поиска в один список без
    дублей по ссылке — сначала все записи первого источника (в его
    порядке), затем новые (по ссылке) записи следующих. Конкретный источник
    может молчать (ошибка/пустая выдача) без вреда остальным — вызывающий
    код передаёт уже пустой список для него."""
    out: list[dict] = []
    seen_links: set[str] = set()
    for items in sources:
        for item in items:
            link = item.get("link") or ""
            if not link or link in seen_links:
                continue
            seen_links.add(link)
            out.append(item)
    return out
