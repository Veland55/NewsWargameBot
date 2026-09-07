"""Тесты Quota (bot/quota.py) — подсчёт расхода и пороги предупреждений
70/90%. Telegram (bot.send_message) мокается, никаких настоящих запросов."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.db import Storage
from bot.quota import Quota, until_reset


def make_quota(storage: Storage, *, request_limit: int | None = None) -> Quota:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    llm = MagicMock()
    llm.model = "test-model"
    llm.is_free_model = True
    llm.key_info = AsyncMock(return_value=None)  # backend='default' без реального ключа OpenRouter
    q = Quota(storage, llm, bot, admin_ids={111, 222})
    if request_limit is not None:
        storage.set("free_daily_limit", str(request_limit))
    return q


# --- until_reset --------------------------------------------------------------

def test_until_reset_format_has_hours_and_minutes():
    text = until_reset()
    assert "ч" in text and "мин" in text


# --- thresholds parsing ---------------------------------------------------

def test_thresholds_default_is_70_90(storage: Storage):
    q = make_quota(storage)
    assert q.thresholds() == [70, 90]


def test_thresholds_ignores_out_of_range_and_dedups(storage: Storage):
    storage.set("alert_thresholds", "70,70,0,101,50")
    q = make_quota(storage)
    assert q.thresholds() == [50, 70]


def test_thresholds_empty_when_field_blank(storage: Storage):
    storage.set("alert_thresholds", "")
    q = make_quota(storage)
    assert q.thresholds() == []


# --- record() -------------------------------------------------------------

def test_record_bumps_usage_for_backend(storage: Storage):
    q = make_quota(storage)
    q.record({"tokens_in": 100, "tokens_out": 50, "cost": 0.01}, backend="default")
    from bot.quota import utc_day
    usage = storage.usage(utc_day(), "default")
    assert usage == {"requests": 1, "tokens_in": 100, "tokens_out": 50, "cost": 0.01}


def test_record_keeps_backends_separate(storage: Storage):
    q = make_quota(storage)
    q.record({"tokens_in": 1, "tokens_out": 1, "cost": 0.0}, backend="default")
    q.record({"tokens_in": 2, "tokens_out": 2, "cost": 0.0}, backend="claude")
    from bot.quota import utc_day
    assert storage.usage(utc_day(), "default")["requests"] == 1
    assert storage.usage(utc_day(), "claude")["requests"] == 1
    assert storage.usage(utc_day(), "gemini")["requests"] == 0


# --- check_and_alert: пороги 70/90% ----------------------------------------

@pytest.mark.asyncio
async def test_check_and_alert_sends_at_70_percent(storage: Storage):
    q = make_quota(storage, request_limit=100)
    for _ in range(70):
        q.record({}, backend="default")
    await q.check_and_alert("default")
    assert q.bot.send_message.await_count == len(q.admin_ids)
    sent_text = q.bot.send_message.call_args.args[1] if q.bot.send_message.call_args.args \
        else q.bot.send_message.call_args.kwargs.get("text", "")
    assert "70%" in sent_text


@pytest.mark.asyncio
async def test_check_and_alert_does_not_resend_same_threshold(storage: Storage):
    q = make_quota(storage, request_limit=100)
    for _ in range(70):
        q.record({}, backend="default")
    await q.check_and_alert("default")
    q.bot.send_message.reset_mock()
    # Ещё один запрос, всё ещё в диапазоне 70-89% — тот же порог 70 уже
    # отправлен сегодня, повторно слать не нужно.
    q.record({}, backend="default")
    await q.check_and_alert("default")
    q.bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_check_and_alert_90_after_70_sends_again(storage: Storage):
    q = make_quota(storage, request_limit=100)
    for _ in range(70):
        q.record({}, backend="default")
    await q.check_and_alert("default")
    q.bot.send_message.reset_mock()
    for _ in range(20):
        q.record({}, backend="default")
    await q.check_and_alert("default")  # теперь 90%
    assert q.bot.send_message.await_count == len(q.admin_ids)


@pytest.mark.asyncio
async def test_check_and_alert_jumping_straight_to_90_sends_once_not_twice(storage: Storage):
    """Расход одним махом перепрыгивает и 70, и 90% — предупреждение должно
    уйти только одно (самое серьёзное), не по одному на каждый порог."""
    q = make_quota(storage, request_limit=100)
    for _ in range(95):
        q.record({}, backend="default")
    await q.check_and_alert("default")
    assert q.bot.send_message.await_count == len(q.admin_ids)
    q.bot.send_message.reset_mock()
    q.record({}, backend="default")
    await q.check_and_alert("default")
    q.bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_check_and_alert_below_threshold_sends_nothing(storage: Storage):
    q = make_quota(storage, request_limit=100)
    for _ in range(10):
        q.record({}, backend="default")
    await q.check_and_alert("default")
    q.bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_check_and_alert_backends_alert_independently(storage: Storage):
    # У каждого бэкенда свой счёт расхода — предупреждение по default не
    # должно молча гасить такое же по claude в тот же день (общий флаг
    # по дню без бэкенда в ключе делал бы именно это, см. комментарий в
    # check_and_alert).
    q = make_quota(storage, request_limit=100)
    for _ in range(70):
        q.record({}, backend="default")
    await q.check_and_alert("default")
    q.bot.send_message.reset_mock()
    for _ in range(70):
        q.record({}, backend="claude")
    await q.check_and_alert("claude")
    assert q.bot.send_message.await_count == len(q.admin_ids)


@pytest.mark.asyncio
async def test_check_and_alert_no_admins_does_nothing(storage: Storage):
    bot = MagicMock()
    bot.send_message = AsyncMock()
    llm = MagicMock()
    llm.model = "m"
    llm.is_free_model = True
    llm.key_info = AsyncMock(return_value=None)
    q = Quota(storage, llm, bot, admin_ids=set())
    storage.set("free_daily_limit", "100")
    for _ in range(95):
        q.record({}, backend="default")
    await q.check_and_alert("default")
    bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_check_and_alert_race_double_call_sends_once(storage: Storage):
    """Фоновый цикл и /checknow могут вызвать check_and_alert почти
    одновременно — set_if_absent должен пропустить только одно из двух
    отправленных предупреждений (см. комментарий в check_and_alert про
    атомарный claim вместо get()+set())."""
    import asyncio
    q = make_quota(storage, request_limit=100)
    for _ in range(70):
        q.record({}, backend="default")
    await asyncio.gather(q.check_and_alert("default"), q.check_and_alert("default"))
    assert q.bot.send_message.await_count == len(q.admin_ids)
