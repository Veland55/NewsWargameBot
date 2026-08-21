"""Веб-панель управления ботом — alternative к командам в Telegram.

Работает в том же процессе, тем же asyncio-event-loop-ом, что и сам бот
(см. main.py) — отдельного сервиса/деплоя не требует. Вся логика уже есть в
Storage/Publisher, эти хендлеры только рендерят HTML и дёргают те же методы,
что и bot/handlers.py — бизнес-правила не дублируются.

Поднимается только если задан WEB_PANEL_PASSWORD — без пароля публиковать
панель было бы небезопасно, поэтому по умолчанию она выключена.

Пароль передаётся по HTTP в открытом виде (см. README/SETUP про причины и
альтернативы — домен+HTTPS или SSH-туннель). Это осознанный выбор для
личного использования, а не оплошность: сессионная кука (не Basic Auth)
хотя бы сокращает передачу пароля до одного запроса вместо каждого.
"""
from __future__ import annotations

import hashlib
import hmac
import html as html_mod
import json
import logging
import secrets
import time
from typing import Awaitable, Callable
from urllib.parse import parse_qsl

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import LinkPreviewOptions
from aiohttp import web

from .db import DEFAULTS, Storage
from .llm import LLMError
from .publisher import (TG_CAPTION_LIMIT, TG_LIMIT, Publisher, html_problem,
                        tg_len)
from .quota import until_reset
from .rss import Entry, discover_sitemap, fetch, fetch_sitemap

log = logging.getLogger(__name__)

SESSION_COOKIE = "bot_session"
SESSION_TTL = 7 * 24 * 3600      # неделя — снова логиниться каждый день утомительно
LOGIN_MAX_FAILS = 5              # неудачных попыток с одного адреса
LOGIN_LOCKOUT = 15 * 60          # прежде чем снова можно пробовать

SETTINGS_EDITABLE = (
    "interval", "max_per_cycle", "post_delay", "backfill",
    "max_age_days", "flood_guard", "keep_seen",
    "alert_thresholds", "free_daily_limit", "max_images",
)
SETTINGS_TOGGLES = ("require_russian", "disable_preview", "images", "og_image")

# (заголовок группы, [(ключ, короткая подпись, единица, подсказка), ...]) —
# человекочитаемые подписи для полей SETTINGS_EDITABLE вместо голых
# snake_case-имён настроек. Подпись+единица — короткие, влезают над полем в
# одну строку; всё, что раньше делало подпись длинным предложением, ушло в
# hint под полем (.field-hint), чтобы поля в ряду не разъезжались по высоте.
GENERAL_GROUPS: list[tuple[str, list[tuple[str, str, str, str]]]] = [
    ("Периодичность", [
        ("interval", "Интервал проверки", "мин", "Как часто опрашивать все ленты"),
        ("max_per_cycle", "Лимит за проход", "шт", "Максимум новостей с одной ленты за раз"),
        ("post_delay", "Пауза между постами", "сек", "Задержка между публикациями подряд"),
        ("backfill", "При добавлении публиковать", "шт", "Сколько последних новостей опубликовать сразу"),
    ]),
    ("Защита от сбоев", [
        ("max_age_days", "Не старше", "дней", "Новости старше — пропускать. 0 — без ограничения"),
        ("flood_guard", "Порог сбоя ленты", "записей", "Разом больше — считаем сбоем, публикуем только последние"),
        ("keep_seen", "История ленты", "записей", "Сколько отметок «прочитано» хранить на ленту"),
    ]),
    ("Лимиты и уведомления", [
        ("alert_thresholds", "Пороги предупреждений", "%", "Через запятую, например 70,90"),
        ("free_daily_limit", "Суточный лимит", "запросов", "0 — определить автоматически по модели"),
    ]),
    ("Картинки", [
        ("max_images", "Картинок в альбом", "1-10", "Сколько скачивать за раз лентам с «несколькими картинками» — включается у каждой ленты отдельно, на «Лентах»"),
    ]),
]

# ключ → (подпись, короткая подсказка) — для SETTINGS_TOGGLES.
TOGGLE_LABELS: dict[str, tuple[str, str]] = {
    "require_russian": ("Требовать русский язык", "Ответ не на русском — считать отказом модели и повторить"),
    "disable_preview": ("Без превью ссылок", "Не показывать превью ссылки в посте"),
    "images": ("Прикладывать картинку", "Картинка из новости — к посту"),
    "og_image": ("Картинка со страницы", "Если в ленте её нет — взять со страницы новости"),
}

TG_AUTH_DATE_TTL = 24 * 3600     # старше суток initData не принимаем (см. verify_telegram_init_data)


def verify_telegram_init_data(init_data: str, bot_token: str) -> dict | None:
    """Проверка initData из Telegram Web App (авто-вход /tg-login).

    Тот же алгоритм, что описан в официальной доке Telegram — HMAC от
    bot_token, secret_key = HMAC_SHA256(key="WebAppData", data=bot_token).
    Возвращает распарсенный объект user или None, если подпись неверна,
    истёк auth_date, или initData вообще пустой (бот открыт не из Telegram).
    """
    if not init_data or not bot_token:
        return None
    try:
        data = dict(parse_qsl(init_data, keep_blank_values=True))
    except ValueError:
        return None
    received_hash = data.pop("hash", None)
    if not received_hash:
        return None
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed_hash, received_hash):
        return None
    try:
        auth_date = int(data.get("auth_date", "0"))
    except ValueError:
        return None
    if not auth_date or time.time() - auth_date > TG_AUTH_DATE_TTL:
        return None
    try:
        user = json.loads(data.get("user", "null"))
    except ValueError:
        return None
    return user if isinstance(user, dict) and "id" in user else None


def _e(text: object) -> str:
    return html_mod.escape(str(text if text is not None else ""))


def _rows_for(text: str, min_rows: int = 6, max_rows: int = 22) -> int:
    """rows= под конкретный текст, а не наугад — иначе поле либо обрезает
    свой же текст по нижнему краю (мало строк), либо стоит пустым колодцем
    (много строк на короткий текст)."""
    return max(min_rows, min(max_rows, text.count("\n") + 3))


def _safe_href(url: str) -> str:
    """Ссылки в постах приходят из RSS/Atom лент — это контент сайта-источника,
    не то, что ввёл сам админ. javascript:-урлы там маловероятны, но раз мы всё
    равно рендерим их кликабельными в браузере — лучше не давать возможности."""
    if url.strip().lower().startswith(("http://", "https://")):
        return _e(url)
    return "#"


class WebAuth:
    """Пароль один на всех админов — панель личная, разграничивать
    пользователей ещё не для чего. Сессии и счётчик неудач — в памяти:
    переживать перезапуск процесса им не обязательно."""

    def __init__(self, password: str):
        self.password = password
        self._sessions: dict[str, dict] = {}       # token -> {expires, csrf}
        self._fails: dict[str, tuple[int, float]] = {}   # ip -> (count, reset_at)

    def locked_out(self, ip: str) -> bool:
        entry = self._fails.get(ip)
        return bool(entry and time.time() < entry[1] and entry[0] >= LOGIN_MAX_FAILS)

    def record_fail(self, ip: str) -> None:
        now = time.time()
        count, reset_at = self._fails.get(ip, (0, now + LOGIN_LOCKOUT))
        if now > reset_at:
            count, reset_at = 0, now + LOGIN_LOCKOUT
        self._fails[ip] = (count + 1, reset_at)

    def record_success(self, ip: str) -> None:
        self._fails.pop(ip, None)

    def check(self, password: str) -> bool:
        return bool(self.password) and secrets.compare_digest(password, self.password)

    def new_session(self) -> str:
        token = secrets.token_urlsafe(32)
        self._sessions[token] = {
            "expires": time.time() + SESSION_TTL,
            "csrf": secrets.token_urlsafe(24),
        }
        self._prune()
        return token

    def verify(self, token: str | None) -> dict | None:
        if not token:
            return None
        entry = self._sessions.get(token)
        if not entry or time.time() > entry["expires"]:
            self._sessions.pop(token, None)
            return None
        return entry

    def revoke(self, token: str | None) -> None:
        if token:
            self._sessions.pop(token, None)

    def _prune(self) -> None:
        now = time.time()
        expired = [t for t, e in self._sessions.items() if now > e["expires"]]
        for t in expired:
            self._sessions.pop(t, None)


