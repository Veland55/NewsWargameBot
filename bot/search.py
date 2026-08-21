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
import logging

import aiohttp

log = logging.getLogger(__name__)

API_URL = "https://google.serper.dev/search"
TIMEOUT = 15


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
        out = [
            {"title": it.get("title", "") or "", "link": it.get("link", "") or "",
             "snippet": it.get("snippet", "") or ""}
            for it in items if it.get("link")
        ]
        return out, None
