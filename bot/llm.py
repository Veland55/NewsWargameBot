"""Клиент к DeepSeek (OpenAI-совместимый /chat/completions).

Тот же код работает с любым совместимым провайдером — достаточно поменять
LLM_BASE_URL и LLM_MODEL (например, бесплатный пул DeepSeek на OpenRouter).
"""
from __future__ import annotations

import asyncio
import logging
import socket
from typing import Callable

import aiohttp

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


class LLMEmpty(LLMError):
    """Ответ пришёл, но текста в нём нет.

    Так бывает, когда модель израсходовала бюджет на размышления или
    отфильтровала сама себя. Это не отказ сервиса: тот же запрос со второй
    попытки обычно отрабатывает, поэтому ошибку надо повторять, а не сдаваться.
    """

    def __init__(self, message: str, finish_reason: str = ""):
        super().__init__(message)
        self.finish_reason = finish_reason


class LLMClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout: int = 90,
        max_tokens: int = 800,
        temperature: float = 0.3,
        retries: int = 2,
        on_usage: Callable[[dict], None] | None = None,
        reasoning_effort: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.retries = retries
        # Только для «думающих» моделей (сейчас — Gemini): без этого они тратят
        # весь max_tokens на невидимые рассуждения и возвращают пустой ответ.
        # У DeepSeek/OpenRouter не выставляется — там параметр незнаком и может
        # вызвать ошибку запроса.
        self.reasoning_effort = reasoning_effort
        # Вызывается после каждого удачного запроса — так учёт расхода
        # не знает про HTTP, а клиент не знает про базу.
        self.on_usage = on_usage
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # family=AF_INET: на некоторых хостах исходящий IPv6 у Google
            # для generativelanguage.googleapis.com гео-заблокирован
            # ("User location is not supported"), хотя IPv4 с того же
            # сервера проходит нормально — форсируем его для всех
            # провайдеров на этом клиенте, не только для Gemini.
            connector = aiohttp.TCPConnector(family=socket.AF_INET)
            self._session = aiohttp.ClientSession(timeout=self._timeout, connector=connector)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    @property
    def endpoint(self) -> str:
        # DeepSeek принимает и /chat/completions, и /v1/...; OpenRouter уже
        # содержит /v1 в base_url — поэтому просто дописываем путь.
        return f"{self.base_url}/chat/completions"

    @property
    def is_openrouter(self) -> bool:
        return "openrouter" in self.base_url

    @property
    def is_free_model(self) -> bool:
        """У бесплатных вариантов OpenRouter id заканчивается на «:free»."""
        return self.model.strip().endswith(":free")

    @property
    def _auth_headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if self.is_openrouter:
            # Необязательная атрибуция: под этим именем запросы видны
            # в статистике на openrouter.ai.
            headers["X-Title"] = "rss-deepseek-bot"
        return headers

    async def key_info(self) -> dict | None:
        """GET /api/v1/key — остаток кредитов по ключу (только OpenRouter).

        Возвращает поля limit / limit_remaining / usage_daily / is_free_tier
        или None, если провайдер не OpenRouter либо запрос не удался.
        """
        if not self.is_openrouter or not self.api_key:
            return None
        try:
            session = await self._get_session()
            async with session.get(
                f"{self.base_url}/key", headers=self._auth_headers
            ) as resp:
                if resp.status != 200:
                    log.warning("GET /key вернул HTTP %s", resp.status)
                    return None
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            log.warning("не удалось получить /key: %s", exc)
            return None
        info = data.get("data") if isinstance(data, dict) else None
        return info if isinstance(info, dict) else None

    async def complete(self, prompt: str, system: str | None = None) -> str:
        if not self.api_key:
            raise LLMError("LLM_API_KEY не задан")

        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": False,
        }
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        headers = self._auth_headers

        last_error = "неизвестная ошибка"
        # Обрезанный, но непустой ответ (finish_reason=length) раньше считался
        # успехом и уходил в канал на полуслове, без хештегов в конце —
        # держим его тут как запасной вариант, а сами всё равно пробуем
        # дотянуть до чистого finish_reason=stop с большим лимитом.
        truncated_text: str | None = None
        truncated_usage: dict | None = None
        for attempt in range(1, self.retries + 2):
            try:
                session = await self._get_session()
                async with session.post(
                    self.endpoint, json=payload, headers=headers
                ) as resp:
                    body = await resp.text()
                    if resp.status == 200:
                        try:
                            text, finish, usage = self._extract(body)
                        except LLMEmpty as exc:
                            # Пустой ответ раньше сразу ронял запрос, минуя
                            # повторы, — и новость уходила необработанной.
                            last_error = str(exc)
                            finish = exc.finish_reason
                        else:
                            if finish != "length":
                                if self.on_usage:
                                    try:
                                        self.on_usage(usage)
                                    except Exception:
                                        log.exception("сбой в учёте расхода")
                                return text
                            last_error = f"ответ обрезан на {len(text)} символах (finish_reason=length)"
                            truncated_text, truncated_usage = text, usage
                        if finish == "length":
                            # Бюджет вышел до того, как модель дописала
                            # ответ (или не начала вовсе) — на повтор даём
                            # больше места.
                            payload["max_tokens"] = min(
                                4000, int(payload["max_tokens"] * 2))
                            last_error += (f"; повышаю лимит до "
                                           f"{payload['max_tokens']} токенов")
                    else:
                        last_error = f"HTTP {resp.status}: {body[:300]}"
                        # 4xx (кроме 429) повторять смысла нет. Проверка
                        # относится только к отказам HTTP: пустой ответ
                        # приходит с кодом 200 и повторяться должен.
                        if resp.status < 500 and resp.status != 429:
                            break
            except asyncio.TimeoutError:
                last_error = "таймаут запроса к LLM"
            except aiohttp.ClientError as exc:
                last_error = f"{type(exc).__name__}: {exc}"

            if attempt <= self.retries:
                delay = 3 * attempt
                log.warning("LLM попытка %s не удалась (%s), повтор через %ss",
                            attempt, last_error, delay)
                await asyncio.sleep(delay)

        if truncated_text is not None:
            # Так и не дотянули до чистого finish_reason=stop — публикуем
            # обрезанный черновик, это лучше, чем совсем ничего.
            log.warning("отдаю обрезанный ответ после всех попыток: %s", last_error)
            if self.on_usage and truncated_usage:
                try:
                    self.on_usage(truncated_usage)
                except Exception:
                    log.exception("сбой в учёте расхода")
            return truncated_text
        raise LLMError(last_error)

    @staticmethod
    def _extract(body: str) -> tuple[str, str, dict]:
        """Возвращает (текст ответа, finish_reason, сведения о расходе токенов)."""
        import json

        try:
            data = json.loads(body)
        except ValueError as exc:
            raise LLMError(f"не JSON в ответе: {exc}") from exc

        if data.get("error"):
            err = data["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise LLMError(str(msg))

        try:
            choice = data["choices"][0]
            message = choice["message"]
            content = message.get("content")
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"неожиданная структура ответа: {body[:300]}") from exc

        text = (content or "").strip()
        finish = str(choice.get("finish_reason") or "")
        if not text:
            # Рассуждающие модели кладут ход мыслей отдельно; в пост он не
            # годится, но по нему видно, на что ушёл бюджет.
            thinking = (message.get("reasoning")
                        or message.get("reasoning_content") or "")
            detail = f", finish_reason={finish}" if finish else ""
            if thinking:
                detail += f", рассуждений на {len(thinking)} символов"
            raise LLMEmpty(f"модель вернула пустой ответ{detail}", finish)

        raw = data.get("usage") or {}
        usage = {
            "tokens_in": int(raw.get("prompt_tokens") or 0),
            "tokens_out": int(raw.get("completion_tokens") or 0),
            # OpenRouter кладёт сюда стоимость запроса в кредитах
            "cost": float(raw.get("cost") or 0.0),
        }
        return text, finish, usage