# ======================== HTML-обвязка ========================
# Мобильный — основной сценарий (панель открывают из Telegram на телефоне),
# поэтому это не «десктоп + медиа-запрос для мелкого экрана», а наоборот:
# базовые стили уже под палец и узкий экран, @media (min-width) добавляет
# десктопные удобства (таблицы вместо карточек и т.п.) сверху.
STYLE = """
:root {
  color-scheme: dark; --tg-top: 0px; --tg-bottom: 0px; --nav-h: 60px; --side-w: 216px;
  --bg: #100f14; --bg-alt: #16151d; --card: #1a1922; --card-hover: #201f2a;
  --border: #2a2836; --border-soft: #232230;
  --text: #eceaf3; --text-dim: #9490a6; --text-faint: #8a86a0;
  --accent: #ffb648; --accent-dim: #3a2f1c;
  --blue: #6d8dff; --blue-dim: #22284a; --blue-hover: #7f9bff;
  --green: #52d989; --green-dim: #142a1f; --green-text: #a4f0c0;
  --amber: #e0a730; --amber-dim: #332510;
  --red: #ff6b6b; --red-dim: #34191c; --red-border: #5a2a2e; --red-hover: #4a2226; --red-text: #ffb4b4;
  --gray-dim: #24232f; --gray: #9490a6;
  --btn-hover: #2a2938; --btn-border-hover: #3a3850;
  --field-bg: #0c0b10; --on-blue: #0d1020;
  --radius: 14px; --radius-sm: 9px;
}
* { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
html { overflow-x: hidden; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: var(--bg); color: var(--text); margin: 0; overflow-x: hidden; font-size: 15px;
  line-height: 1.45;
  /* место под фиксированное нижнее меню на телефоне, см. .bottom-nav */
  padding-bottom: calc(var(--nav-h) + 14px + var(--tg-bottom));
}
:focus-visible { outline: 2px solid var(--blue); outline-offset: 2px; }
.shell { display: flex; min-height: 100vh; }
.main-col { flex: 1 1 auto; min-width: 0; }
/* Верхняя шапка — на телефоне это единственная навигационная точка (бренд +
   выход), на широком экране бренд уже есть в боковом меню и там же выход,
   поэтому шапка превращается в узкую строку с названием текущей страницы. */
header {
  background: var(--bg-alt);
  padding: calc(12px + var(--tg-top)) 16px 12px; border-bottom: 1px solid var(--border-soft);
  position: sticky; top: 0; z-index: 10;
  display: flex; align-items: center; justify-content: space-between; gap: 10px;
}
header h1 { font-size: 16px; margin: 0; font-weight: 600; }
header .page-title { display: none; }
header .logout button { padding: 7px 13px; font-size: 12.5px; }
/* Боковое меню — только на широком экране, см. .side-nav display в media-запросе. */
.side-nav {
  display: none; flex-direction: column; width: var(--side-w); flex-shrink: 0;
  background: var(--bg-alt); border-right: 1px solid var(--border-soft);
  padding: 18px 12px; position: sticky; top: 0; height: 100vh; overflow-y: auto;
}
.side-brand { display: flex; align-items: center; gap: 9px; font-weight: 600; font-size: 15px;
              padding: 4px 8px 20px; }
.side-links { display: flex; flex-direction: column; gap: 2px; flex: 1; }
.side-nav .nav-link {
  display: flex; align-items: center; gap: 11px; padding: 10px 11px; border-radius: var(--radius-sm);
  color: var(--text-dim); text-decoration: none; font-size: 13.5px; transition: background .12s, color .12s;
}
.side-nav .nav-link .ic { font-size: 16px; line-height: 1; }
.side-nav .nav-link:hover { background: var(--card-hover); color: var(--text); }
.side-nav .nav-link.active { background: var(--accent-dim); color: var(--accent); font-weight: 600; }
.side-logout { margin-top: 10px; }
.side-logout button { width: 100%; justify-content: flex-start; padding-left: 11px; }
/* Нижнее меню на телефоне — дотягивается большим пальцем при работе одной
   рукой, в отличие от верхнего, где надо либо листать горизонтально, либо
   тянуться через весь экран. */
.bottom-nav {
  position: fixed; left: 0; right: 0; bottom: 0; z-index: 20;
  display: flex; background: var(--bg-alt); border-top: 1px solid var(--border-soft);
  padding-bottom: var(--tg-bottom); height: calc(var(--nav-h) + var(--tg-bottom));
}
.bottom-nav .nav-link {
  flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 3px; color: var(--text-dim); text-decoration: none; font-size: 12px; min-width: 0; padding: 0 2px;
}
.bottom-nav .nav-link .ic { font-size: 21px; line-height: 1; }
.bottom-nav .nav-link .lbl { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 100%; }
.bottom-nav .nav-link.active { color: var(--accent); }
main { max-width: 1000px; margin: 16px auto; padding: 0 14px; }
main.wide { max-width: 1180px; }
/* h2 — настоящий заголовок раздела (не декоративная ярлычная строка):
   крупнее и контрастнее обычного текста, без акцентного цвета — золотой
   зарезервирован только под бренд/активную вкладку, иначе взгляд цепляется
   за заголовки, а не за сами элементы управления. */
h2 { font-size: 20px; color: var(--text); font-weight: 700; margin: 30px 0 12px; letter-spacing: -.2px; }
/* Якорные переходы (например дашборд → #duplicates) иначе утыкаются
   заголовком прямо под залипающую шапку — застревает, накрытый ей. */
h2[id] { scroll-margin-top: calc(76px + var(--tg-top)); }
h2:first-child { margin-top: 4px; }
/* h3 — подзаголовок группы полей внутри карточки, нарочно тише и мельче h2,
   чтобы иерархия читалась с одного взгляда. */
h3 { font-size: 11px; color: var(--text-faint); margin: 0 0 10px; font-weight: 600;
     text-transform: uppercase; letter-spacing: .5px; padding-bottom: 6px; border-bottom: 1px solid var(--border-soft); }
.section-hint { color: var(--text-faint); font-size: 13px; margin: -6px 0 12px; }
.card {
  background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 15px 16px; margin-bottom: 12px;
}
.card.scroll { overflow-x: auto; }
.card + h2 { margin-top: 30px; }
.row { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-bottom: 8px; }
.row > * { flex: 1 1 220px; }
.row > button, .row > .btn { flex: 0 1 auto; }
.row:last-child { margin-bottom: 0; }
/* Строки полей формы (число+подпись+подсказка) — низ полей на одной линии,
   даже если у соседних подписи разной длины и переносятся по-разному. */
.field-row { display: flex; gap: 10px; flex-wrap: wrap; align-items: flex-end; margin-bottom: 14px; }
.field-row:last-child { margin-bottom: 0; }
.field-row > * { flex: 1 1 220px; }
.field { min-width: 0; }
/* Простая строка «подпись: значение» — не форма, без flex-разъезда полей */
.line { margin-bottom: 8px; line-height: 1.6; }
.line:last-child { margin-bottom: 0; }
label { display: block; font-size: 12px; color: var(--text-dim); font-weight: 500;
        letter-spacing: .2px; margin-bottom: 4px; min-height: 15px; }
label .unit { font-weight: 400; color: var(--text-faint); }
.field-hint { color: var(--text-faint); font-size: 11.5px; margin: 4px 0 0; }
input[type=text], input[type=password], input[type=number], textarea, select {
  background: var(--field-bg); color: var(--text); border: 1px solid var(--border); border-radius: 8px;
  padding: 10px 11px; font-size: 16px; width: 100%; font-family: inherit; transition: border-color .12s;
}
input:focus, textarea:focus, select:focus { outline: none; border-color: var(--blue); }
code {
  background: var(--field-bg); border: 1px solid var(--border-soft); border-radius: 5px;
  padding: 1px 6px; font-family: ui-monospace, "SF Mono", monospace; font-size: .9em; color: var(--blue-hover);
}
pre code { background: none; border: none; padding: 0; color: inherit; }
/* font-size меньше 16px в полях ввода — Safari на iOS зумит страницу при фокусе */
/* На телефоне высоту растягивает скрипт (autosizeTextareas в TG_INIT_SCRIPT)
   под конкретную страницу — здесь только запасной размер на случай, если он
   не отработал. На широком экране скрипт этого не делает (см. скрипт) —
   там высотой управляет только rows= в разметке плюс ручной resize. */
textarea { min-height: 45vh; resize: vertical; font-family: ui-monospace, monospace; font-size: 14px; }
button, .btn {
  background: var(--card-hover); color: var(--text); border: 1px solid var(--border);
  border-radius: 9px; padding: 11px 16px; font-size: 14px; cursor: pointer;
  min-height: 42px; display: inline-flex; align-items: center; justify-content: center;
  gap: 6px; text-decoration: none; transition: background .12s, border-color .12s, transform .06s;
}
button:hover, .btn:hover { background: var(--btn-hover); border-color: var(--btn-border-hover); }
button:active, .btn:active { transform: scale(.98); }
button.primary { background: var(--blue); border-color: var(--blue); color: var(--on-blue); font-weight: 600; }
button.primary:hover { background: var(--blue-hover); }
button.danger { background: var(--red-dim); border-color: var(--red-border); color: var(--red); }
button.danger:hover { background: var(--red-hover); }
button.icon, .btn.icon { min-height: 38px; min-width: 38px; padding: 6px; font-size: 15px; flex: 0 0 auto; }
.card-actions { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin-top: 12px; }
.link-btn {
  background: none; border: none; color: var(--text-faint); font-size: 12.5px; padding: 6px 2px;
  min-height: auto; text-decoration: underline; text-underline-offset: 2px;
}
.link-btn:hover { background: none; color: var(--text-dim); }
/* Ссылка «назад» — не второстепенное действие вроде сброса поля, а обычный
   переход; приглушённый вечно-подчёркнутый вид .link-btn читался бы как
   отключённая/посещённая ссылка. */
.back-link {
  display: inline-flex; align-items: center; gap: 4px; color: var(--text-dim);
  text-decoration: none; font-size: 13px; margin-bottom: 4px;
}
.back-link:hover { color: var(--text); text-decoration: underline; }
h2.page-heading.after-back { margin-top: 6px; }
.pill { display: inline-block; padding: 3px 10px; border-radius: 999px; font-size: 11.5px; font-weight: 600; }
.pill.on { background: var(--green-dim); color: var(--green); }
.pill.off { background: var(--red-dim); color: var(--red); }
.pill.warn { background: var(--amber-dim); color: var(--amber); }
.pill.neutral { background: var(--gray-dim); color: var(--gray); }
.flash { padding: 12px 14px; border-radius: var(--radius-sm); margin-bottom: 16px; font-size: 14px; }
.flash.ok { background: var(--green-dim); color: var(--green-text); }
.flash.err { background: var(--red-dim); color: var(--red-text); }
.muted { color: var(--text-dim); font-size: 12.5px; }
.mono { font-family: ui-monospace, "SF Mono", monospace; font-size: 12.5px; overflow-wrap: anywhere; }
.mono.ellipsis { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; display: block; }
pre.post { white-space: pre-wrap; word-break: break-word; background: var(--field-bg); border: 1px solid var(--border);
           border-radius: var(--radius-sm); padding: 10px 12px; font-size: 13px; }
form.inline { display: inline; }
hr.sep { border: none; border-top: 1px solid var(--border-soft); margin: 16px 0; }
/* <details>/<summary> без родного треугольника браузера — свой шеврон,
   который разворачивается при открытии. */
summary.disclosure {
  cursor: pointer; color: var(--blue-hover); font-weight: 600; list-style: none;
  display: flex; align-items: center; gap: 6px;
}
summary.disclosure::-webkit-details-marker { display: none; }
summary.disclosure::before { content: "›"; display: inline-block; transition: transform .12s; font-weight: 700; }
details[open] > summary.disclosure::before { transform: rotate(90deg); }
table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
td, th { text-align: left; padding: 7px 6px; border-bottom: 1px solid var(--border-soft); vertical-align: top; }
table.kv td:first-child { color: var(--text-dim); white-space: nowrap; padding-right: 20px; width: 1%; }
/* Списки (ленты, посты) — карточка-контейнер один раз снаружи, строки внутри
   разделены волосяными линиями, а не вложенными собственными рамками —
   иначе получаются рамка в рамке и повторный фон-«приподнятие». */
.list { background: var(--card); border: 1px solid var(--border); border-radius: var(--radius); overflow: hidden; }
.list-item {
  display: flex; align-items: center; gap: 10px; padding: 13px 16px;
  border-bottom: 1px solid var(--border-soft); transition: background .12s;
  color: inherit; text-decoration: none;
}
a.list-item:hover { background: var(--card-hover); }
.list-item:last-child { border-bottom: none; }
.list-item-info { flex: 1 1 auto; min-width: 0; }
.list-item-title { color: var(--text); font-size: 14px; overflow-wrap: break-word; }
.list-item-actions { display: flex; gap: 6px; flex-shrink: 0; align-items: center; }
.list-item-chevron { color: var(--text-faint); font-size: 18px; flex-shrink: 0; }
/* Дашборд: сетка карточек-метрик вместо списка строк «подпись: значение» —
   легче окинуть взглядом состояние бота целиком. */
.stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(170px, 100%), 1fr)); gap: 10px;
             margin-bottom: 12px; }
.stat-card { background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
             padding: 13px 14px; display: flex; flex-direction: column; justify-content: space-between; min-height: 72px; }
.stat-card .stat-label { font-size: 11px; text-transform: uppercase; letter-spacing: .3px;
                          color: var(--text-faint); margin-bottom: 7px; }
.stat-card .stat-value { font-size: 14.5px; color: var(--text); overflow-wrap: anywhere; }
.hero-card {
  background: var(--card); border: 1px solid var(--border); border-left: 3px solid var(--green);
  border-radius: var(--radius); padding: 18px; margin-bottom: 14px;
  display: flex; align-items: center; justify-content: space-between; gap: 14px; flex-wrap: wrap;
}
.hero-card.paused { border-left-color: var(--amber); }
.hero-card.debug { border-left-color: var(--blue); }
.hero-card .hero-state { font-size: 20px; font-weight: 700; display: flex; align-items: center; gap: 9px; }
.hero-card .hero-dot { width: 10px; height: 10px; border-radius: 50%; background: var(--green); flex-shrink: 0; }
.hero-card.paused .hero-dot { background: var(--amber); }
.hero-card.debug .hero-dot { background: var(--blue); }
.hero-card .hero-sub { color: var(--text-dim); font-size: 12.5px; margin-top: 4px; }
/* Выбор ИИ-бэкенда в настройках — три варианта в виде селектируемых строк
   вместо трёх отдельных карточек-дублей с одинаковой формой включения. */
.ai-option {
  display: grid; grid-template-columns: 16px 1fr; gap: 4px 11px; align-items: start;
  padding: 12px; border-radius: var(--radius-sm); position: relative; cursor: pointer;
  border: 1px solid var(--border); margin-bottom: 8px; transition: border-color .12s, background .12s;
  min-height: 56px;
}
.ai-option:hover { border-color: var(--btn-border-hover); background: var(--card-hover); }
.ai-option input[type=radio] { accent-color: var(--blue); width: 16px; height: 16px; margin-top: 2px; cursor: pointer; }
/* Кликабельна вся строка, не только текст подписи — ::after растягивает
   область клика на всю карточку поверх остального содержимого. */
.ai-option-label { min-width: 0; cursor: pointer; align-self: center; }
.ai-option-label::after { content: ""; position: absolute; inset: 0; }
.ai-option-title { font-size: 14px; color: var(--text); display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
.ai-option:has(input:checked) { border-color: var(--blue); background: var(--blue-dim); }
/* Переключатели («Поведение» в параметрах публикации) — подпись+подсказка
   справа от чекбокса, весь ряд кликабелен целиком. */
.toggle-row { display: flex; align-items: flex-start; gap: 9px; margin-bottom: 12px; cursor: pointer; }
.toggle-row:last-child { margin-bottom: 0; }
.toggle-row input[type=checkbox] { margin-top: 3px; width: 16px; height: 16px; flex-shrink: 0; accent-color: var(--blue); }
.toggle-title { color: var(--text); font-size: 13.5px; }
/* Группы полей в «Параметры публикации» — на широком экране раскладываются
   по колонкам, на узком идут одна под другой. */
.field-groups { display: block; }
.settings-group { margin-bottom: 18px; }
.settings-group:last-child { margin-bottom: 0; }
@media (max-width: 640px) {
  /* Инлайновые flex:2/flex:1 в формах (например «Ленты» — url шире названия)
     хороши на широком экране; на узком любой из них всё равно должен
     занимать всю ширину, иначе поле для ввода URL становится нечитаемо
     узким. !important нужен только против инлайновых стилей. */
  .row > *, .field-row > * { flex: 1 1 100% !important; }
  .hero-card { flex-direction: column; align-items: stretch; }
  /* .row делает форму на всю ширину, но не кнопку внутри неё (у button своя,
     по содержимому) — без этого на узком экране кнопки паузы/проверки
     стоят полноширинными формами с кнопкой-огрызком по размеру текста. */
  .hero-card .row button { width: 100%; }
  /* Основная кнопка карточки — на весь ряд и на всю ширину: внизу узкого
     экрана угловая auto-width кнопка — худшая зона для дотягивания пальцем. */
  .card-actions { flex-direction: column; align-items: stretch; }
  .card-actions button.primary { width: 100%; }
  .card-actions .link-btn { align-self: center; }
}
@media (min-width: 641px) {
  .side-nav { display: flex; }
  .bottom-nav { display: none; }
  body { padding-bottom: 0; }
  header .logout { display: none; }
  header { padding-left: 24px; padding-right: 24px; }
  header h1.brand-mobile { display: none; }
  header .page-title { display: block; }
  /* На узком экране это единственный заголовок страницы (в шапке — только
     бренд), поэтому там он нужен; на широком его дублирует page-title в
     шапке — второй раз просто не рендерим. */
  h2.page-heading { display: none; }
  main { padding: 0 24px; margin: 26px auto; }
  .card { padding: 17px 20px; }
  .row > * { flex: 1 1 auto; }
  .field-groups { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 4px 28px; align-items: start; }
  .content-grid, .settings-columns { display: grid; grid-template-columns: 1fr 1fr; gap: 0 24px; align-items: start; }
  /* Промпт и формат — сознательно парная пара карточек рядом; тянем их до
     одной высоты, чтобы кнопки под ними не разъезжались на разных уровнях. */
  .content-grid { align-items: stretch; }
  .content-grid > div { display: flex; flex-direction: column; }
  .content-grid .card { display: flex; flex-direction: column; flex: 1; }
  .content-grid .card form { display: flex; flex-direction: column; flex: 1; }
  .content-grid textarea { flex: 1; }
  /* На телефоне textarea растягивает JS (autosizeTextareas) под весь экран —
     там это единственный удобный способ редактировать длинный текст пальцем.
     На широком экране места достаточно и без этого: высоту задаёт rows= в
     разметке (у каждого поля своя, под типичную длину именно этого текста),
     а не общий на все поля процент от вьюпорта — иначе короткие поля вроде
     формата поста получают тот же пустой километраж, что и длинный промпт. */
  textarea { min-height: unset; height: auto !important; }
}
"""


