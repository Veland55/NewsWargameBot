"""Клиент к Claude (Anthropic Messages API).

Отдельный протокол от OpenAI-совместимого /chat/completions в llm.py:
свой заголовок авторизации, свой формат тела и ответа. Интерфейс (`.complete`,
`.model`) специально повторяет LLMClient, чтобы Publisher мог использовать
любой из двух клиентов не меняя остальной код — см. Publisher._active_llm.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable

import aiohttp

from .llm import LLMEmpty, LLMError, LLMQuotaExceeded

log = logging.getLogger(__name__)

API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


class ClaudeClient:
    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-5",
        *,
        timeout: int = 90,
        max_tokens: int = 1024,
        temperature: float = 0.3,
        retries: int = 2,
        on_usage: Callable[[dict], None] | None = None,
    ):
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.retries = retries
        # См. LLMClient.on_usage — тот же смысл: учёт расхода не знает про
        # HTTP, клиент не знает про базу. У Anthropic нет поля "cost" в
        # ответе (в отличие от OpenRouter) — считаем только запросы/токены.
        self.on_usage = on_usage
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }

    async def complete(self, prompt: str, system: str | None = None) -> str:
        if not self.api_key:
            raise LLMError("CLAUDE_API_KEY не задан")

        payload: dict[str, object] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            payload["system"] = system

        def record(usage: dict) -> None:
            if self.on_usage:
                try:
                    self.on_usage(usage)
                except Exception:
                    log.exception("сбой в учёте расхода")

        last_error = "неизвестная ошибка"
        last_status: int | None = None
        for attempt in range(1, self.retries + 2):
            last_status = None
            try:
                session = await self._get_session()
                async with session.post(
                    API_URL, json=payload, headers=self._headers
                ) as resp:
                    body = await resp.text()
                    if resp.status == 200:
                        try:
                            text, usage = self._extract(body)
                        except LLMEmpty as exc:
                            last_error = str(exc)
                            if exc.usage:
                                record(exc.usage)
                        else:
                            record(usage)
                            return text
                    else:
                        last_error = f"HTTP {resp.status}: {body[:300]}"
                        last_status = resp.status
                        # 429 и 5xx стоит повторить, остальные 4xx — нет.
                        if resp.status < 500 and resp.status != 429:
                            break
            except asyncio.TimeoutError:
                last_error = "таймаут запроса к Claude"
            except aiohttp.ClientError as exc:
                last_error = f"{type(exc).__name__}: {exc}"

            if attempt <= self.retries:
                delay = 3 * attempt
                log.warning("Claude попытка %s не удалась (%s), повтор через %ss",
                            attempt, last_error, delay)
                await asyncio.sleep(delay)

        if last_status == 429:
            raise LLMQuotaExceeded(last_error)
        raise LLMError(last_error)

    @staticmethod
    def _extract(body: str) -> tuple[str, dict]:
        import json

        try:
            data = json.loads(body)
        except ValueError as exc:
            raise LLMError(f"не JSON в ответе: {exc}") from exc

        if data.get("type") == "error":
            err = data.get("error") or {}
            raise LLMError(str(err.get("message") or body[:300]))

        raw = data.get("usage") or {}
        # У Anthropic нет поля "cost" (это особенность ответов OpenRouter) —
        # только количество токенов, стоимость в деньгах отсюда не посчитать.
        usage = {
            "tokens_in": int(raw.get("input_tokens") or 0),
            "tokens_out": int(raw.get("output_tokens") or 0),
            "cost": 0.0,
        }

        parts = data.get("content") or []
        text = "".join(
            p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text"
        ).strip()
        if not text:
            stop = str(data.get("stop_reason") or "")
            raise LLMEmpty(f"Claude вернул пустой ответ, stop_reason={stop}", stop, usage)
        return text, usage
