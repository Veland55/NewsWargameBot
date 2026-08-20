"""Учёт расхода лимитов LLM и предупреждения админам.

Отслеживаются две независимые вещи:

* **Запросы в сутки** — у бесплатных моделей OpenRouter (id на `:free`) есть
  суточный лимит запросов. Провайдер его не отдаёт в ответах, поэтому считаем
  сами: каждый удачный запрос инкрементит счётчик за текущий день UTC.
* **Кредиты** — если на ключе задан лимит трат, `GET /api/v1/key` возвращает
  `limit` и `limit_remaining`; следим за их отношением.

При переходе через каждый порог (по умолчанию 70% и 90%) админам уходит одно
сообщение в личку — повторно за те же сутки оно не отправляется.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from .db import FREE_RPD_NO_CREDITS, FREE_RPD_WITH_CREDITS, Storage
from .llm import LLMClient

log = logging.getLogger(__name__)

KEY_CACHE_TTL = 600  # как часто перезапрашивать /api/v1/key, сек


def utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def until_reset() -> str:
    now = datetime.now(timezone.utc)
    left = 24 * 60 - (now.hour * 60 + now.minute)
    return f"{left // 60} ч {left % 60} мин"


@dataclass(slots=True)
class QuotaInfo:
    day: str
    requests: int
    tokens_in: int
    tokens_out: int
    cost: float
    model: str
    is_free_model: bool
    request_limit: int | None = None      # суточный лимит запросов, если известен
    limit_source: str = ""                # откуда взят лимит
    credit_limit: float | None = None     # лимит трат по ключу
    credit_remaining: float | None = None
    credits_used_total: float | None = None
    credits_used_today: float | None = None
    is_free_tier: bool | None = None

    @property
    def request_pct(self) -> float | None:
        if not self.request_limit:
            return None
        return self.requests / self.request_limit * 100

    @property
    def credit_pct(self) -> float | None:
        if not self.credit_limit or self.credit_remaining is None:
            return None
        return (self.credit_limit - self.credit_remaining) / self.credit_limit * 100


class Quota:
    def __init__(self, storage: Storage, llm: LLMClient, bot: Bot, admin_ids: set[int]):
        self.st = storage
        self.llm = llm
        self.bot = bot
        self.admin_ids = set(admin_ids)
        self._key_cache: dict | None = None
        self._key_cache_at: float = 0.0

    # --- запись расхода --------------------------------------------------
    def record(self, usage: dict) -> None:
        """Колбэк для LLMClient.on_usage — синхронный и быстрый."""
        day = utc_day()
        self.st.bump_usage(
            day,
            tokens_in=int(usage.get("tokens_in") or 0),
            tokens_out=int(usage.get("tokens_out") or 0),
            cost=float(usage.get("cost") or 0.0),
        )
        self.st.drop_alerts_except(day)

    # --- сбор картины ----------------------------------------------------
    async def key_info(self, force: bool = False) -> dict | None:
        fresh = time.monotonic() - self._key_cache_at < KEY_CACHE_TTL
        if self._key_cache is not None and fresh and not force:
            return self._key_cache
        info = await self.llm.key_info()
        if info is not None:
            self._key_cache = info
            self._key_cache_at = time.monotonic()
        return info

    async def snapshot(self, force: bool = False) -> QuotaInfo:
        day = utc_day()
        used = self.st.usage(day)
        info = QuotaInfo(
            day=day,
            requests=int(used["requests"]),
            tokens_in=int(used["tokens_in"]),
            tokens_out=int(used["tokens_out"]),
            cost=float(used["cost"]),
            model=self.llm.model,
            is_free_model=self.llm.is_free_model,
        )

        manual = self.st.get_int("free_daily_limit")
        if manual > 0:
            info.request_limit = manual
            info.limit_source = "задан вручную"

        key = await self.key_info(force=force)
        if key:
            info.credit_limit = key.get("limit")
            info.credit_remaining = key.get("limit_remaining")
            info.credits_used_total = key.get("usage")
            info.credits_used_today = key.get("usage_daily")
            info.is_free_tier = key.get("is_free_tier")

        if info.request_limit is None and info.is_free_model:
            # is_free_tier == True означает «кредиты никогда не покупались»,
            # то есть действует урезанный суточный лимит.
            if info.is_free_tier is True:
                info.request_limit = FREE_RPD_NO_CREDITS
                info.limit_source = "бесплатный тариф OpenRouter"
            elif info.is_free_tier is False:
                info.request_limit = FREE_RPD_WITH_CREDITS
                info.limit_source = "OpenRouter, кредиты покупались"
            else:
                info.request_limit = FREE_RPD_NO_CREDITS
                info.limit_source = "предположение (ключ не опрошен)"
        return info

    # --- предупреждения --------------------------------------------------
    def thresholds(self) -> list[int]:
        raw = self.st.get("alert_thresholds")
        out: list[int] = []
        for part in raw.replace(" ", "").split(","):
            if part.isdigit() and 1 <= int(part) <= 100:
                out.append(int(part))
        return sorted(set(out))

    async def check_and_alert(self) -> None:
        """Вызывается после публикаций; отправляет только новые предупреждения."""
        if not self.thresholds() or not self.admin_ids:
            return
        try:
            info = await self.snapshot()
        except Exception:
            log.exception("не удалось собрать сведения о лимите")
            return

        for kind, pct in (("requests", info.request_pct), ("credits", info.credit_pct)):
            if pct is None:
                continue
            # Берём самый высокий из перейдённых порогов, чтобы при резком
            # скачке не присылать два сообщения подряд.
            crossed = [t for t in self.thresholds() if pct >= t]
            if not crossed:
                continue
            top = max(crossed)
            flag = f"alerted:{info.day}:{kind}:{top}"
            if self.st.get(flag) == "1":
                continue
            for lower in crossed:
                self.st.set(f"alerted:{info.day}:{kind}:{lower}", "1")
            await self._notify(self._alert_text(kind, top, pct, info))

    @staticmethod
    def _alert_text(kind: str, threshold: int, pct: float, info: QuotaInfo) -> str:
        icon = "🔴" if threshold >= 90 else "🟡"
        if kind == "requests":
            return (
                f"{icon} <b>Израсходовано {pct:.0f}% суточного лимита запросов</b>\n\n"
                f"Запросов сегодня: {info.requests} из {info.request_limit}\n"
                f"Модель: <code>{info.model}</code>\n"
                f"Лимит: {info.limit_source}\n"
                f"Обнулится через {until_reset()} (00:00 UTC)\n\n"
                + (
                    "Когда лимит закончится, запросы начнут отвечать ошибкой 429. "
                    "Варианты: увеличить <code>/interval</code>, уменьшить "
                    "<code>/set max_per_cycle</code> или перейти на платную модель "
                    "через <code>/setmodel</code>."
                    if threshold >= 90
                    else "Расход можно снизить командами <code>/interval</code> и "
                         "<code>/set max_per_cycle</code>."
                )
            )
        return (
            f"{icon} <b>Израсходовано {pct:.0f}% лимита кредитов на ключе</b>\n\n"
            f"Потрачено: {(info.credit_limit or 0) - (info.credit_remaining or 0):.4f} "
            f"из {info.credit_limit:.4f}\n"
            f"Осталось: {info.credit_remaining:.4f}\n"
            f"Модель: <code>{info.model}</code>\n\n"
            "Пополните баланс или поднимите лимит ключа на openrouter.ai/settings/keys."
        )

    async def _notify(self, text: str) -> None:
        for admin_id in sorted(self.admin_ids):
            try:
                await self.bot.send_message(admin_id, text, parse_mode="HTML")
            except TelegramAPIError as exc:
                log.warning("не смог предупредить админа %s: %s", admin_id, exc)
