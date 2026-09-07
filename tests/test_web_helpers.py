"""Тесты чистых функций-хелперов bot/web.py: защита от open redirect
(_safe_next), проверка подписи Telegram initData, фильтр небезопасных URL
для href/src. Ничего из этого не поднимает aiohttp-приложение — только сами
функции, никакого сетевого I/O."""
from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import urlencode

from bot.web import _is_http_url, _safe_href, _safe_next, verify_telegram_init_data


# --- _safe_next: защита от open redirect после /login -----------------------

def test_safe_next_allows_plain_path():
    assert _safe_next("/queue") == "/queue"
    assert _safe_next("/queue/42?page=2") == "/queue/42?page=2"


def test_safe_next_rejects_empty_or_relative():
    assert _safe_next("") == "/"
    assert _safe_next("queue") == "/"


def test_safe_next_rejects_absolute_url():
    assert _safe_next("https://evil.example/phish") == "/"
    assert _safe_next("http://evil.example") == "/"


def test_safe_next_rejects_protocol_relative_double_slash():
    assert _safe_next("//evil.example") == "/"


def test_safe_next_rejects_backslash_variant_of_double_slash():
    """Баг: WHATWG URL-парсер (все современные браузеры) у "спецсхем"
    (http/https) заменяет "\\" на "/" ДО разбора пути. Поэтому
    "Location: /\\evil.example" браузер понимает так же, как "Location:
    //evil.example" — переход на chosen хост evil.example, а не свой сайт.
    Старая проверка smотрела только на буквальное "//", пропуская этот
    вариант — открытый редирект через /login?next=/\\evil.example."""
    assert _safe_next("/\\evil.example") == "/"
    assert _safe_next("/\\/evil.example") == "/"
    assert _safe_next("\\/evil.example") == "/"
    assert _safe_next("\\\\evil.example") == "/"


def test_safe_next_allows_path_with_literal_backslash_in_query():
    # Не перегибаем: обычный свой путь с одиночным "\" не на второй позиции
    # (не образует "//"-подобной пары в начале) остаётся рабочим.
    assert _safe_next("/queue?x=a\\b") == "/queue?x=a\\b"


# --- verify_telegram_init_data -----------------------------------------------

def _sign(data: dict, bot_token: str) -> str:
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    return hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()


def _build_init_data(bot_token: str, *, user_id: int = 1, auth_date: int | None = None) -> str:
    data = {
        "user": f'{{"id":{user_id},"first_name":"T"}}',
        "auth_date": str(auth_date if auth_date is not None else int(time.time())),
        "query_id": "AAA",
    }
    data["hash"] = _sign(data, bot_token)
    return urlencode(data)


def test_verify_telegram_init_data_accepts_valid_signature():
    token = "123:ABC"
    init_data = _build_init_data(token, user_id=42)
    user = verify_telegram_init_data(init_data, token)
    assert user is not None
    assert user["id"] == 42


def test_verify_telegram_init_data_rejects_tampered_hash():
    token = "123:ABC"
    init_data = _build_init_data(token) + "0"  # портим hash на конце
    assert verify_telegram_init_data(init_data, token) is None


def test_verify_telegram_init_data_rejects_wrong_token():
    init_data = _build_init_data("123:ABC")
    assert verify_telegram_init_data(init_data, "999:WRONG") is None


def test_verify_telegram_init_data_rejects_expired_auth_date():
    token = "123:ABC"
    old = int(time.time()) - 2 * 24 * 3600  # двое суток назад, TTL — сутки
    init_data = _build_init_data(token, auth_date=old)
    assert verify_telegram_init_data(init_data, token) is None


def test_verify_telegram_init_data_rejects_missing_hash():
    assert verify_telegram_init_data("user=%7B%7D&auth_date=1", "token") is None


def test_verify_telegram_init_data_rejects_empty_input():
    assert verify_telegram_init_data("", "token") is None
    assert verify_telegram_init_data("a=b", "") is None


# --- _is_http_url / _safe_href -----------------------------------------------

def test_is_http_url_accepts_http_and_https():
    assert _is_http_url("https://example.com/a") is True
    assert _is_http_url("http://example.com") is True
    assert _is_http_url("HTTPS://EXAMPLE.COM") is True


def test_is_http_url_rejects_javascript_scheme():
    assert _is_http_url("javascript:alert(1)") is False


def test_safe_href_passes_through_http_url_escaped():
    assert _safe_href("https://example.com/a?x=1&y=2") == "https://example.com/a?x=1&amp;y=2"


def test_safe_href_returns_hash_for_javascript_scheme():
    assert _safe_href("javascript:alert(document.cookie)") == "#"


def test_safe_href_returns_hash_for_empty():
    assert _safe_href("") == "#"