# Открыто как Telegram Mini App (кнопка /panel или меню бота) — SDK молча
# ни на что не влияет вне Telegram. ready()/expand() разворачивают на весь
# экран, а --tg-top/--tg-bottom дают шапке отступ от родного заголовка
# Telegram в полноэкранном режиме (см. STYLE выше).
TG_INIT_SCRIPT = """<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script>
(function () {
  function cssPx(name, fallback) {
    var v = parseFloat(getComputedStyle(document.documentElement).getPropertyValue(name));
    return isNaN(v) ? fallback : v;
  }
  // Растягивает textarea от текущей позиции (какая бы она ни была на
  // конкретной странице — это и есть вся разница с фиксированным числом
  // в CSS) до низа экрана, оставляя место под нижнее меню и кнопки под полем.
  function autosizeTextareas() {
    // Растягивание на весь экран — приём для тесного телефонного экрана
    // (нижнее меню видно только там). На широком экране места и так
    // достаточно, а во весь вьюпорт textarea выглядит нелепо пустой —
    // там просто оставляем CSS-высоту (см. textarea в STYLE) и ручной resize.
    var bottomNav = document.querySelector('.bottom-nav');
    var navVisible = bottomNav && bottomNav.offsetParent !== null;
    if (!navVisible) return;
    var mainEl = document.querySelector('main');
    if (!mainEl) return;
    // Раньше резерв под то, что идёт после поля (кнопки, подсказки, вторая
    // форма — на разных страницах их разное количество), был одним и тем же
    // числом для всех страниц — где-то с запасом, где-то впритык так, что
    // кнопку под полем перекрывало нижним меню. Теперь меряем реальную
    // высоту того, что идёт после textarea (до конца main), а не гадаем.
    var reserve = cssPx('--nav-h', 58) + cssPx('--tg-bottom', 0) + 14;
    var list = document.querySelectorAll('textarea');
    for (var i = 0; i < list.length; i++) {
      var ta = list[i];
      ta.style.height = 'auto';
      var taRect = ta.getBoundingClientRect();
      var mainRect = mainEl.getBoundingClientRect();
      var following = Math.max(0, mainRect.bottom - taRect.bottom);
      var h = window.innerHeight - reserve - taRect.top - following;
      h = Math.max(160, Math.min(h, window.innerHeight * 0.7));
      ta.style.height = h + 'px';
    }
  }
  window.addEventListener('load', autosizeTextareas);
  window.addEventListener('resize', autosizeTextareas);

  var tg = window.Telegram && window.Telegram.WebApp;
  if (!tg) return;
  try { tg.ready(); tg.expand(); } catch (e) {}
  function applyInsets() {
    var sa = tg.safeAreaInset || {}, csa = tg.contentSafeAreaInset || {};
    document.documentElement.style.setProperty('--tg-top', ((sa.top||0)+(csa.top||0)) + 'px');
    document.documentElement.style.setProperty('--tg-bottom', ((sa.bottom||0)+(csa.bottom||0)) + 'px');
    autosizeTextareas();
  }
  applyInsets();
  if (tg.onEvent) {
    tg.onEvent('safeAreaChanged', applyInsets);
    tg.onEvent('contentSafeAreaChanged', applyInsets);
    tg.onEvent('viewportChanged', applyInsets);
  }
})();
</script>"""


