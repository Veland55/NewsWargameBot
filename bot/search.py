"""Обнаружение свежих статей через веб-поиск — для источников без RSS, у
которых собственный кэш сайта (sitemap.xml, страница списка новостей и
подобное) отдаёт устаревший снимок, никак не обходимый обычными HTTP-
средствами (см. историю в SETUP.md, раздел «Сайты без RSS»: на
warhammer-community.com sitemap.xml оказался закэширован на CDN на 54+
часа, ни параметры против кэша, ни заголовки no-cache, ни служебные
заголовки Next.js это не обходят — а поисковик индексирует те же страницы
куда быстрее).

Поиск использует Google Programmable Search Engine (Custom Search JSON
API) — единственный практичный способ узнавать о новых страницах сайта
независимо от того, насколько устарел кэш самого сайта. Нужен ключ API и
id поисковой системы, см. SETUP.md.
"""
from __future__ import annotations

import asyncio
import logging

import aiohttp

log = logging.getLogger(__name__)

API_URL = "https://www.googleapis.com/customsearch/v1"
TIMEOUT = 15


class SearchError(RuntimeError):
    pass


class SearchClient:
    def __init__(self, api_key: str = "", cse_id: str = "", *, timeout: int = TIMEOUT):
        self.api_key = (api_key or "").strip()
        self.cse_id = (cse_id or "").strip()
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.cse_id)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def search(self, query: str, date_restrict: str = "w1",
                     num: int = 10) -> tuple[list[dict], str | None]:
        """(результаты, ошибка). Результат — {title, link, snippet}, в том
        порядке, в котором их вернул поиск (примерно по релевантности —
        для запроса вида site:домен/путь без других слов это на практике
        означает «недавно проиндексированное и популярное» сперва).

        date_restrict — окно свежести на стороне Google: 'd3' (3 дня),
        'w1' (неделя) и т.п. Не даёт точную дату публикации (это не всегда
        умеет и сам Google для произвольного сайта), только предфильтрует
        совсем старое, чтобы не тратить на него запросы к странице статьи.
        """
        if not self.configured:
            return [], "поиск не настроен: нет ключа API или id поисковой системы"
        params = {
            "key": self.api_key,
            "cx": self.cse_id,
            "q": query,
            "num": str(max(1, min(10, num))),
        }
        if date_restrict:
            params["dateRestrict"] = date_restrict
        try:
            session = await self._get_session()
            async with session.get(API_URL, params=params) as resp:
                try:
                    data = await resp.json()
                except (aiohttp.ContentTypeError, ValueError):
                    data = {}
                if resp.status != 200:
                    msg = (data.get("error") or {}).get("message") or f"HTTP {resp.status}"
                    return [], msg
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            return [], f"{type(exc).__name__}: {exc}"

        items = data.get("items") or []
        out = [
            {"title": it.get("title", "") or "", "link": it.get("link", "") or "",
             "snippet": it.get("snippet", "") or ""}
            for it in items if it.get("link")
        ]
        return out, None