# (путь, иконка, подпись) — единый источник для нижнего/бокового меню на
# каждой странице. Шаблон промпта и формат поста были отдельными пунктами —
# слили в один «Контент» (bot/web.py: content_get), это два тесно связанных
# поля одной и той же настройки «как ИИ обрабатывает новость».
NAV_ITEMS = [
    ("/", "📊", "Статус"),
    ("/feeds", "📰", "Ленты"),
    ("/content", "📝", "Контент"),
    ("/settings", "⚙️", "Настройки"),
    ("/posts", "📮", "Посты"),
]


async def _usage_body(pub: "Publisher") -> str:
    if pub.quota is None:
        return ""
    info = await pub.quota.snapshot(force=True)
    # Модель уже видна в карточке «Обрабатывает» на дашборде — здесь незачем
    # повторять её ещё раз, пометка «бесплатная» тоже туда не влезает без
    # лишней возни, а тут скорее про сам расход, а не про то, что за модель.
    rows = [
        ("Запросов сегодня", f"{info.requests}" + (f" из {info.request_limit} ({info.request_pct:.0f}%)" if info.request_limit else "")),
        ("Токены", f"{info.tokens_in} вход / {info.tokens_out} выход"),
    ]
    if info.request_limit:
        rows.append(("Обнуление лимита", f"через {until_reset()} (00:00 UTC), источник: {info.limit_source}"))
    if info.credit_limit is not None:
        rows.append(("Кредиты на ключе", f"{info.credit_limit:.4f}, осталось {info.credit_remaining:.4f}"))
    return ("<h2>Расход за сутки</h2>"
            "<div class='section-hint'>Только обычный режим — у Claude и Gemini свой счёт.</div>"
            "<div class='card scroll'><table class='kv'>") + "".join(
        f"<tr><td class='muted'>{_e(k)}</td><td>{_e(v)}</td></tr>" for k, v in rows
    ) + "</table></div>"


def _layout(title: str, body: str, flash: str = "", flash_kind: str = "ok", active: str = "",
           wide: bool = False) -> str:
    flash_html = f'<div class="flash {flash_kind}">{flash}</div>' if flash else ""

    nav_html = "".join(
        f'<a href="{path}" class="nav-link{" active" if path == active else ""}">'
        f'<span class="ic">{icon}</span><span class="lbl">{label}</span></a>'
        for path, icon, label in NAV_ITEMS
    )
    main_class = " wide" if wide else ""
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)} — bot panel</title>
{TG_INIT_SCRIPT}
<style>{STYLE}</style></head><body>
<div class="shell">
  <aside class="side-nav">
    <div class="side-brand">📰 RSS → канал</div>
    <nav class="side-links">{nav_html}</nav>
    <form class="side-logout" method="post" action="/logout"><button>🚪 Выйти</button></form>
  </aside>
  <div class="main-col">
    <header>
      <h1 class="brand-mobile">📰 RSS → канал</h1>
      <h1 class="page-title">{_e(title)}</h1>
      <form class="inline logout" method="post" action="/logout"><button>Выйти</button></form>
    </header>
    <main class="{main_class}">{flash_html}{body}</main>
  </div>
</div>
<nav class="bottom-nav">{nav_html}</nav>
</body></html>"""


def _login_page(error: str = "") -> str:
    err_html = f'<div class="flash err">{_e(error)}</div>' if error else ""
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Вход — bot panel</title>
{TG_INIT_SCRIPT}
<style>{STYLE}</style></head><body>
<main style="max-width:360px; margin:calc(15vh + var(--tg-top)) auto 0;">
  <div style="text-align:center; font-size:34px; margin-bottom:6px;">📰</div>
  <h2 style="justify-content:center;">Вход в панель</h2>
  {err_html}
  <div id="tgLoginNote" class="flash ok" style="display:none;">Вхожу через Telegram…</div>
  <div class="card">
    <form method="post" action="/login">
      <label>Пароль</label>
      <input type="password" name="password" autofocus required>
      <div style="margin-top:12px;"><button class="primary" type="submit" style="width:100%;">Войти</button></div>
    </form>
  </div>
</main>
<script>
(function () {{
  var tg = window.Telegram && window.Telegram.WebApp;
  if (!tg || !tg.initData) return;
  document.getElementById('tgLoginNote').style.display = 'block';
  fetch('/tg-login', {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{initData: tg.initData}})
  }}).then(function (r) {{
    if (r.ok) {{ location.href = '/'; }}
    else {{ document.getElementById('tgLoginNote').style.display = 'none'; }}
  }}).catch(function () {{ document.getElementById('tgLoginNote').style.display = 'none'; }});
}})();
</script>
</body></html>"""


def _redirect(path: str) -> web.HTTPFound:
    return web.HTTPFound(path)


# ======================== приложение ========================
def create_app(storage: Storage, publisher: Publisher, bot: Bot, password: str,
               admin_ids: set[int] | None = None) -> web.Application:
    auth = WebAuth(password)
    admin_ids = admin_ids or set()
    app = web.Application()
    app["auth"] = auth
    app["st"] = storage
    app["publisher"] = publisher
    app["bot"] = bot

    PUBLIC_PATHS = {"/login", "/tg-login"}

    @web.middleware
    async def auth_middleware(request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]):
        if request.path in PUBLIC_PATHS:
            return await handler(request)
        session = auth.verify(request.cookies.get(SESSION_COOKIE))
        if session is None:
            return _redirect("/login")
        request["csrf"] = session["csrf"]
        if request.method == "POST":
            form = await request.post()
            if not secrets.compare_digest(str(form.get("csrf", "")), session["csrf"]):
                return web.Response(status=403, text="CSRF-токен не совпадает — обновите страницу и попробуйте снова.")
            request["form"] = form
        return await handler(request)

    app.middlewares.append(auth_middleware)

    def csrf_field(request: web.Request) -> str:
        return f'<input type="hidden" name="csrf" value="{_e(request["csrf"])}">'

    # --- аутентификация ---------------------------------------------------
    async def login_get(request: web.Request) -> web.Response:
        if auth.verify(request.cookies.get(SESSION_COOKIE)):
            return _redirect("/")
        return web.Response(text=_login_page(), content_type="text/html")

    async def login_post(request: web.Request) -> web.Response:
        ip = request.remote or "?"
        if auth.locked_out(ip):
            return web.Response(
                text=_login_page(f"Слишком много попыток — подождите {LOGIN_LOCKOUT // 60} минут."),
                content_type="text/html", status=429)
        form = await request.post()
        if not auth.check(str(form.get("password", ""))):
            auth.record_fail(ip)
            return web.Response(text=_login_page("Неверный пароль."), content_type="text/html", status=401)
        auth.record_success(ip)
        token = auth.new_session()
        resp = _redirect("/")
        resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL, httponly=True, samesite="Lax")
        return resp

    async def logout_post(request: web.Request) -> web.Response:
        auth.revoke(request.cookies.get(SESSION_COOKIE))
        resp = _redirect("/login")
        resp.del_cookie(SESSION_COOKIE)
        return resp

    async def tg_login_post(request: web.Request) -> web.Response:
        """Авто-вход без пароля, когда панель открыта Web App-кнопкой в
        Telegram: initData подписан ботом, доверяем ей вместо пароля — но
        только для тех, кто и так может управлять ботом командами (ADMIN_IDS).
        Не троганное CSRF-мидлварой — сессии тут ещё нет, а подделать
        initData без BOT_TOKEN невозможно, ровно как и подобрать пароль."""
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "bad_request"}, status=400)
        user = verify_telegram_init_data(str(payload.get("initData", "")), bot.token)
        if user is None:
            return web.json_response({"ok": False, "error": "invalid_signature"}, status=401)
        if int(user["id"]) not in admin_ids:
            return web.json_response({"ok": False, "error": "not_admin"}, status=403)
        token = auth.new_session()
        resp = web.json_response({"ok": True})
        resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL, httponly=True, samesite="Lax")
        return resp

    # --- статус -------------------------------------------------------------
    async def dashboard(request: web.Request) -> web.Response:
        st: Storage = app["st"]
        pub: Publisher = app["publisher"]
        feeds = st.feeds()
        active = sum(1 for f in feeds if f["enabled"])
        errors = [f for f in feeds if f["last_error"]]
        paused = st.get("paused") == "1"
        hero_class, state_text = ("paused", "На паузе")
        if pub.debug:
            hero_class, state_text = "debug", "Отладка — посты в личку"
        elif not paused:
            hero_class, state_text = "", "Работает"
        feeds_value = f"{active} / {len(feeds)}"
        if errors:
            feeds_value += f' <span class="pill off">{len(errors)} с ошибкой</span>'
        dupes = st.count_dedup_candidates()
        stats = [
            ("Модель", _e(pub.active_backend_label)),
            ("Канал", _e(pub.channel or "не задан")),
            ("Ленты (активно/всего)", feeds_value),
            ("VK", ('<span class="pill on">' + _e(pub.vk_group) + '</span>')
                   if pub.vk_on else '<span class="pill neutral">выключен</span>'),
        ]
        if dupes:
            stats.append(("Дубли", f'<a href="/feeds#duplicates" class="pill warn" style="text-decoration:none;">{dupes} на разбор ›</a>'))
        stat_html = "".join(
            f'<div class="stat-card"><div class="stat-label">{_e(k)}</div>'
            f'<div class="stat-value">{v}</div></div>' for k, v in stats
        )
        body = f"""
        <div class="hero-card {hero_class}">
          <div>
            <div class="hero-state"><span class="hero-dot"></span>{_e(state_text)}</div>
            <div class="hero-sub">Публикация новостей в канал</div>
          </div>
          <div class="row" style="margin:0;">
            <form method="post" action="/pause"><input type="hidden" name="csrf" value="{_e(request['csrf'])}">
              <button class="{'primary' if paused else ''}" type="submit">
                {'▶️ Возобновить' if paused else '⏸ Приостановить'}
              </button></form>
            <form method="post" action="/checknow"><input type="hidden" name="csrf" value="{_e(request['csrf'])}">
              <button type="submit">🔄 Проверить сейчас</button></form>
          </div>
        </div>
        <div class="stat-grid">{stat_html}</div>
        """
        body += await _usage_body(pub)
        return web.Response(text=_layout("Статус", body, active="/", wide=True), content_type="text/html")

    async def pause_post(request: web.Request) -> web.Response:
        st: Storage = app["st"]
        st.set("paused", "0" if st.get("paused") == "1" else "1")
        return _redirect("/")

    async def checknow_post(request: web.Request) -> web.Response:
        pub: Publisher = app["publisher"]
        pub.wake()
        return _redirect("/")

    # --- ленты ---------------------------------------------------------------
    def _dupes_section_html(st: "Storage") -> str:
        """Дубли между лентами показываются здесь же, на «Лентах» — это тоже
        решение по выдаче лент, а не отдельная самостоятельная сущность.
        Пусто — секция не рендерится вовсе, чтобы не мозолить глаза, когда
        разбирать нечего (как бейдж на дашборде)."""
        dupes = st.dedup_candidates(50)
        if not dupes:
            return ""
        items = ""
        for r in dupes:
            matched = st.post(r["matched_post_id"]) if r["matched_post_id"] else None
            matched_html = (f'<a href="/posts/{r["matched_post_id"]}">пост #{r["matched_post_id"]}</a>'
                            if matched else f'пост #{r["matched_post_id"]} (уже удалён)')
            thumb = (f'<img src="{_safe_href(r["image"])}" alt="" '
                    f'style="width:64px; height:64px; object-fit:cover; border-radius:8px; flex-shrink:0;">'
                    if r["image"] else '<div style="width:64px; height:64px; border-radius:8px; '
                    'background:var(--field-bg); flex-shrink:0;"></div>')
            when = time.strftime("%d.%m %H:%M", time.localtime(r["detected_at"]))
            items += f"""<div class="list-item">
              {thumb}
              <div class="list-item-info">
                <div class="list-item-title">{_e(r['title'][:140])} <span class="pill neutral">{r['score']:.0%}</span></div>
                <div class="muted">{_e(r['source'] or 'без ленты')} · найдено {when} · похоже на {matched_html}</div>
              </div>
              <div class="list-item-actions">
                <a class="btn icon" href="/duplicates/{r['id']}" title="Подробнее">›</a>
              </div>
            </div>"""
        return f"""
        <h2 id="duplicates">Дубли <span class="muted" style="font-weight:400;">({len(dupes)})</span></h2>
        <div class="section-hint">Похожи на уже опубликованные с другой ленты — не в канале, ждут решения.</div>
        <div class="list">{items}</div>
        """

    def _feed_row_html(f: sqlite3.Row, request: web.Request, st: "Storage") -> str:
        """Строка ленты или сайта без RSS — разметка общая: управление (свой
        промпт, несколько картинок, пауза, удаление) не зависит от того,
        откуда берутся новости."""
        checked = time.strftime("%d.%m %H:%M", time.localtime(f["last_check"])) if f["last_check"] else "—"
        err = f'<div class="muted" style="color:var(--red)">{_e(f["last_error"][:150])}</div>' if f["last_error"] else ""
        own_prompt = ' <span class="pill neutral">свой промпт</span>' if f["template"] else ""
        multi = ' <span class="pill neutral">неск. картинок</span>' if f["multi_images"] else ""
        path_hint = (f' <span class="pill neutral">{_e(f["article_path"])}</span>'
                    if f["kind"] == "sitemap" and f["article_path"] else "")
        return f"""<div class="list-item">
          <div class="list-item-info">
            <div class="list-item-title">
              <b>#{f['id']}</b> {_e(f['title'] or '(без названия)')}
              <span class="pill {'on' if f['enabled'] else 'neutral'}">{'вкл' if f['enabled'] else 'пауза'}</span>{own_prompt}{multi}{path_hint}
            </div>
            <div class="muted mono ellipsis">{_e(f['url'])}</div>
            <div class="muted">проверена: {checked} · в архиве: {st.seen_count(f['id'])}</div>
            {err}
          </div>
          <div class="list-item-actions">
            <a class="btn icon" href="/feeds/{f['id']}/template" title="Свой промпт">🤖</a>
            <form class="inline" method="post" action="/feeds/{f['id']}/multiimages">{csrf_field(request)}
              <button class="icon" type="submit" title="{'Одна картинка, как раньше' if f['multi_images'] else 'Публиковать несколько картинок альбомом'}">🖼</button></form>
            <form class="inline" method="post" action="/feeds/{f['id']}/toggle">{csrf_field(request)}
              <button class="icon" type="submit" title="{'Поставить на паузу' if f['enabled'] else 'Включить'}">{'⏸' if f['enabled'] else '▶️'}</button></form>
            <form class="inline" method="post" action="/feeds/{f['id']}/delete"
                  onsubmit="return confirm('Удалить #{f['id']}?')">{csrf_field(request)}
              <button class="icon" type="submit" title="Удалить">✕</button></form>
          </div>
        </div>"""

    async def feeds_get(request: web.Request, flash: str = "", flash_kind: str = "ok") -> web.Response:
        st: Storage = app["st"]
        dupes_html = _dupes_section_html(st)
        rows = st.feeds()
        rss_rows = [f for f in rows if f["kind"] != "sitemap"]
        sitemap_rows = [f for f in rows if f["kind"] == "sitemap"]

        rss_list = "".join(_feed_row_html(f, request, st)
                           for f in rss_rows) or (
            "<div style='padding:28px 16px; text-align:center;'>"
            "<div style='font-size:28px; margin-bottom:8px;'>📰</div>"
            "<div class='muted'>Лент пока нет — добавьте первую выше.</div></div>"
        )
        sitemap_list = "".join(_feed_row_html(f, request, st)
                               for f in sitemap_rows) or (
            "<div style='padding:28px 16px; text-align:center;'>"
            "<div style='font-size:28px; margin-bottom:8px;'>🗺️</div>"
            "<div class='muted'>Сайтов без RSS пока нет — добавьте первый выше.</div></div>"
        )

        body = f"""
        {dupes_html}
        <h2>Ленты <span class="muted" style="font-weight:400;">({len(rss_rows)})</span></h2>
        <details>
          <summary class="disclosure">Добавить ленту</summary>
          <div class="card" style="margin-top:10px;">
            <form method="post" action="/feeds/add">{csrf_field(request)}
              <div class="row" style="align-items:flex-end;">
                <div style="flex:2;"><label>URL ленты</label><input type="text" name="url" placeholder="https://example.com/rss" required></div>
                <div style="flex:1;"><label>Название (необязательно)</label><input type="text" name="title"></div>
                <button class="primary" type="submit">Добавить</button>
              </div>
            </form>
          </div>
        </details>
        <div class="list" style="margin-top:10px;">{rss_list}</div>

        <h2>Сайты без RSS <span class="muted" style="font-weight:400;">({len(sitemap_rows)})</span></h2>
        <div class="section-hint">Новости с сайтов, где нет RSS-ленты — проверяются через sitemap.xml сайта,
          без браузера и без JS, обычный лёгкий запрос раз в цикл опроса.</div>
        <details>
          <summary class="disclosure">Добавить сайт без RSS</summary>
          <div class="card" style="margin-top:10px;">
            <form method="post" action="/feeds/add-sitemap">{csrf_field(request)}
              <div class="row" style="align-items:flex-end;">
                <div style="flex:2;"><label>Адрес сайта</label><input type="text" name="url" placeholder="https://example.com/" required></div>
                <div style="flex:1;"><label>Название (необязательно)</label><input type="text" name="title"></div>
              </div>
              <div class="row" style="align-items:flex-end; margin-top:8px;">
                <div style="flex:2;"><label>Часть адреса статей (необязательно)</label>
                  <input type="text" name="article_path" placeholder="/articles/"></div>
                <button class="primary" type="submit">Добавить</button>
              </div>
              <div class="field-hint" style="margin-top:4px;">Нужна, если в sitemap сайта вперемешку и
                новости, и другие страницы (товары, категории) — без неё заберём всё подряд.</div>
            </form>
          </div>
        </details>
        <div class="list" style="margin-top:10px;">{sitemap_list}</div>
        """
        return web.Response(text=_layout("Ленты", body, flash, flash_kind, active="/feeds"), content_type="text/html")

    async def feeds_add(request: web.Request) -> web.Response:
        form = request["form"]
        url = str(form.get("url", "")).strip()
        title = str(form.get("title", "")).strip()
        if not url.startswith(("http://", "https://")):
            return await feeds_get(request, "Нужна ссылка, начинающаяся на http:// или https://", "err")
        result = await fetch(url)
        if result.error:
            return await feeds_get(request, f"Лента недоступна: {result.error}", "err")
        if not result.entries:
            return await feeds_get(request, "В ленте нет записей — проверьте адрес.", "err")
        feed_id = app["st"].add_feed(url, title or result.feed_title[:120])
        if feed_id is None:
            return await feeds_get(request, "Такая лента уже добавлена.", "err")
        app["publisher"].wake()
        return _redirect("/feeds")

    async def feeds_add_sitemap(request: web.Request) -> web.Response:
        form = request["form"]
        url = str(form.get("url", "")).strip()
        title = str(form.get("title", "")).strip()
        article_path = str(form.get("article_path", "")).strip()
        if not url.startswith(("http://", "https://")):
            return await feeds_get(request, "Нужна ссылка, начинающаяся на http:// или https://", "err")
        sitemap_url = await discover_sitemap(url)
        if sitemap_url is None:
            return await feeds_get(
                request,
                "Не нашли sitemap.xml — ни в robots.txt, ни по стандартному адресу. "
                "Для этого сайта такой способ не подойдёт.", "err")
        result = await fetch_sitemap(sitemap_url, article_path)
        if result.error:
            return await feeds_get(request, f"sitemap.xml недоступен: {result.error}", "err")
        if not result.entries:
            return await feeds_get(
                request,
                "sitemap.xml прочитался, но подходящих записей не нашлось — "
                "проверьте «часть адреса статей», если она заполнена.", "err")
        feed_id = app["st"].add_feed(sitemap_url, title, kind="sitemap", article_path=article_path)
        if feed_id is None:
            return await feeds_get(request, "Такой sitemap уже добавлен.", "err")
        app["publisher"].wake()
        return _redirect("/feeds")

    async def feeds_delete(request: web.Request) -> web.Response:
        feed_id = int(request.match_info["id"])
        app["st"].delete_feed(feed_id)
        return _redirect("/feeds")

    async def feeds_toggle(request: web.Request) -> web.Response:
        feed_id = int(request.match_info["id"])
        st: Storage = app["st"]
        row = st.feed(feed_id)
        if row is not None:
            st.set_enabled(feed_id, not row["enabled"])
        return _redirect("/feeds")

    async def feeds_toggle_multi(request: web.Request) -> web.Response:
        feed_id = int(request.match_info["id"])
        st: Storage = app["st"]
        row = st.feed(feed_id)
        if row is not None:
            st.set_multi_images(feed_id, not row["multi_images"])
        return _redirect("/feeds")

    # --- свой промпт для отдельной ленты ----------------------------------
    async def feed_template_get(request: web.Request, draft: str | None = None,
                                flash: str = "", flash_kind: str = "ok") -> web.Response:
        st: Storage = app["st"]
        feed_id = int(request.match_info["id"])
        feed = st.feed(feed_id)
        if feed is None:
            raise web.HTTPNotFound(text="Лента не найдена")
        text = draft if draft is not None else (feed["template"] or "")
        is_custom = bool(feed["template"])
        reset_form = (
            f'<form id="reset-feed-template-{feed_id}" method="post" '
            f'action="/feeds/{feed_id}/template/reset">{csrf_field(request)}</form>'
            if is_custom else ""
        )
        reset_btn = (f'<button type="submit" form="reset-feed-template-{feed_id}" class="link-btn" '
                    f'onclick="return confirm(\'Вернуть общий промпт? Свой текст для этой ленты будет потерян.\')">'
                    f'Вернуть общий промпт</button>' if is_custom else "")
        body = f"""
        <div><a href="/feeds" class="back-link">‹ Все ленты</a></div>
        <h2 class="page-heading after-back">Промпт ленты #{feed_id}</h2>
        <div class="card">
          <div class="line">{_e(feed['title'] or feed['url'])}
            <span class="pill neutral">{'свой промпт' if is_custom else 'общий промпт'}</span></div>
          <hr class="sep">
          <div class="section-hint" style="margin-top:0;">Плейсхолдеры: <code>{{title}}</code> <code>{{summary}}</code>
            <code>{{link}}</code> <code>{{source}}</code> <code>{{published}}</code></div>
          <form method="post" action="/feeds/{feed_id}/template">{csrf_field(request)}
            <label>Свой промпт для этой ленты (пусто — использовать общий из «Контент»)</label>
            <textarea name="text" rows="{_rows_for(text)}"
                      placeholder="Пусто — используется общий промпт из «Контент»">{_e(text)}</textarea>
            <div class="card-actions">
              <button class="primary" type="submit">Сохранить</button>
              {reset_btn}
            </div>
          </form>
          {reset_form}
        </div>
        <div class="card">
          <details>
            <summary class="disclosure">Общий промпт — для сравнения</summary>
            <pre class="post" style="margin-top:10px;">{_e(st.get('template'))}</pre>
          </details>
        </div>
        """
        return web.Response(text=_layout(f"Промпт ленты #{feed_id}", body, flash, flash_kind, active="/feeds"),
                            content_type="text/html")

    async def feed_template_post(request: web.Request) -> web.Response:
        st: Storage = app["st"]
        feed_id = int(request.match_info["id"])
        feed = st.feed(feed_id)
        if feed is None:
            raise web.HTTPNotFound(text="Лента не найдена")
        text = str(request["form"].get("text", "")).strip()
        if not text:
            st.update_feed(feed_id, template=None)
            return await feed_template_get(request, flash="Убрано — используется общий промпт.")
        if "{summary}" not in text and "{title}" not in text:
            return await feed_template_get(
                request, draft=text,
                flash="В промпте нет ни {title}, ни {summary} — модель не получит новость. Не сохранено.",
                flash_kind="err")
        st.update_feed(feed_id, template=text)
        return await feed_template_get(request, flash="Сохранено.")

    async def feed_template_reset(request: web.Request) -> web.Response:
        feed_id = int(request.match_info["id"])
        app["st"].update_feed(feed_id, template=None)
        return _redirect(f"/feeds/{feed_id}/template")

    # --- контент: промпт + формат поста --------------------------------------
    # Раньше это были два отдельных пункта меню («Шаблон», «Формат») — слил в
    # один, они настраивают один и тот же результат (что за текст получится
    # у ИИ и как он попадёт в пост) и обычно правятся вместе.
    async def content_get(request: web.Request, flash: str = "", flash_kind: str = "ok",
                          template_draft: str | None = None, format_draft: str | None = None
                          ) -> web.Response:
        st: Storage = app["st"]
        template_text = template_draft if template_draft is not None else st.get("template")
        format_text = format_draft if format_draft is not None else st.get("post_format")
        body = f"""
        <div class="content-grid">
        <div>
        <h2>Промпт для ИИ</h2>
        <div class="section-hint">Плейсхолдеры: <code>{{title}}</code> <code>{{summary}}</code>
          <code>{{link}}</code> <code>{{source}}</code> <code>{{published}}</code></div>
        <div class="card">
          <form method="post" action="/content/prompt">{csrf_field(request)}
            <textarea name="text" rows="{_rows_for(template_text)}">{_e(template_text)}</textarea>
            <div class="card-actions">
              <button class="primary" type="submit">Сохранить промпт</button>
              <button type="submit" form="reset-template" class="link-btn"
                      onclick="return confirm('Сбросить промпт к умолчанию? Текущий текст будет потерян.')">Сбросить к умолчанию</button>
            </div>
          </form>
          <form id="reset-template" method="post" action="/content/prompt/reset">{csrf_field(request)}</form>
        </div>
        </div>

        <div>
        <h2>Формат поста</h2>
        <div class="section-hint">Плюс <code>{{ai}}</code> — ответ модели. HTML-теги Telegram:
          b i u s code pre a blockquote.</div>
        <div class="card">
          <form method="post" action="/content/format">{csrf_field(request)}
            <textarea name="text" rows="{_rows_for(format_text, min_rows=5)}">{_e(format_text)}</textarea>
            <div class="card-actions">
              <button class="primary" type="submit">Сохранить формат</button>
              <button type="submit" form="reset-format" class="link-btn"
                      onclick="return confirm('Сбросить формат к умолчанию? Текущий текст будет потерян.')">Сбросить к умолчанию</button>
            </div>
          </form>
          <form id="reset-format" method="post" action="/content/format/reset">{csrf_field(request)}</form>
        </div>
        </div>
        </div>
        """
        return web.Response(text=_layout("Контент", body, flash, flash_kind, active="/content", wide=True), content_type="text/html")

    async def content_prompt_post(request: web.Request) -> web.Response:
        text = str(request["form"].get("text", "")).strip()
        if "{summary}" not in text and "{title}" not in text:
            return await content_get(request, "В промпте нет ни {title}, ни {summary} — не сохранено.", "err",
                                     template_draft=text)
        app["st"].set("template", text)
        return await content_get(request, "Промпт сохранён.")

    async def content_prompt_reset(request: web.Request) -> web.Response:
        app["st"].set("template", DEFAULTS["template"])
        return _redirect("/content")

    async def content_format_post(request: web.Request) -> web.Response:
        text = str(request["form"].get("text", "")).strip()
        if "{ai}" not in text:
            return await content_get(request, "Без {ai} в посте не будет текста от модели — не сохранено.", "err",
                                     format_draft=text)
        problem = html_problem(text)
        if problem:
            return await content_get(request, f"Разметка не годится: {problem}", "err", format_draft=text)
        app["st"].set("post_format", text)
        return await content_get(request, "Формат сохранён.")

    async def content_format_reset(request: web.Request) -> web.Response:
        app["st"].set("post_format", DEFAULTS["post_format"])
        return _redirect("/content")

    # --- настройки -------------------------------------------------------
    async def settings_get(request: web.Request, flash: str = "", flash_kind: str = "ok") -> web.Response:
        st: Storage = app["st"]
        pub: Publisher = app["publisher"]

        def field(key: str, label: str, unit: str, hint: str) -> str:
            return (f'<div class="field"><label>{_e(label)} <span class="unit">({_e(unit)})</span></label>'
                    f'<input type="text" name="{_e(key)}" value="{_e(st.get(key))}">'
                    f'<div class="field-hint">{_e(hint)}</div></div>')

        groups_html = "".join(
            f'<div class="settings-group"><h3>{_e(group)}</h3><div class="field-row">'
            + "".join(field(k, label, unit, hint) for k, label, unit, hint in fields) + "</div></div>"
            for group, fields in GENERAL_GROUPS
        )
        toggles_html = "".join(
            f'<label class="toggle-row">'
            f'<input type="checkbox" name="{k}" value="1" {"checked" if st.get(k)=="1" else ""}>'
            f'<span><span class="toggle-title">{_e(title)}</span><br>'
            f'<span class="muted">{_e(hint)}</span></span></label>'
            for k, (title, hint) in TOGGLE_LABELS.items()
        )

        current_mode = "claude" if pub.claude_mode else "gemini" if pub.gemini_mode else "normal"

        def key_note(ok: bool, env_name: str) -> str:
            return "" if ok else f'<span style="color:var(--red)">❌ нет {env_name} в .env</span>'

        def combine(*parts: str) -> str:
            return " · ".join(p for p in parts if p)

        def ai_option(value: str, radio_id: str, title_html: str, sub_html: str) -> str:
            checked = "checked" if current_mode == value else ""
            sub_block = f'<div class="muted">{sub_html}</div>' if sub_html else ""
            return (f'<div class="ai-option"><input type="radio" name="mode" value="{value}" id="{radio_id}" {checked}>'
                    f'<label for="{radio_id}" class="ai-option-label"><div class="ai-option-title">{title_html}</div>'
                    f'{sub_block}</label></div>')

        normal_sub = combine(key_note(bool(pub.llm.api_key), "LLM_API_KEY"))
        claude_sub = combine(key_note(bool(pub.claude and pub.claude.api_key), "CLAUDE_API_KEY"))
        gemini_sub = combine(key_note(bool(pub.gemini and pub.gemini.api_key), "GEMINI_API_KEY"))

        ai_options_html = (
            ai_option("normal", "ai-mode-normal",
                     f'Обычная модель <span class="mono">{_e(pub.llm.model)}</span>', normal_sub)
            + ai_option("claude", "ai-mode-claude",
                       f'Claude <span class="mono">{_e(pub.claude.model if pub.claude else "—")}</span> '
                       f'<span class="pill warn">платно</span>',
                       claude_sub)
            + ai_option("gemini", "ai-mode-gemini",
                       f'Gemini <span class="mono">{_e(pub.gemini.model if pub.gemini else "—")}</span> '
                       f'<span class="pill on">бесплатно</span>',
                       gemini_sub)
        )

        debug_state = "включена" if pub.debug else "выключена"

        body = f"""
        <div class="settings-columns">
        <div>
        <h2>Канал</h2>
        <div class="card">
          <form method="post" action="/settings/channel">{csrf_field(request)}
            <div class="row" style="align-items:flex-end;">
              <div style="flex:1;"><label>@канал или числовой id</label>
                <input type="text" name="channel" value="{_e(pub.channel)}" placeholder="@my_news_channel"></div>
            </div>
            <div class="card-actions"><button class="primary" type="submit">Сохранить</button></div>
          </form>
        </div>
        </div>

        <div>
        <h2>Обработка новостей</h2>
        <div class="section-hint">Активен только один вариант — выбор переключает сразу.</div>
        <div class="card">
          <form method="post" action="/settings/ai">{csrf_field(request)}
            {ai_options_html}
            <div class="card-actions"><button class="primary" type="submit">Сохранить</button></div>
          </form>
        </div>
        </div>
        </div>

        <h2>Параметры публикации</h2>
        <div class="card">
          <form method="post" action="/settings/general">{csrf_field(request)}
            <div class="field-groups">{groups_html}</div>
            <h3 style="margin-top:18px;">Поведение</h3>
            {toggles_html}
            <div class="card-actions"><button class="primary" type="submit">Сохранить</button></div>
          </form>
        </div>

        <div class="settings-columns">
        <div>
        <h2>Отладка</h2>
        <div class="card">
          <div class="line">Сейчас: <span class="pill {'on' if pub.debug else 'neutral'}">{debug_state}</span></div>
          <p class="muted">Посты уходят в личку админам вместо канала, автоцикл в отладке молчит.</p>
          <form method="post" action="/settings/debug">{csrf_field(request)}
            <div class="card-actions">
              <button class="{'' if pub.debug else 'primary'}" type="submit">
                {'Выключить' if pub.debug else 'Включить отладку'}</button>
            </div>
          </form>
        </div>
        </div>

        <div>
        <h2>VK</h2>
        <div class="card">
          <div class="line">Сейчас: <span class="pill {'on' if pub.vk_on else 'neutral'}">
            {'включено, сообщество ' + _e(pub.vk_group) if pub.vk_on else 'выключено'}</span>
            {'· ключ не задан (VK_TOKEN в .env)' if not (pub.vk and pub.vk.token) else ''}</div>
          <form method="post" action="/settings/vk">{csrf_field(request)}
            <div class="row" style="align-items:flex-end;">
              <div style="flex:1;"><label>id сообщества (числовой)</label>
                <input type="text" name="vk_group_id" value="{_e(st.get('vk_group_id'))}" placeholder="123456789"></div>
            </div>
            <input type="hidden" name="action" value="{'off' if pub.vk_on else 'on'}">
            <div class="card-actions">
              <button class="{'' if pub.vk_on else 'primary'}" type="submit">
                {'Выключить' if pub.vk_on else 'Включить'}</button>
            </div>
          </form>
        </div>
        </div>
        </div>
        """
        return web.Response(text=_layout("Настройки", body, flash, flash_kind, active="/settings", wide=True), content_type="text/html")

    async def settings_channel(request: web.Request) -> web.Response:
        target = str(request["form"].get("channel", "")).strip()
        if not target:
            return await settings_get(request, "Укажите канал.", "err")
        try:
            chat = await app["bot"].get_chat(target)
        except Exception as exc:
            return await settings_get(request, f"Не вижу такой чат: {exc}. Бот должен быть админом канала.", "err")
        app["st"].set("channel_id", str(chat.id))
        app["publisher"].wake()
        return await settings_get(request, f"Публикую в «{chat.title or chat.id}».")

    async def settings_general(request: web.Request) -> web.Response:
        st: Storage = app["st"]
        form = request["form"]
        for k in SETTINGS_EDITABLE:
            if k not in form:
                continue
            v = str(form.get(k, "")).strip()
            if k == "alert_thresholds":
                parts = [p for p in v.replace(" ", "").split(",") if p]
                if not parts or not all(p.isdigit() and 1 <= int(p) <= 100 for p in parts):
                    return await settings_get(request, f"{k}: пороги — числа 1-100 через запятую.", "err")
                v = ",".join(str(int(p)) for p in sorted({int(p) for p in parts}))
            elif k == "max_images":
                if not v.isdigit() or not (1 <= int(v) <= 10):
                    return await settings_get(request, "Картинок в альбом — число от 1 до 10.", "err")
            elif not v.isdigit():
                return await settings_get(request, f"{k} должно быть числом.", "err")
            st.set(k, v)
        for k in SETTINGS_TOGGLES:
            st.set(k, "1" if form.get(k) == "1" else "0")
        return await settings_get(request, "Настройки сохранены.")

    async def settings_debug(request: web.Request) -> web.Response:
        st: Storage = app["st"]
        st.set("debug", "0" if st.get("debug") == "1" else "1")
        return _redirect("/settings")

    async def settings_vk(request: web.Request) -> web.Response:
        st: Storage = app["st"]
        form = request["form"]
        group = str(form.get("vk_group_id", "")).strip().lstrip("-")
        if group:
            if not group.isdigit():
                return await settings_get(request, "id сообщества должен быть числом.", "err")
            st.set("vk_group_id", group)
        st.set("vk_enabled", "1" if form.get("action") == "on" else "0")
        return await settings_get(request, "Сохранено.")

    async def settings_ai(request: web.Request) -> web.Response:
        """Один выбор вместо двух отдельных переключателей Claude/Gemini —
        они и так были взаимоисключающими, отдельные формы только заставляли
        помнить это правило глазами, а не видеть его в самом интерфейсе."""
        st: Storage = app["st"]
        form = request["form"]
        mode = str(form.get("mode", "normal"))
        st.set("claude_mode", "1" if mode == "claude" else "0")
        st.set("gemini_mode", "1" if mode == "gemini" else "0")
        pub: Publisher = app["publisher"]
        if mode == "claude" and not pub.claude_mode:
            return await settings_get(request, "Выбран Claude, но не хватает CLAUDE_API_KEY в .env — переключение не подействует.", "err")
        if mode == "gemini" and not pub.gemini_mode:
            return await settings_get(request, "Выбран Gemini, но не хватает GEMINI_API_KEY в .env — переключение не подействует.", "err")
        return await settings_get(request, "Сохранено.")

    # --- посты -----------------------------------------------------------
    def _kind_label(kind: str) -> str:
        return _e({"text": "текст", "photo": "фото", "album": "альбом"}.get(kind, kind))

    async def posts_get(request: web.Request) -> web.Response:
        st: Storage = app["st"]
        rows = st.posts(30)
        items = "".join(
            f"""<a class="list-item" href="/posts/{r['id']}">
              <div class="list-item-info">
                <div class="list-item-title">
                  <b>#{r['id']}</b> {_e(r['title'][:120])}
                </div>
                <div class="muted">{_kind_label(r['kind'])}{' <span class="pill neutral">ред.</span>' if r['edited_at'] else ''}
                  · {time.strftime('%d.%m %H:%M', time.localtime(r['posted_at']))}</div>
              </div>
              <div class="list-item-chevron">›</div>
            </a>""" for r in rows
        )
        list_html = items if rows else (
            "<div style='padding:28px 16px; text-align:center;'>"
            "<div style='font-size:28px; margin-bottom:8px;'>📮</div>"
            "<div class='muted'>Опубликованных постов пока нет.</div></div>"
        )
        body = f"<h2 class='page-heading'>Последние посты</h2><div class='list'>{list_html}</div>"
        return web.Response(text=_layout("Посты", body, active="/posts"), content_type="text/html")

    async def post_detail(request: web.Request, draft: str | None = None,
                          flash: str = "", flash_kind: str = "ok") -> web.Response:
        st: Storage = app["st"]
        post_id = int(request.match_info["id"])
        row = st.post(post_id)
        if row is None:
            raise web.HTTPNotFound(text="Пост не найден")
        text = draft if draft is not None else row["text"]
        draft_note = ('<p class="muted">⚠️ Это черновик после перегенерации — ещё не сохранён в канале. '
                      'Проверьте и нажмите «Сохранить».</p>' if draft is not None else "")
        edited = (f", отредактирован {time.strftime('%d.%m %H:%M', time.localtime(row['edited_at']))}"
                 if row["edited_at"] else "")
        limit = TG_CAPTION_LIMIT if row["kind"] in ("photo", "album") else TG_LIMIT

        images_card = ""
        if row["kind"] == "album":
            extra = st.post_extra_ids(post_id)
            if extra:
                rows_html = '<div class="list-item"><div class="list-item-info">' \
                    '<div class="list-item-title">Картинка №1</div>' \
                    '<div class="muted">С текстом поста — не удаляется</div></div></div>'
                for i, msg_id in enumerate(extra, start=2):
                    rows_html += f"""<div class="list-item">
                      <div class="list-item-info"><div class="list-item-title">Картинка №{i}</div></div>
                      <div class="list-item-actions">
                        <form class="inline" method="post" action="/posts/{post_id}/image/{msg_id}/delete"
                              onsubmit="return confirm('Удалить картинку №{i} из поста?')">{csrf_field(request)}
                          <button class="icon" type="submit" title="Удалить картинку">✕</button></form>
                      </div>
                    </div>"""
                images_card = f"""
                <div class="card">
                  <div class="muted" style="margin-bottom:8px;">Картинок в альбоме: {len(extra) + 1}</div>
                  <div class="list">{rows_html}</div>
                </div>
                """

        body = f"""
        <div><a href="/posts" class="back-link">‹ Все посты</a></div>
        <h2 class="page-heading after-back">Пост #{row['id']} <span class="muted" style="font-weight:400;">({_kind_label(row['kind'])})</span></h2>
        <div class="card">
          <div class="muted">Опубликован {time.strftime('%d.%m %H:%M', time.localtime(row['posted_at']))}{edited}
            · {_e(row['title'])} · <a href="{_safe_href(row['link'])}" target="_blank" rel="noopener">исходная новость</a></div>
          <hr class="sep">
          {draft_note}
          <form method="post" action="/posts/{row['id']}/save">{csrf_field(request)}
            <textarea name="text" rows="{_rows_for(text, min_rows=8)}" maxlength="{limit}">{_e(text)}</textarea>
            <div class="muted" style="margin-top:4px;">Лимит для этого поста: {limit} символов
              ({'подпись к фото' if row['kind'] in ('photo','album') else 'текстовое сообщение'})</div>
            <div class="card-actions"><button class="primary" type="submit">Сохранить в канал</button></div>
          </form>
        </div>
        {images_card}
        <div class="card">
          <form method="post" action="/posts/{row['id']}/regen">{csrf_field(request)}
            <label>Перегенерировать через ИИ из исходной новости</label>
            <p class="field-hint" style="margin:0 0 8px;">Пожелание необязательно. Не сохраняет сразу — покажет
              черновик выше, сохранить нужно отдельно.</p>
            <div class="row">
              <input type="text" name="extra" placeholder="например: короче и без хештегов" style="flex:1;">
              <button type="submit">🤖 Перегенерировать</button>
            </div>
          </form>
        </div>
        """
        return web.Response(text=_layout(f"Пост #{row['id']}", body, flash, flash_kind, active="/posts"), content_type="text/html")

    async def post_save(request: web.Request) -> web.Response:
        st: Storage = app["st"]
        post_id = int(request.match_info["id"])
        row = st.post(post_id)
        if row is None:
            raise web.HTTPNotFound(text="Пост не найден")
        text = str(request["form"].get("text", "")).strip()
        if not text:
            return await post_detail(request, flash="Пустой текст не сохранён.", flash_kind="err")
        limit = TG_CAPTION_LIMIT if row["kind"] in ("photo", "album") else TG_LIMIT
        if tg_len(text) > limit:
            return await post_detail(request, draft=text,
                                     flash=f"Текст длиннее лимита ({tg_len(text)} из {limit}) — не сохранено.",
                                     flash_kind="err")
        problem = html_problem(text)
        if problem:
            return await post_detail(request, draft=text, flash=f"Разметка не годится: {problem}", flash_kind="err")

        err = await _apply_edit(app["bot"], row, text)
        if err:
            return await post_detail(request, draft=text, flash=err, flash_kind="err")
        st.update_post_text(post_id, text)
        return await post_detail(request, flash="Сохранено.")

    async def post_regen(request: web.Request) -> web.Response:
        st: Storage = app["st"]
        pub: Publisher = app["publisher"]
        post_id = int(request.match_info["id"])
        row = st.post(post_id)
        if row is None:
            raise web.HTTPNotFound(text="Пост не найден")
        extra = str(request["form"].get("extra", "")).strip()
        try:
            text = await pub.rebuild_post_text(row, extra)
        except LLMError as exc:
            return await post_detail(request, flash=f"Модель вернула ошибку: {exc}", flash_kind="err")
        return await post_detail(request, draft=text, flash="Черновик готов — не забудьте сохранить.")

    async def post_delete_image(request: web.Request) -> web.Response:
        pub: Publisher = app["publisher"]
        post_id = int(request.match_info["id"])
        message_id = int(request.match_info["msg_id"])
        error = await pub.delete_post_image(post_id, message_id)
        if error:
            return await post_detail(request, flash=error, flash_kind="err")
        return await post_detail(request, flash="Картинка удалена.")

    async def _apply_edit(bot_: Bot, row, text: str) -> str | None:
        try:
            if row["kind"] in ("photo", "album"):
                await bot_.edit_message_caption(chat_id=row["chat_id"], message_id=row["message_id"],
                                               caption=text, parse_mode="HTML")
            else:
                await bot_.edit_message_text(chat_id=row["chat_id"], message_id=row["message_id"],
                                            text=text, parse_mode="HTML",
                                            link_preview_options=LinkPreviewOptions(is_disabled=True))
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return "Текст не изменился."
            return (f"Telegram отказал в правке: {exc}. Частые причины: пост старше ~48 часов, "
                    f"бот больше не админ канала, сообщение удалено вручную.")
        except TelegramAPIError as exc:
            return f"Ошибка Telegram: {exc}"
        return None

    # --- дубли между лентами ----------------------------------------------
    def _dedup_entry(row) -> Entry:
        """Восстанавливает Entry из строки dedup_candidates — для повторного
        прогона через build_post при ручной публикации. key_parts тут ни на
        что не влияет: запись уже отмечена прочитанной в момент обнаружения,
        второй раз mark_seen для неё не понадобится."""
        return Entry(key_parts=(f"dedup:{row['id']}",), title=row["title"], link=row["link"],
                    summary=row["summary"], published=row["published"], published_ts=0,
                    image=row["image"])

    async def duplicate_detail(request: web.Request) -> web.Response:
        st: Storage = app["st"]
        cid = int(request.match_info["id"])
        row = st.dedup_candidate(cid)
        if row is None:
            raise web.HTTPNotFound(text="Запись не найдена — возможно, уже разобрана")
        matched = st.post(row["matched_post_id"]) if row["matched_post_id"] else None
        matched_html = (f'<a href="/posts/{row["matched_post_id"]}">пост #{row["matched_post_id"]}</a>'
                        f' — «{_e(matched["title"][:100])}»' if matched
                        else f'пост #{row["matched_post_id"]} (уже удалён)')
        image_html = (f'<img src="{_safe_href(row["image"])}" alt="" '
                      f'style="max-width:100%; border-radius:10px; margin-top:10px;">'
                      if row["image"] else "")
        body = f"""
        <div><a href="/feeds#duplicates" class="back-link">‹ Ленты</a></div>
        <h2 class="page-heading after-back">Дубль #{row['id']}</h2>
        <div class="card">
          <div class="line"><b>{_e(row['title'])}</b></div>
          <div class="muted">{_e(row['source'] or 'без ленты')} · {_e(row['published'] or '—')} ·
            <a href="{_safe_href(row['link'])}" target="_blank" rel="noopener">исходная новость</a></div>
          <div class="muted" style="margin-top:6px;">Похоже на {matched_html} — схожесть {row['score']:.0%}</div>
          {image_html}
          <hr class="sep">
          <div class="field-hint" style="margin:0 0 6px;">Как есть в ленте, без обработки ИИ:</div>
          <pre class="post">{_e(row['summary'] or '(пусто)')}</pre>
          <div class="card-actions">
            <form method="post" action="/duplicates/{row['id']}/publish">{csrf_field(request)}
              <button class="primary" type="submit">✅ Опубликовать всё же</button></form>
            <form method="post" action="/duplicates/{row['id']}/delete"
                  onsubmit="return confirm('Удалить из очереди? Новость останется неопубликованной.')">{csrf_field(request)}
              <button class="link-btn" type="submit">Удалить, это правда дубль</button></form>
          </div>
        </div>
        """
        return web.Response(text=_layout(f"Дубль #{row['id']}", body, active="/feeds"), content_type="text/html")

    async def duplicate_publish(request: web.Request) -> web.Response:
        st: Storage = app["st"]
        pub: Publisher = app["publisher"]
        cid = int(request.match_info["id"])
        row = st.dedup_candidate(cid)
        if row is None:
            raise web.HTTPNotFound(text="Запись не найдена — возможно, уже разобрана")
        feed = st.feed(row["feed_id"]) if row["feed_id"] else None
        error = await pub.publish_now(_dedup_entry(row), feed)
        if error:
            return await feeds_get(request, flash=error, flash_kind="err")
        st.delete_dedup_candidate(cid)
        return await feeds_get(request, flash="Опубликовано.")

    async def duplicate_delete(request: web.Request) -> web.Response:
        st: Storage = app["st"]
        cid = int(request.match_info["id"])
        st.delete_dedup_candidate(cid)
        return await feeds_get(request, flash="Убрано из очереди.")

    app.router.add_get("/login", login_get)
    app.router.add_post("/login", login_post)
    app.router.add_post("/tg-login", tg_login_post)
    app.router.add_post("/logout", logout_post)
    app.router.add_get("/", dashboard)
    app.router.add_post("/pause", pause_post)
    app.router.add_post("/checknow", checknow_post)
    app.router.add_get("/feeds", feeds_get)
    app.router.add_post("/feeds/add", feeds_add)
    app.router.add_post("/feeds/add-sitemap", feeds_add_sitemap)
    app.router.add_post("/feeds/{id}/delete", feeds_delete)
    app.router.add_post("/feeds/{id}/toggle", feeds_toggle)
    app.router.add_post("/feeds/{id}/multiimages", feeds_toggle_multi)
    app.router.add_get("/feeds/{id}/template", feed_template_get)
    app.router.add_post("/feeds/{id}/template", feed_template_post)
    app.router.add_post("/feeds/{id}/template/reset", feed_template_reset)
    app.router.add_get("/content", content_get)
    app.router.add_post("/content/prompt", content_prompt_post)
    app.router.add_post("/content/prompt/reset", content_prompt_reset)
    app.router.add_post("/content/format", content_format_post)
    app.router.add_post("/content/format/reset", content_format_reset)
    app.router.add_get("/settings", settings_get)
    app.router.add_post("/settings/channel", settings_channel)
    app.router.add_post("/settings/general", settings_general)
    app.router.add_post("/settings/debug", settings_debug)
    app.router.add_post("/settings/vk", settings_vk)
    app.router.add_post("/settings/ai", settings_ai)
    app.router.add_get("/posts", posts_get)
    app.router.add_get("/posts/{id}", post_detail)
    app.router.add_post("/posts/{id}/save", post_save)
    app.router.add_post("/posts/{id}/regen", post_regen)
    app.router.add_post("/posts/{id}/image/{msg_id}/delete", post_delete_image)
    app.router.add_get("/duplicates/{id}", duplicate_detail)
    app.router.add_post("/duplicates/{id}/publish", duplicate_publish)
    app.router.add_post("/duplicates/{id}/delete", duplicate_delete)

    return app


async def run_web_panel(storage: Storage, publisher: Publisher, bot: Bot,
                        password: str, port: int, host: str = "0.0.0.0",
                        admin_ids: set[int] | None = None
                        ) -> tuple[web.AppRunner, web.TCPSite]:
    app = create_app(storage, publisher, bot, password, admin_ids=admin_ids)
    runner = web.AppRunner(app, access_log=log)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    return runner, site
