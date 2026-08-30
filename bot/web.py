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
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Awaitable, Callable
from urllib.parse import parse_qsl, quote, urlsplit

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import LinkPreviewOptions
from aiohttp import web

from .db import DEFAULTS, Storage
from .llm import LLMError
from .publisher import (TG_CAPTION_LIMIT, TG_LIMIT, Publisher, html_problem,
                        tg_len)
from .quota import until_reset
from .rss import Entry, fetch, strip_html
from .search import site_query

log = logging.getLogger(__name__)

SESSION_COOKIE = "bot_session"
SESSION_TTL = 7 * 24 * 3600      # неделя — снова логиниться каждый день утомительно
LOGIN_MAX_FAILS = 5              # неудачных попыток с одного адреса
LOGIN_LOCKOUT = 15 * 60          # прежде чем снова можно пробовать
REGEN_DRAFT_TTL = 24 * 3600      # черновик, который так и не сохранили — не копить в памяти вечно
UNDO_TTL = 20                    # окно «Отменить» после отклонения карточки очереди согласования

SETTINGS_EDITABLE = (
    "interval", "max_per_cycle", "post_delay", "backfill",
    "max_age_days", "flood_guard", "keep_seen",
    "alert_thresholds", "free_daily_limit", "max_images",
    "moderation_max_queue", "moderation_remind_hours", "moderation_keep_days",
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
    ("Ручное согласование", [
        ("moderation_max_queue", "Потолок очереди", "карточек",
         "Больше — новые новости не обрабатываются, пока не разберёте текущие. 0 — без ограничения"),
        ("moderation_remind_hours", "Напомнить через", "ч",
         "Если самая старая карточка ждёт дольше — пришлём напоминание. 0 — не напоминать"),
        ("moderation_keep_days", "Автоотклонение через", "дней",
         "Карточки, которые никто не разобрал столько дней, отклоняются сами"),
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


def _is_http_url(url: str) -> bool:
    return url.strip().lower().startswith(("http://", "https://"))


def _safe_href(url: str) -> str:
    """Ссылки в постах приходят из RSS/Atom лент — это контент сайта-источника,
    не то, что ввёл сам админ. javascript:-урлы там маловероятны, но раз мы всё
    равно рендерим их кликабельными в браузере — лучше не давать возможности.

    Для <a href> "#" — безопасный no-op. Для <img src> он вредный: браузер
    тут же повторно запросит текущую страницу как картинку и покажет
    «битую» иконку — там сначала проверяйте _is_http_url() и рисуйте плейсхолдер."""
    if _is_http_url(url):
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
  color-scheme: dark; --tg-top: 0px; --tg-bottom: 0px; --nav-h: 60px; --side-w: 204px;
  /* Палитра — тёмный терминал: почти-чёрный с еле заметным зелёным подтоном,
     фосфорно-зелёный (--accent/--green) под «включено»/бренд, терминальный
     циан (--blue, имя сохранено — на нём завязана логика акцентных кнопок/
     фокуса/ссылок) под интерактив, янтарь/красный — предупреждение/ошибка. */
  --bg: #070a08; --bg-alt: #0c1210; --card: #101815; --card-hover: #16201b;
  --border: #213228; --border-soft: #19241d;
  --text: #dcefe0; --text-dim: #8fac97; --text-faint: #69836f;
  --accent: #4ade80; --accent-dim: #132818;
  --blue: #42d4e0; --blue-dim: #0e262a; --blue-hover: #6fe0ea;
  --green: #4ade80; --green-dim: #132818; --green-text: #a3f5c0;
  --amber: #e8b13a; --amber-dim: #2f2410;
  --purple: #c792ea; --purple-dim: #241f30;
  --red: #ff6b6b; --red-dim: #301316; --red-border: #4a2226; --red-hover: #3d181b; --red-text: #ffb4b4;
  --gray-dim: #182019; --gray: #84998b;
  --btn-hover: #182219; --btn-border-hover: #2c4033;
  --field-bg: #040605; --on-blue: #03211f;
  --radius: 8px; --radius-sm: 5px;
  --font: ui-monospace, "SF Mono", "Cascadia Code", "JetBrains Mono", Consolas, monospace;
}
* { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
body {
  font-family: var(--font);
  background-color: var(--bg);
  /* Скан-линии — статичный узор, не анимация: печатается один раз в фон,
     ничего не двигается и не мигает, так что рендеру он ничего не стоит.
     Видно его только там, где не легли непрозрачные карточки/шапка/меню —
     то есть в зазорах между ними, лёгким намёком на ЭЛТ-текстуру. */
  background-image: repeating-linear-gradient(to bottom,
    rgba(74, 222, 128, .035) 0px, rgba(74, 222, 128, .035) 1px,
    transparent 1px, transparent 3px);
  color: var(--text); margin: 0; font-size: 14.5px;
  line-height: 1.5;
  /* место под фиксированное нижнее меню на телефоне, см. .bottom-nav */
  padding-bottom: calc(var(--nav-h) + 14px + var(--tg-bottom));
}
:focus-visible { outline: 2px solid var(--blue); outline-offset: 2px; }
.shell { display: flex; min-height: 100vh; }
/* overflow-x:hidden — сознательно тут, а не на html/body: если задан только
   overflow-x, браузер обязан «повысить» overflow-y до auto (правило
   вычисления overflow CSS-спеки), а это делает элемент новым скролл-
   контейнером — position:sticky у .side-nav (соседний элемент, не потомок
   .main-col) внутри такого контейнера перестаёт цепляться за реальный
   вьюпорт и просто едет вместе со страницей. На html/body тот же
   overflow-x:hidden ломал боковое меню именно поэтому. */
.main-col { flex: 1 1 auto; min-width: 0; overflow-x: hidden; }
/* Верхняя шапка — на телефоне это единственная навигационная точка (бренд +
   выход), на широком экране бренд уже есть в боковом меню и там же выход,
   поэтому шапка превращается в узкую строку с названием текущей страницы. */
header {
  background: var(--bg-alt);
  padding: calc(12px + var(--tg-top)) 16px 12px; border-bottom: 1px solid var(--border-soft);
  position: sticky; top: 0; z-index: 10;
  display: flex; align-items: center; justify-content: space-between; gap: 10px;
}
header h1 { font-size: 15.5px; margin: 0; font-weight: 600; letter-spacing: .2px; }
header .page-title { display: none; }
header .page-title::before { content: "$ "; color: var(--accent); }
header .logout button { padding: 7px 13px; font-size: 12.5px; }
/* Боковое меню — только на широком экране, см. .side-nav display в media-запросе. */
.side-nav {
  display: none; flex-direction: column; width: var(--side-w); flex-shrink: 0;
  background: var(--bg-alt); border-right: 1px solid var(--border-soft);
  padding: 16px 10px; position: sticky; top: 0; height: 100vh; overflow-y: auto;
}
.side-brand { display: flex; align-items: center; gap: 9px; font-weight: 600; font-size: 14.5px;
              padding: 4px 8px 18px; letter-spacing: .2px; }
.side-links { display: flex; flex-direction: column; gap: 2px; flex: 1; }
.side-nav .nav-link {
  display: flex; align-items: center; gap: 10px; padding: 9px 10px; border-radius: var(--radius-sm);
  color: var(--text-dim); text-decoration: none; font-size: 13px; transition: background .12s, color .12s;
}
.side-nav .nav-link .ic { font-size: 15px; line-height: 1; }
.side-nav .nav-link:hover { background: var(--card-hover); color: var(--text); }
.side-nav .nav-link.active { background: var(--accent-dim); color: var(--accent); font-weight: 600; }
/* Мигающий курсор у активного пункта меню — единственная непрерывная
   анимация в теме, и та только здесь: один маленький блок, период почти
   1.5с, только на широком экране (на телефоне бокового меню нет вовсе). */
.side-nav .nav-link.active::after {
  content: ""; width: 6px; height: 12px; margin-left: auto; flex-shrink: 0;
  background: var(--accent); animation: cursor-blink 1.4s steps(1, end) infinite;
}
@keyframes cursor-blink { 0%, 49% { opacity: 1; } 50%, 100% { opacity: 0; } }
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
/* Активная вкладка не должна опираться только на цвет (WCAG 1.4.1) —
   на телефоне это единственный ориентир «где я», второй признак — точка. */
.bottom-nav .nav-link.active { color: var(--accent); font-weight: 600; }
.side-nav .nav-dot { display: none; }
.bottom-nav .nav-dot {
  width: 4px; height: 4px; border-radius: 50%; margin-top: 2px; visibility: hidden;
}
.bottom-nav .nav-link.active .nav-dot { visibility: visible; background: var(--accent); }
main { max-width: 1000px; margin: 16px auto; padding: 0 14px; }
main.wide { max-width: 1180px; }
/* h2 — настоящий заголовок раздела (не декоративная ярлычная строка):
   крупнее и контрастнее обычного текста, без акцентного цвета для самого
   текста — фосфорный зелёный зарезервирован под бренд/активную вкладку,
   иначе взгляд цепляется за заголовки, а не за сами элементы управления.
   Префикс «$ » — единственный явный привет терминалу в типографике:
   статичный псевдоэлемент, ничего не стоит на рендере. */
h2 { font-size: 18.5px; color: var(--text); font-weight: 700; margin: 24px 0 10px; letter-spacing: -.1px; }
h2::before { content: "$ "; color: var(--accent); }
/* Якорные переходы (например дашборд → #duplicates) иначе утыкаются
   заголовком прямо под залипающую шапку — застревает, накрытый ей. */
h2[id] { scroll-margin-top: calc(76px + var(--tg-top)); }
h2:first-child { margin-top: 4px; }
/* h3 — подзаголовок группы полей внутри карточки, нарочно тише и мельче h2,
   чтобы иерархия читалась с одного взгляда. */
h3 { font-size: 10.5px; color: var(--text-faint); margin: 0 0 8px; font-weight: 600;
     text-transform: uppercase; letter-spacing: .5px; padding-bottom: 5px; border-bottom: 1px solid var(--border-soft); }
.section-hint { color: var(--text-faint); font-size: 12.5px; margin: -5px 0 10px; }
.card {
  background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 13px 14px; margin-bottom: 10px;
}
.card.scroll { overflow-x: auto; }
.card + h2 { margin-top: 24px; }
.row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-bottom: 8px; }
/* Отдельный класс, не .row — на узком экране .row > * растягивается на всю
   ширину (!important, см. media-запрос ниже), и «‹ Назад / Стр. N / Вперёд ›»
   встали бы тремя полными строками вместо одной компактной. */
.pager { display: flex; justify-content: space-between; align-items: center; margin-top: 12px; gap: 8px; }
.row > * { flex: 1 1 220px; }
.row > button, .row > .btn { flex: 0 1 auto; }
.row:last-child { margin-bottom: 0; }
/* Строки полей формы (число+подпись+подсказка) — низ полей на одной линии,
   даже если у соседних подписи разной длины и переносятся по-разному. */
.field-row { display: flex; gap: 8px; flex-wrap: wrap; align-items: flex-end; margin-bottom: 12px; }
.field-row:last-child { margin-bottom: 0; }
.field-row > * { flex: 1 1 220px; }
.field { min-width: 0; }
/* Простая строка «подпись: значение» — не форма, без flex-разъезда полей */
.line { margin-bottom: 6px; line-height: 1.6; }
.line:last-child { margin-bottom: 0; }
label { display: block; font-size: 11.5px; color: var(--text-dim); font-weight: 500;
        letter-spacing: .2px; margin-bottom: 3px; min-height: 14px; }
label .unit { font-weight: 400; color: var(--text-faint); }
.field-hint { color: var(--text-faint); font-size: 11px; margin: 3px 0 0; }
input[type=text], input[type=password], input[type=number], textarea, select {
  background: var(--field-bg); color: var(--text); border: 1px solid var(--border); border-radius: 6px;
  padding: 9px 10px; font-size: 16px; width: 100%; font-family: inherit; transition: border-color .12s, box-shadow .12s;
}
input:focus, textarea:focus, select:focus { outline: none; border-color: var(--blue); box-shadow: 0 0 0 1px var(--blue); }
code {
  background: var(--field-bg); border: 1px solid var(--border-soft); border-radius: 4px;
  padding: 1px 6px; font-family: var(--font); font-size: .9em; color: var(--blue-hover);
}
pre code { background: none; border: none; padding: 0; color: inherit; }
/* font-size меньше 16px в полях ввода — Safari на iOS зумит страницу при фокусе */
/* На телефоне высоту растягивает скрипт (autosizeTextareas в TG_INIT_SCRIPT)
   под конкретную страницу — здесь только запасной размер на случай, если он
   не отработал. На широком экране скрипт этого не делает (см. скрипт) —
   там высотой управляет только rows= в разметке плюс ручной resize. */
textarea { min-height: 45vh; resize: vertical; font-family: var(--font); font-size: 13.5px; }
button, .btn {
  background: var(--card-hover); color: var(--text); border: 1px solid var(--border);
  border-radius: var(--radius-sm); padding: 11px 16px; font-size: 13.5px; cursor: pointer;
  min-height: 42px; display: inline-flex; align-items: center; justify-content: center;
  gap: 6px; text-decoration: none; transition: background .12s, border-color .12s, transform .06s;
  font-family: inherit;
}
button:hover, .btn:hover { background: var(--btn-hover); border-color: var(--btn-border-hover); }
button:active, .btn:active { transform: scale(.98); }
button.primary { background: var(--blue); border-color: var(--blue); color: var(--on-blue); font-weight: 700; }
button.primary:hover { background: var(--blue-hover); }
button.danger { background: var(--red-dim); border-color: var(--red-border); color: var(--red); }
button.danger:hover { background: var(--red-hover); }
button.icon, .btn.icon { min-height: 38px; min-width: 38px; padding: 6px; font-size: 15px; flex: 0 0 auto; }
.card-actions { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin-top: 10px; }
.link-btn {
  background: none; border: none; color: var(--text-faint); font-size: 12px; padding: 6px 2px;
  min-height: auto; text-decoration: underline; text-underline-offset: 2px; font-family: inherit;
}
.link-btn:hover { background: none; color: var(--text-dim); }
/* Ссылка «назад» — не второстепенное действие вроде сброса поля, а обычный
   переход; приглушённый вечно-подчёркнутый вид .link-btn читался бы как
   отключённая/посещённая ссылка. */
.back-link {
  display: inline-flex; align-items: center; gap: 4px; color: var(--text-dim);
  text-decoration: none; font-size: 12.5px; margin-bottom: 4px;
}
.back-link:hover { color: var(--text); text-decoration: underline; }
h2.page-heading.after-back { margin-top: 6px; }
/* Прямоугольные бейджи-теги вместо скруглённых «пилюль» — читаются как
   лог-метки уровня терминала ([OK]/[WARN]), не требуя лишних символов
   в самом тексте, который приходит из Python (менять его не нужно). */
.pill { display: inline-block; padding: 2px 7px; border-radius: 4px; font-size: 10.5px; font-weight: 700;
        text-transform: uppercase; letter-spacing: .3px; }
.pill.on { background: var(--green-dim); color: var(--green); }
.pill.off { background: var(--red-dim); color: var(--red); }
.pill.warn { background: var(--amber-dim); color: var(--amber); }
.pill.neutral { background: var(--gray-dim); color: var(--gray); }
.flash { padding: 10px 13px; border-radius: var(--radius-sm); margin-bottom: 14px; font-size: 13.5px;
         border-left: 3px solid transparent; }
.flash.ok { background: var(--green-dim); color: var(--green-text); border-left-color: var(--green); }
.flash.err { background: var(--red-dim); color: var(--red-text); border-left-color: var(--red); }
.flash form { display: inline; margin-left: 8px; }
.flash .undo-btn { background: none; border: none; padding: 0; font: inherit; color: inherit;
                   text-decoration: underline; cursor: pointer; }
.muted { color: var(--text-dim); font-size: 12.5px; }
.mono { font-family: var(--font); font-size: 12.5px; overflow-wrap: anywhere; }
.mono.ellipsis { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; display: block; }
pre.post { white-space: pre-wrap; word-break: break-word; background: var(--field-bg); border: 1px solid var(--border);
           border-radius: var(--radius-sm); padding: 9px 11px; font-size: 12.5px; }
form.inline { display: inline; }
hr.sep { border: none; border-top: 1px solid var(--border-soft); margin: 14px 0; }
/* <details>/<summary> без родного треугольника браузера — свой шеврон,
   который разворачивается при открытии. */
summary.disclosure {
  cursor: pointer; color: var(--blue-hover); font-weight: 600; list-style: none;
  display: flex; align-items: center; gap: 6px;
}
summary.disclosure::-webkit-details-marker { display: none; }
summary.disclosure::before { content: "›"; display: inline-block; transition: transform .12s; font-weight: 700; }
details[open] > summary.disclosure::before { transform: rotate(90deg); }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
td, th { text-align: left; padding: 6px; border-bottom: 1px solid var(--border-soft); vertical-align: top; }
table.kv td:first-child { color: var(--text-dim); white-space: nowrap; padding-right: 20px; width: 1%; }
/* Списки (ленты, посты) — карточка-контейнер один раз снаружи, строки внутри
   разделены волосяными линиями, а не вложенными собственными рамками —
   иначе получаются рамка в рамке и повторный фон-«приподнятие». */
.list { background: var(--card); border: 1px solid var(--border); border-radius: var(--radius); overflow: hidden; }
.list-item {
  display: flex; align-items: center; gap: 10px; padding: 11px 14px;
  border-bottom: 1px solid var(--border-soft); transition: background .12s;
  color: inherit; text-decoration: none;
}
a.list-item:hover { background: var(--card-hover); }
.list-item:last-child { border-bottom: none; }
.list-item-info { flex: 1 1 auto; min-width: 0; }
.list-item-title { color: var(--text); font-size: 13.5px; overflow-wrap: break-word; }
.list-item-actions { display: flex; gap: 6px; flex-shrink: 0; align-items: center; }
.list-item-chevron { color: var(--text-faint); font-size: 18px; flex-shrink: 0; }
/* Строка с быстрыми действиями (очередь согласования): открыть карточку —
   тап по всей строке (.list-item-cover накрывает её целиком), а кнопки
   быстрых действий лежат выше по стеку и получают клик первыми. */
.list-item.actionable { position: relative; }
.list-item-cover { position: absolute; inset: 0; z-index: 0; }
.list-item.actionable .list-item-actions { position: relative; z-index: 1; }
.dupe-thumb { width: 64px; height: 64px; border-radius: var(--radius-sm); flex-shrink: 0; }
/* Дашборд: сетка карточек-метрик вместо списка строк «подпись: значение» —
   легче окинуть взглядом состояние бота целиком. */
.stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(170px, 100%), 1fr)); gap: 8px;
             margin-bottom: 10px; }
.stat-card { background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
             padding: 11px 13px; display: flex; flex-direction: column; justify-content: space-between; min-height: 64px; }
.stat-card .stat-label { font-size: 10.5px; text-transform: uppercase; letter-spacing: .3px;
                          color: var(--text-faint); margin-bottom: 6px; }
.stat-card .stat-value { font-size: 14px; color: var(--text); overflow-wrap: anywhere; }
.hero-card {
  background: var(--card); border: 1px solid var(--border); border-left: 3px solid var(--green);
  border-radius: var(--radius); padding: 15px 16px; margin-bottom: 12px;
  display: flex; align-items: center; justify-content: space-between; gap: 14px; flex-wrap: wrap;
}
.hero-card.paused { border-left-color: var(--amber); }
.hero-card.debug { border-left-color: var(--blue); }
.hero-card.moderation { border-left-color: var(--purple); }
.hero-card .hero-state { font-size: 18px; font-weight: 700; display: flex; align-items: center; gap: 9px; }
.hero-card .hero-dot { width: 9px; height: 9px; border-radius: 50%; background: var(--green); flex-shrink: 0; }
.hero-card.paused .hero-dot { background: var(--amber); }
.hero-card.debug .hero-dot { background: var(--blue); }
.hero-card.moderation .hero-dot { background: var(--purple); }
.hero-card .hero-sub { color: var(--text-dim); font-size: 12px; margin-top: 3px; }
/* Выбор ИИ-бэкенда в настройках — три варианта в виде селектируемых строк
   вместо трёх отдельных карточек-дублей с одинаковой формой включения.
   .ai-options — общий контейнер (см. settings_get): на узком экране просто
   оборачивает список, на широком превращает три строки в три колонки. */
.ai-options { display: block; }
.ai-option {
  display: grid; grid-template-columns: 16px 1fr; gap: 4px 10px; align-items: start;
  padding: 10px 11px; border-radius: var(--radius-sm); position: relative; cursor: pointer;
  border: 1px solid var(--border); margin-bottom: 7px; transition: border-color .12s, background .12s;
  min-height: 50px;
}
.ai-option:hover { border-color: var(--btn-border-hover); background: var(--card-hover); }
.ai-option input[type=radio] { accent-color: var(--blue); width: 16px; height: 16px; margin-top: 2px; cursor: pointer; }
/* Кликабельна вся строка, не только текст подписи — ::after растягивает
   область клика на всю карточку поверх остального содержимого. */
.ai-option-label { min-width: 0; cursor: pointer; align-self: center; }
.ai-option-label::after { content: ""; position: absolute; inset: 0; }
.ai-option-title { font-size: 13.5px; color: var(--text); display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
.ai-option-title .mono { white-space: nowrap; }
.ai-option:has(input:checked) { border-color: var(--blue); background: var(--blue-dim); }
/* Переключатели («Поведение» в параметрах публикации) — подпись+подсказка
   справа от чекбокса, весь ряд кликабелен целиком. .toggle-grid — тот же
   приём, что у .ai-options: на широком экране раскладывает переключатели
   в две колонки вместо длинного одного столбца. */
.toggle-grid { display: block; }
.toggle-row { display: flex; align-items: flex-start; gap: 9px; margin-bottom: 10px; cursor: pointer; }
.toggle-row:last-child { margin-bottom: 0; }
.toggle-row input[type=checkbox] { margin-top: 3px; width: 16px; height: 16px; flex-shrink: 0; accent-color: var(--blue); }
.toggle-title { color: var(--text); font-size: 13px; }
/* Группы полей в «Параметры публикации» — на узком идут одна под другой;
   на широком (см. media ниже) складываются в две газетные колонки, чтобы
   раздел «Настройки» помещался в экран без прокрутки. */
.field-groups { display: block; }
.settings-group { margin-bottom: 14px; }
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
  /* Когда каждая кнопка в .card-actions — своя отдельная <form> (несколько
     независимых действий подряд, как в очереди согласования), stretch выше
     растягивает саму форму, а не кнопку внутри неё — кнопка остаётся по
     размеру текста и прижимается к левому краю. display:contents убирает
     форму из раскладки, оставляя кнопку прямым flex-элементом. */
  .card-actions > form { display: contents; }
}
@media (min-width: 641px) {
  .side-nav { display: flex; }
  .bottom-nav { display: none; }
  body { padding-bottom: 0; }
  header .logout { display: none; }
  header { padding: 9px 20px; }
  header h1.brand-mobile { display: none; }
  header .page-title { display: block; }
  /* На узком экране это единственный заголовок страницы (в шапке — только
     бренд), поэтому там он нужен; на широком его дублирует page-title в
     шапке — второй раз просто не рендерим. */
  h2.page-heading { display: none; }
  main { padding: 0 20px; margin: 14px auto; }
  .card { padding: 12px 15px; }
  .row > * { flex: 1 1 auto; }
  /* Плотность на широком экране — вторая (после самих отступов) главная
     мера, которой раздел «Настройки» умещается в экран без прокрутки:
     input/textarea/select без ограничения в 16px (актуального только для
     iOS Safari — на десктопе зума при фокусе нет), кнопки и карточки-строки
     компактнее, чем в мобильной раскладке. */
  input[type=text], input[type=password], input[type=number], select { font-size: 13px; padding: 7px 9px; }
  button, .btn { min-height: 32px; padding: 7px 12px; }
  button.icon, .btn.icon { min-height: 28px; min-width: 28px; }
  .list-item { padding: 8px 14px; }
  .stat-card { padding: 9px 12px; min-height: 56px; }
  .hero-card { padding: 12px 15px; }
  .ai-option { padding: 8px 10px; min-height: auto; margin-bottom: 0; }
  /* Первый вариант (обычная модель, без сноски про отсутствующий ключ) —
     во всю ширину сверху, Claude/Gemini — по половине под ним. Три равные
     колонки в один ряд были слишком узкими: подпись с моделью и пометкой
     «платно»/«бесплатно» переносилась на 2-3 строки и раздувала карточку. */
  .ai-options { display: grid; grid-template-columns: 1fr 1fr; gap: 7px; }
  .ai-options .ai-option:first-child { grid-column: 1 / -1; }
  .toggle-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 2px 24px; }
  .toggle-row { margin-bottom: 6px; }
  /* Каждая группа настроек (см. GENERAL_GROUPS) — на всю ширину карточки,
     поля внутри неё в один ряд (.field-row и так flex-wrap, а на всю ширину
     широкого экрана 3-4 поля умещаются без переноса). Раньше здесь была
     сетка узких колонок-групп — из-за неё поля внутри каждой группы
     наоборот складывались в высокий столбец, это было заметно выше. */
  .field-groups { display: block; }
  .settings-group { margin-bottom: 8px; }
  .settings-group .field-row { margin-bottom: 0; }
  .settings-group h3 { margin-bottom: 6px; }
  /* Подсказка под полем — в одну строку с многоточием вместо 1-2 строк:
     на мобильном имеет смысл читать её целиком сразу, на широком — соседние
     поля в ряду и так на виду, а полный текст всегда доступен по наведению
     (title=, задан в settings_get). Экономит по вертикали ощутимо, потому
     что несколько полей в разделе «Настройки» иначе разъезжаются на 2
     строки подсказки из-за длины текста. */
  .settings-group .field-hint {
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; cursor: help;
  }
  /* Список лент/сайтов без RSS — на широком экране в две колонки (см.
     .list-grid в feeds_get), иначе список из скольких-нибудь лент один
     не даёт разделу поместиться на экране без прокрутки. Правая колонка —
     с вертикальным разделителем вместо второй рамки, чтобы не задваивать
     обводку внутри уже обведённого .list. */
  .list.list-grid { display: grid; grid-template-columns: 1fr 1fr; }
  .list.list-grid .list-item:nth-child(odd) { border-right: 1px solid var(--border-soft); }
  .list.list-grid .list-item:last-child { border-right: none; }
  .dupe-thumb { width: 44px; height: 44px; }
  .content-grid, .settings-columns { display: grid; gap: 0 22px; align-items: start; }
  /* Промпт и формат — сознательно парная пара карточек рядом; тянем их до
     одной высоты, чтобы кнопки под ними не разъезжались на разных уровнях. */
  .content-grid { grid-template-columns: 1fr 1fr; align-items: stretch; }
  /* auto-fit, не жёсткие 1fr 1fr: карточек в этом блоке то 4, то 5 (появилось
     «Согласование») — при нечётном числе жёсткая сетка оставляла бы половину
     последней строки пустой; auto-fit сам решает, сколько влезает в ряд. */
  .settings-columns { grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); }
  .content-grid > div { display: flex; flex-direction: column; }
  .content-grid .card { display: flex; flex-direction: column; flex: 1; }
  .content-grid .card form { display: flex; flex-direction: column; flex: 1; }
  .content-grid textarea { flex: 1; }
  /* «Канал» — короткая форма (одно поле) рядом с более длинной «Обработка
     новостей» (три варианта): без растягивания карточка с одним полем
     заканчивается на середине высоты соседней, оставляя пустой разрыв под
     собой — тот же приём растягивания, что у .content-grid, только кнопку
     внутри карточки не выталкивает вверх/вниз, а прижимает к низу
     (margin-top:auto), оставляя воздух между полем и кнопкой внутри рамки
     карточки — так короткая форма визуально уравнивается с высокой, а не
     висит обрубком. */
  .settings-columns { align-items: stretch; }
  .settings-columns > div { display: flex; flex-direction: column; }
  .settings-columns .card { display: flex; flex-direction: column; flex: 1; }
  .settings-columns .card form { display: flex; flex-direction: column; flex: 1; }
  .settings-columns .card-actions { margin-top: auto; padding-top: 12px; }
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
// Разрушающие действия (удаление ленты/картинки/дубля) подтверждаются перед
// отправкой формы. Внутри Telegram Mini App нативный window.confirm() у
// части клиентов (особенно десктопного) не поддерживается синхронно, как
// того требует onsubmit="return confirm(...)" — используем showConfirm()
// моста, когда он есть, и обычный confirm() только вне Telegram.
function tgConfirmSubmit(form, message) {
  var tg = window.Telegram && window.Telegram.WebApp;
  if (tg && tg.showConfirm) {
    tg.showConfirm(message, function (ok) { if (ok) form.submit(); });
  } else if (window.confirm(message)) {
    form.submit();
  }
  return false;
}
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
  // Каждая страница по умолчанию прячет MainButton/BackButton — это полная
  // перезагрузка страницы, а не SPA, так что состояние кнопок с прошлой
  // страницы (см. очередь согласования ниже) иначе могло бы протечь на
  // страницы, которые о нём не знают. Кто хочет свои — включает сам,
  // ниже, в собственном <script> конкретной страницы.
  try { if (tg.MainButton) tg.MainButton.hide(); } catch (e) {}
  try { if (tg.BackButton) tg.BackButton.hide(); } catch (e) {}
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
    ("/queue", "🖐", "Очередь"),
    ("/content", "📝", "Контент"),
    ("/settings", "⚙️", "Настройки"),
    ("/posts", "📮", "Посты"),
]


async def _usage_body(pub: "Publisher") -> str:
    if pub.quota is None:
        return ""
    # Расход у каждого бэкенда свой (см. bot/quota.py) — раньше здесь всегда
    # показывался счётчик обычного LLM, даже когда реально работал Gemini
    # или Claude, и их расход нигде не отражался вообще.
    backend = pub.backend_key
    info = await pub.quota.snapshot(backend, force=True)
    rows = [
        ("Запросов сегодня", f"{info.requests}" + (f" из {info.request_limit} ({info.request_pct:.0f}%)" if info.request_limit else "")),
        ("Токены", f"{info.tokens_in} вход / {info.tokens_out} выход"),
    ]
    if info.cost:
        rows.append(("Стоимость", f"{info.cost:.4f} кредита"))
    if info.request_limit:
        rows.append(("Обнуление лимита", f"через {until_reset()} (00:00 UTC), источник: {info.limit_source}"))
    if info.credit_limit is not None:
        rows.append(("Кредиты на ключе", f"{info.credit_limit:.4f}, осталось {info.credit_remaining:.4f}"))
    hint = ("Обычный режим — расход и лимиты по ключу OpenRouter." if backend == "default"
           else f"Активен режим {pub.active_backend_label} — показан расход именно этого бэкенда, "
                "у него свой отдельный счёт.")
    return ("<h2>Расход за сутки</h2>"
            f"<div class='section-hint'>{_e(hint)}</div>"
            "<div class='card scroll'><table class='kv'>") + "".join(
        f"<tr><td class='muted'>{_e(k)}</td><td>{_e(v)}</td></tr>" for k, v in rows
    ) + "</table></div>"


def _layout(title: str, body: str, flash: str = "", flash_kind: str = "ok", active: str = "",
           wide: bool = False, flash_action: str = "") -> str:
    # flash_action — готовый HTML (например, форма «Отменить»), не текст:
    # вызывающий код сам решает, что туда положить, поэтому не экранируем,
    # в отличие от flash. Пусто по умолчанию — большинство флешей его не используют.
    flash_html = (f'<div class="flash {flash_kind}">{_e(flash)}{flash_action}</div>' if flash else "")

    nav_html = "".join(
        f'<a href="{path}" class="nav-link{" active" if path == active else ""}">'
        f'<span class="ic">{icon}</span><span class="lbl">{label}</span>'
        f'<span class="nav-dot" aria-hidden="true"></span></a>'
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


def _safe_next(path: str) -> str:
    """Путь для возврата после входа — только свой же адрес. Без этого
    /login?next=https://... стал бы открытым редиректом, а голый "//evil"
    браузер тоже понимает как переход на чужой хост."""
    if path and path.startswith("/") and not path.startswith("//") and "://" not in path:
        return path
    return "/"


def _login_page(error: str = "", next_path: str = "/") -> str:
    next_path = _safe_next(next_path)
    err_html = f'<div class="flash err">{_e(error)}</div>' if error else ""
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Вход — bot panel</title>
{TG_INIT_SCRIPT}
<style>{STYLE}</style></head><body>
<main style="max-width:360px; margin:calc(15vh + var(--tg-top)) auto 0;">
  <div style="text-align:center; font-size:34px; margin-bottom:6px;">📰</div>
  <h2 style="text-align:center;">Вход в панель</h2>
  {err_html}
  <div id="tgLoginNote" class="flash ok" style="display:none;">Вхожу через Telegram…</div>
  <div class="card">
    <form method="post" action="/login">
      <input type="hidden" name="next" value="{_e(next_path)}">
      <label for="login-password">Пароль</label>
      <input type="password" id="login-password" name="password" required>
      <div style="margin-top:12px;"><button class="primary" type="submit" style="width:100%;">Войти</button></div>
    </form>
  </div>
</main>
<script>
(function () {{
  var nextPath = {json.dumps(next_path)};
  var tg = window.Telegram && window.Telegram.WebApp;
  if (!tg || !tg.initData) {{
    // Вне Telegram авто-входа не будет — фокус на пароль как раньше,
    // просто не через статичный autofocus (который иначе открывал бы
    // клавиатуру и внутри Telegram, где поле обычно не нужно вовсе).
    document.getElementById('login-password').focus();
    return;
  }}
  document.getElementById('tgLoginNote').style.display = 'block';
  // На десктопном клиенте Telegram fetch() к /tg-login иногда подвисает
  // навсегда (ни then, ни catch не срабатывают) — плашка «Вхожу через
  // Telegram…» тогда висит бесконечно, закрывая доступ к полю пароля.
  // Таймаут — подстраховка от любого такого зависания, не только этого.
  var settled = false;
  function finish(ok) {{
    if (settled) return;
    settled = true;
    if (ok) {{ location.href = nextPath; }}
    else {{ document.getElementById('tgLoginNote').style.display = 'none'; }}
  }}
  setTimeout(function () {{ finish(false); }}, 6000);
  try {{
    fetch('/tg-login', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{initData: tg.initData}})
    }}).then(function (r) {{ finish(r.ok); }}).catch(function () {{ finish(false); }});
  }} catch (e) {{ finish(false); }}
}})();
</script>
</body></html>"""


def _redirect(path: str) -> web.HTTPFound:
    return web.HTTPFound(path)


def _gone_page(title: str, message: str, status: int = 404,
              back_href: str = "/", back_label: str = "На главную") -> web.Response:
    """Страница-тупик (404/битый id и т.п.) через _layout, а не голый текст —
    в Telegram Mini App системного «назад» может не быть вовсе, без ссылки
    это реальный тупик, не только неаккуратная вёрстка."""
    body = (f'<div class="card"><p>{_e(message)}</p>'
           f'<a class="btn" href="{_e(back_href)}">{_e(back_label)}</a></div>')
    return web.Response(status=status, text=_layout(title, body), content_type="text/html")


# ======================== приложение ========================
def create_app(storage: Storage, publisher: Publisher, bot: Bot, password: str,
               admin_ids: set[int] | None = None, secure_cookies: bool = False
               ) -> web.Application:
    auth = WebAuth(password)
    admin_ids = admin_ids or set()
    app = web.Application()
    app["auth"] = auth
    app["st"] = storage
    app["publisher"] = publisher
    app["bot"] = bot
    # Черновики после перегенерации поста через ИИ — держим в памяти между
    # POST /regen и последующим GET, а не отдаём их прямо в ответе на POST
    # (см. post_regen/post_detail): иначе обновление страницы/pull-to-refresh
    # в Telegram Mini App переотправляет тот же POST и повторно тратит деньги
    # на ИИ-перегенерацию, которую админ не запрашивал. Значение — (текст,
    # время создания): если админ так и не сохранил и не открыл пост заново,
    # запись иначе осталась бы в памяти процесса навсегда — см. REGEN_DRAFT_TTL.
    app["regen_drafts"]: dict[int, tuple[str, float]] = {}
    # Последняя отклонённая карточка очереди согласования на сессию — для
    # «Отменить» во флеше вместо confirm()-диалога на каждое отклонение (см.
    # queue_reject/queue_undo). Словарь по токену сессии, не один общий
    # слот: иначе «Отменить» у одного залогиненного админа могло вернуть
    # отклонение, сделанное другим — оба работают с одной и той же общей
    # панелью без разграничения личности (см. WebAuth). Внутри одной
    # сессии — по-прежнему только самое недавнее действие, более ранние
    # не актуальны.
    app["undo_stash"]: dict[str, tuple[dict, float]] = {}
    # SameSite=Lax (без Secure) — обычный браузер по прямому https/http-адресу.
    # SameSite=None+Secure — когда панель открыта как Telegram Mini App
    # (WEB_PANEL_PUBLIC_URL задан, Telegram требует https для WebApp URL):
    # встроенный вебвью, особенно у десктопного клиента, не всегда считает
    # переход на "/" после fetch-запроса тем же сайтом для целей SameSite=Lax
    # — кука тогда просто не долетает до следующего запроса, авторизация не
    # закрепляется, и /login открывается заново (в этот момент сбрасывая
    # то, что уже успели напечатать в поле пароля — выглядит как «сбрасывается
    # при вводе», хотя на деле это была молчаливая гонка редиректов).
    app["cookie_kwargs"] = ({"samesite": "None", "secure": True} if secure_cookies
                            else {"samesite": "Lax"})

    PUBLIC_PATHS = {"/login", "/tg-login"}

    @web.middleware
    async def bad_id_middleware(request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]):
        # Роуты вида /posts/{id}, /feeds/{id}/... делают int(match_info["id"])
        # без проверки — нечисловой id (ссылка вручную, битая закладка) иначе
        # ронял бы запрос в голый 500 вместо понятного «не найдено».
        try:
            return await handler(request)
        except ValueError:
            return _gone_page("Не найдено", "Некорректный идентификатор в адресе.")
        except web.HTTPNotFound as exc:
            # Хендлеры делают `raise web.HTTPNotFound(text="...")` (запись уже
            # удалена/обработана — обычное дело для очередей на разбор, не
            # только опечатка в адресе) — тот же тупик без стилей и выхода,
            # если не перехватить здесь и не отрисовать через _layout.
            return _gone_page("Не найдено", exc.text or "Запись не найдена.")

    app.middlewares.append(bad_id_middleware)

    @web.middleware
    async def security_headers_middleware(request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]):
        # Полноценный CSP тут не завести без переписывания вёрстки — весь
        # HTML/CSS/JS страницы инлайновый (см. STYLE/TG_INIT_SCRIPT и
        # style=/onsubmit= по всему файлу), 'unsafe-inline' свёл бы CSP к
        # пустой формальности. X-Frame-Options не ставим — панель открывается
        # как Telegram Mini App во встроенном webview. То, что можно включить
        # без риска что-то сломать, — включаем.
        resp = await handler(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        return resp

    app.middlewares.append(security_headers_middleware)

    @web.middleware
    async def auth_middleware(request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]):
        if request.path in PUBLIC_PATHS:
            return await handler(request)
        session = auth.verify(request.cookies.get(SESSION_COOKIE))
        if session is None:
            # ?next= — чтобы после входа вернуться туда, куда шли (например,
            # по кнопке "Открыть очередь" из уведомления), а не на дашборд.
            return _redirect(f"/login?next={quote(request.path, safe='')}")
        request["csrf"] = session["csrf"]
        if request.method == "POST":
            form = await request.post()
            if not secrets.compare_digest(str(form.get("csrf", "")), session["csrf"]):
                # Голый текст без стилей и без выхода — тупик для Telegram
                # Mini App, где назад можно уйти только системным жестом.
                body = ('<div class="card"><p>Токен формы устарел — обычно значит, что страница была'
                       ' открыта давно в другой вкладке. Обновите её и попробуйте снова.</p>'
                       '<a class="btn" href="/">На главную</a></div>')
                return web.Response(status=403, text=_layout("Токен устарел", body), content_type="text/html")
            request["form"] = form
        return await handler(request)

    app.middlewares.append(auth_middleware)

    def csrf_field(request: web.Request) -> str:
        return f'<input type="hidden" name="csrf" value="{_e(request["csrf"])}">'

    # --- аутентификация ---------------------------------------------------
    async def login_get(request: web.Request) -> web.Response:
        next_path = _safe_next(request.query.get("next", "/"))
        if auth.verify(request.cookies.get(SESSION_COOKIE)):
            return _redirect(next_path)
        return web.Response(text=_login_page(next_path=next_path), content_type="text/html")

    def client_ip(request: web.Request) -> str:
        # За nginx (см. SETUP.md) request.remote — всегда 127.0.0.1, и все
        # посетители делят один и тот же счётчик неудачных попыток: пять
        # чужих неверных паролей блокируют вход и самому админу. Доверяем
        # X-Forwarded-For только когда панель действительно поднята за
        # прокси (secure_cookies включён вместе с WEB_PANEL_PUBLIC_URL) —
        # иначе это открытый порт наружу, и заголовок мог бы подделать кто
        # угодно, обходя блокировку.
        if secure_cookies:
            fwd = request.headers.get("X-Forwarded-For")
            if fwd:
                # nginx's $proxy_add_x_forwarded_for APPENDS the real client
                # IP after whatever the client already sent in this header,
                # so the last hop is the only part nginx itself guarantees —
                # taking the first would let a client fake any IP and dodge
                # the lockout below.
                return fwd.split(",")[-1].strip()
        return request.remote or "?"

    async def login_post(request: web.Request) -> web.Response:
        ip = client_ip(request)
        form = await request.post()
        next_path = _safe_next(str(form.get("next", "/")))
        if auth.locked_out(ip):
            return web.Response(
                text=_login_page(f"Слишком много попыток — подождите {LOGIN_LOCKOUT // 60} минут.",
                                 next_path=next_path),
                content_type="text/html", status=429)
        if not auth.check(str(form.get("password", ""))):
            auth.record_fail(ip)
            return web.Response(text=_login_page("Неверный пароль.", next_path=next_path),
                                content_type="text/html", status=401)
        auth.record_success(ip)
        token = auth.new_session()
        resp = _redirect(next_path)
        resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL, httponly=True, **app["cookie_kwargs"])
        return resp

    async def logout_post(request: web.Request) -> web.Response:
        auth.revoke(request.cookies.get(SESSION_COOKIE))
        resp = _redirect("/login")
        resp.del_cookie(SESSION_COOKIE, **app["cookie_kwargs"])
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
        resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL, httponly=True, **app["cookie_kwargs"])
        return resp

    # --- статус -------------------------------------------------------------
    async def dashboard(request: web.Request, flash: str = "", flash_kind: str = "ok") -> web.Response:
        st: Storage = app["st"]
        pub: Publisher = app["publisher"]
        feeds = st.feeds()
        active = sum(1 for f in feeds if f["enabled"])
        errors = [f for f in feeds if f["last_error"]]
        paused = st.get("paused") == "1"
        queue_n = st.count_moderation()
        if pub.debug and paused:
            hero_class, state_text = "debug", "Отладка · на паузе"
        elif pub.debug:
            hero_class, state_text = "debug", "Отладка — посты в личку"
        elif pub.moderation and paused:
            hero_class, state_text = "moderation", "Согласование · на паузе"
        elif pub.moderation:
            hero_class, state_text = "moderation", f"Согласование{f' · ждут: {queue_n}' if queue_n else ''}"
        elif paused:
            hero_class, state_text = "paused", "На паузе"
        else:
            hero_class, state_text = "", "Работает"
        feeds_value = f"{active} / {len(feeds)}"
        if errors:
            feeds_value += f' <span class="pill off">{len(errors)} с ошибкой</span>'
        dupes = st.count_dedup_candidates()
        postponed = st.count_postponed()
        stats = [
            ("Модель", _e(pub.active_backend_label)),
            ("Канал", _e(pub.channel or "не задан")),
            ("Ленты (активно/всего)", feeds_value),
            ("VK", ('<span class="pill on">' + _e(pub.vk_group) + '</span>')
                   if pub.vk_on else '<span class="pill neutral">выключен</span>'),
        ]
        # Пилюлю очереди согласования показываем и без включённого режима —
        # выключили с непустой очередью, сами карточки не публикуются (см.
        # settings_moderation), забыть про них иначе легко.
        if queue_n:
            stats.append(("Согласование", f'<a href="/queue" class="pill" '
                                          f'style="text-decoration:none; background:var(--purple-dim); '
                                          f'color:var(--purple);">{queue_n} ждут ›</a>'))
        if postponed:
            stats.append(("Отложенные", f'<a href="/feeds#postponed" class="pill off" '
                                        f'style="text-decoration:none;">{postponed} ждут — '
                                        f'модель отказала ›</a>'))
        if dupes:
            stats.append(("Дубли", f'<a href="/feeds#duplicates" class="pill warn" style="text-decoration:none;">{dupes} на разбор ›</a>'))
        stat_html = "".join(
            f'<div class="stat-card"><div class="stat-label">{_e(k)}</div>'
            f'<div class="stat-value">{v}</div></div>' for k, v in stats
        )
        body = f"""
        <h2 class="page-heading">Статус</h2>
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
        return web.Response(text=_layout("Статус", body, flash, flash_kind, active="/", wide=True), content_type="text/html")

    async def pause_post(request: web.Request) -> web.Response:
        st: Storage = app["st"]
        st.set("paused", "0" if st.get("paused") == "1" else "1")
        return _redirect("/")

    async def checknow_post(request: web.Request) -> web.Response:
        # Голый редирект без обратной связи выглядел как нерабочая кнопка —
        # результат (новые посты) появляется не мгновенно, а после того как
        # проснувшийся цикл реально опросит ленты.
        pub: Publisher = app["publisher"]
        pub.wake()
        return await dashboard(request, "Проверка запущена — новые публикации появятся в течение минуты.")

    # --- ленты ---------------------------------------------------------------
    def _dupes_section_html(st: "Storage") -> str:
        """Дубли между лентами показываются здесь же, на «Лентах» — это тоже
        решение по выдаче лент, а не отдельная самостоятельная сущность.
        Пусто — секция не рендерится вовсе, чтобы не мозолить глаза, когда
        разбирать нечего (как бейдж на дашборде)."""
        dupes = st.dedup_candidates(50)
        if not dupes:
            return ""
        total = st.count_dedup_candidates()
        more_note = (f'<div class="muted" style="margin-top:6px;">Показаны первые 50 из {total}.</div>'
                    if total > len(dupes) else "")
        matched_ids = st.existing_post_ids(
            [r["matched_post_id"] for r in dupes if r["matched_post_id"]])
        items = ""
        for r in dupes:
            matched = r["matched_post_id"] in matched_ids
            matched_html = (f'<a href="/posts/{r["matched_post_id"]}">пост #{r["matched_post_id"]}</a>'
                            if matched else f'пост #{r["matched_post_id"]} (уже удалён)')
            thumb = (f'<img class="dupe-thumb" src="{_safe_href(r["image"])}" alt="" '
                    f'style="object-fit:cover;">'
                    if r["image"] and _is_http_url(r["image"])
                    else '<div class="dupe-thumb" style="background:var(--field-bg);"></div>')
            when = time.strftime("%d.%m %H:%M", time.localtime(r["detected_at"]))
            items += f"""<div class="list-item">
              {thumb}
              <div class="list-item-info">
                <div class="list-item-title">{_e(r['title'][:140])} <span class="pill neutral">{r['score']:.0%}</span></div>
                <div class="muted">{_e(r['source'] or 'без ленты')} · найдено {when} · похоже на {matched_html}</div>
              </div>
              <div class="list-item-actions">
                <a class="btn icon" href="/duplicates/{r['id']}" title="Подробнее" aria-label="Подробнее">›</a>
              </div>
            </div>"""
        return f"""
        <h2 id="duplicates">Дубли <span class="muted" style="font-weight:400;">({total})</span></h2>
        <div class="section-hint">Похожи на уже опубликованные с другой ленты — не в канале, ждут решения.</div>
        <div class="list">{items}</div>
        {more_note}
        """

    def _postponed_section_html(st: "Storage") -> str:
        """Новости, на которых модель отказала (сбой бэкенда, квота, гео-блок
        и т.п.) — не потеряны, каждый автопроход переоценивает их заново
        сам, но до тех пор админ не видел вообще ничего, кроме почасового
        отчёта в личке. Пусто — секция не рендерится, как и «Дубли»."""
        rows = st.postponed_list(50)
        if not rows:
            return ""
        total = st.count_postponed()
        more_note = (f'<div class="muted" style="margin-top:6px;">Показаны первые 50 из {total}.</div>'
                    if total > len(rows) else "")
        items = ""
        for r in rows:
            when = time.strftime("%d.%m %H:%M", time.localtime(r["last_failed_at"]))
            attempts = f" · попыток: {r['attempts']}" if r["attempts"] > 1 else ""
            items += f"""<div class="list-item">
              <div class="list-item-info">
                <div class="list-item-title">{_e(r['title'][:140])}</div>
                <div class="muted">{_e(r['feed_title'] or 'лента удалена')} · отказ {when}{attempts}</div>
                <div class="muted" style="color:var(--red)">{_e(r['error'][:150])}</div>
              </div>
              <div class="list-item-actions">
                <a class="btn icon" href="/postponed/{r['id']}" title="Подробнее" aria-label="Подробнее">›</a>
              </div>
            </div>"""
        return f"""
        <h2 id="postponed">Отложенные <span class="muted" style="font-weight:400;">({total})</span></h2>
        <div class="section-hint">Модель отказала при обработке — новость не потеряна и переоценивается
          сама на каждом автопроходе; здесь можно повторить прямо сейчас или отказаться от публикации.</div>
        <div class="list">{items}</div>
        {more_note}
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
                    if f["kind"] == "search" and f["article_path"] else "")
        multi_label = "Одна картинка, как раньше" if f["multi_images"] else "Публиковать несколько картинок альбомом"
        toggle_label = "Поставить на паузу" if f["enabled"] else "Включить"
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
            <a class="btn icon" href="/feeds/{f['id']}/template" title="Свой промпт" aria-label="Свой промпт">🤖</a>
            <form class="inline" method="post" action="/feeds/{f['id']}/multiimages">{csrf_field(request)}
              <button class="icon" type="submit" title="{multi_label}" aria-label="{multi_label}">🖼</button></form>
            <form class="inline" method="post" action="/feeds/{f['id']}/toggle">{csrf_field(request)}
              <button class="icon" type="submit" title="{toggle_label}" aria-label="{toggle_label}">{'⏸' if f['enabled'] else '▶️'}</button></form>
            <form class="inline" method="post" action="/feeds/{f['id']}/delete"
                  onsubmit="return tgConfirmSubmit(this, 'Удалить #{f['id']}?')">{csrf_field(request)}
              <button class="icon" type="submit" title="Удалить" aria-label="Удалить">✕</button></form>
          </div>
        </div>"""

    async def feeds_get(request: web.Request, flash: str = "", flash_kind: str = "ok",
                        rss_prefill: dict[str, str] | None = None,
                        search_prefill: dict[str, str] | None = None) -> web.Response:
        st: Storage = app["st"]
        dupes_html = _dupes_section_html(st)
        postponed_html = _postponed_section_html(st)
        rows = st.feeds()
        rss_rows = [f for f in rows if f["kind"] != "search"]
        search_rows = [f for f in rows if f["kind"] == "search"]

        # grid-column: 1/-1 — на широком экране .list.list-grid раскладывает
        # содержимое в 2 колонки (см. CSS), без этого заглушка занимала бы
        # только левую половину карточки вместо центрирования по всей ширине.
        rss_list = "".join(_feed_row_html(f, request, st)
                           for f in rss_rows) or (
            "<div style='padding:28px 16px; text-align:center; grid-column:1/-1;'>"
            "<div style='font-size:28px; margin-bottom:8px;'>📰</div>"
            "<div class='muted'>Лент пока нет — добавьте первую выше.</div></div>"
        )
        search_list = "".join(_feed_row_html(f, request, st)
                              for f in search_rows) or (
            "<div style='padding:28px 16px; text-align:center; grid-column:1/-1;'>"
            "<div style='font-size:28px; margin-bottom:8px;'>🔎</div>"
            "<div class='muted'>Сайтов без RSS пока нет — добавьте первый выше.</div></div>"
        )

        # При ошибке добавления форма перерисовывается заново — раньше поля
        # были всегда пустыми (value= не подставлялся), и адрес с названием
        # приходилось набирать заново; заодно открываем <details>, чтобы
        # ошибку было видно сразу, а не только после ручного разворачивания.
        rss_prefill = rss_prefill or {}
        search_prefill = search_prefill or {}

        body = f"""
        {postponed_html}
        {dupes_html}
        <h2>Ленты <span class="muted" style="font-weight:400;">({len(rss_rows)})</span></h2>
        <details {"open" if rss_prefill else ""}>
          <summary class="disclosure">Добавить ленту</summary>
          <div class="card" style="margin-top:10px;">
            <form method="post" action="/feeds/add">{csrf_field(request)}
              <div class="row" style="align-items:flex-end;">
                <div style="flex:2;"><label for="rss-url">URL ленты</label><input type="text" id="rss-url" name="url" placeholder="https://example.com/rss" value="{_e(rss_prefill.get('url', ''))}" required></div>
                <div style="flex:1;"><label for="rss-title">Название (необязательно)</label><input type="text" id="rss-title" name="title" value="{_e(rss_prefill.get('title', ''))}"></div>
                <button class="primary" type="submit">Добавить</button>
              </div>
            </form>
          </div>
        </details>
        <div class="list list-grid" style="margin-top:10px;">{rss_list}</div>

        <h2>Сайты без RSS <span class="muted" style="font-weight:400;">({len(search_rows)})</span></h2>
        <div class="section-hint">Новости с сайтов, где нет RSS-ленты — находятся через веб-поиск, а не
          разбором ленты: у собственных средств сайта узнать «что нового» (sitemap.xml, страница списка
          новостей) кэш нередко отдаёт устаревший снимок, поиск от этого не зависит. Нужен ключ поиска —
          см. SETUP.md.</div>
        <details {"open" if search_prefill else ""}>
          <summary class="disclosure">Добавить сайт без RSS</summary>
          <div class="card" style="margin-top:10px;">
            <form method="post" action="/feeds/add-search">{csrf_field(request)}
              <div class="row" style="align-items:flex-end;">
                <div style="flex:2;"><label for="search-url">Адрес сайта</label><input type="text" id="search-url" name="url" placeholder="https://example.com/" value="{_e(search_prefill.get('url', ''))}" required></div>
                <div style="flex:1;"><label for="search-title">Название (необязательно)</label><input type="text" id="search-title" name="title" value="{_e(search_prefill.get('title', ''))}"></div>
              </div>
              <div class="row" style="align-items:flex-end; margin-top:8px;">
                <div style="flex:2;"><label for="search-article-path">Часть адреса статей (необязательно)</label>
                  <input type="text" id="search-article-path" name="article_path" placeholder="/articles/" value="{_e(search_prefill.get('article_path', ''))}"></div>
                <button class="primary" type="submit">Добавить</button>
              </div>
              <div class="field-hint" style="margin-top:4px;">Сужает поисковый запрос (site:домен + этот
                путь) — нужна, если на сайте вперемешку новости и другие страницы (товары, категории).</div>
            </form>
          </div>
        </details>
        <div class="list list-grid" style="margin-top:10px;">{search_list}</div>
        """
        return web.Response(text=_layout("Ленты", body, flash, flash_kind, active="/feeds"), content_type="text/html")

    async def feeds_add(request: web.Request) -> web.Response:
        form = request["form"]
        url = str(form.get("url", "")).strip()
        title = str(form.get("title", "")).strip()
        prefill = {"url": url, "title": title}
        if not url.lower().startswith(("http://", "https://")):
            return await feeds_get(request, "Нужна ссылка, начинающаяся на http:// или https://", "err", rss_prefill=prefill)
        result = await fetch(url)
        if result.error:
            return await feeds_get(request, f"Лента недоступна: {result.error}", "err", rss_prefill=prefill)
        if not result.entries:
            return await feeds_get(request, "В ленте нет записей — проверьте адрес.", "err", rss_prefill=prefill)
        feed_id = app["st"].add_feed(url, title or result.feed_title[:120])
        if feed_id is None:
            return await feeds_get(request, "Такая лента уже добавлена.", "err", rss_prefill=prefill)
        app["publisher"].wake()
        return _redirect("/feeds")

    async def feeds_add_search(request: web.Request) -> web.Response:
        form = request["form"]
        url = str(form.get("url", "")).strip()
        title = str(form.get("title", "")).strip()
        article_path = str(form.get("article_path", "")).strip()
        prefill = {"url": url, "title": title, "article_path": article_path}
        if not url.lower().startswith(("http://", "https://")):
            return await feeds_get(request, "Нужна ссылка, начинающаяся на http:// или https://", "err", search_prefill=prefill)
        publisher: Publisher = app["publisher"]
        if publisher.bing is None and (publisher.search is None or not publisher.search.configured):
            return await feeds_get(
                request,
                "Поиск недоступен ни одним из источников. Проверьте SERPER_API_KEY в .env "
                "— см. SETUP.md, раздел «Сайты без RSS».", "err", search_prefill=prefill)
        domain = urlsplit(url).netloc
        if not domain:
            return await feeds_get(request, "Не разобрал домен в этой ссылке.", "err", search_prefill=prefill)
        query = site_query(domain, article_path)
        items, error = await publisher.search_articles(domain, article_path)
        if error:
            return await feeds_get(request, f"Поиск не ответил: {error}", "err", search_prefill=prefill)
        if not items:
            return await feeds_get(
                request,
                f"По запросу «{query}» поиск ничего не нашёл за последнюю неделю — либо сайт "
                f"не публиковал новостей, либо часть адреса статей выбрана неверно.", "err", search_prefill=prefill)
        feed_id = app["st"].add_feed(url, title, kind="search", article_path=article_path)
        if feed_id is None:
            return await feeds_get(request, "Такой сайт уже добавлен.", "err", search_prefill=prefill)
        app["publisher"].wake()
        return _redirect("/feeds")

    async def feeds_delete(request: web.Request) -> web.Response:
        feed_id = int(request.match_info["id"])
        app["st"].delete_feed(feed_id)
        return await feeds_get(request, flash="Лента удалена.")

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
                    f'onclick="return tgConfirmSubmit(this.form, \'Вернуть общий промпт? Свой текст для '
                    f'этой ленты будет потерян.\')">Вернуть общий промпт</button>' if is_custom else "")
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
        <h2 class="page-heading">Контент</h2>
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
                      onclick="return tgConfirmSubmit(this.form, 'Сбросить промпт к умолчанию? Текущий текст будет потерян.')">Сбросить к умолчанию</button>
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
                      onclick="return tgConfirmSubmit(this.form, 'Сбросить формат к умолчанию? Текущий текст будет потерян.')">Сбросить к умолчанию</button>
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
        if not text:
            return await content_get(request, "Формат поста не может быть пустым — не сохранено.", "err",
                                     format_draft=text)
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
    async def settings_get(request: web.Request, flash: str = "", flash_kind: str = "ok",
                           prefill: dict[str, str] | None = None) -> web.Response:
        st: Storage = app["st"]
        pub: Publisher = app["publisher"]
        prefill = prefill or {}

        def field(key: str, label: str, unit: str, hint: str) -> str:
            # title=hint — на широком экране .field-hint обрезается в одну
            # строку (иначе разнобой в высоте подсказок раздувает раздел
            # «Настройки» по вертикали), полный текст всплывает по наведению.
            # for=/id= — без них скринридер объявляет поле безымянным.
            # prefill — то, что админ только что ввёл (при ошибке валидации
            # в ОДНОМ поле форма иначе перерисовывалась бы значениями из БД,
            # молча стирая правки во всех остальных полях того же сохранения).
            field_id = f"field-{_e(key)}"
            value = prefill.get(key, st.get(key))
            return (f'<div class="field"><label for="{field_id}">{_e(label)} <span class="unit">({_e(unit)})</span></label>'
                    f'<input type="text" id="{field_id}" name="{_e(key)}" value="{_e(value)}">'
                    f'<div class="field-hint" title="{_e(hint)}">{_e(hint)}</div></div>')

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
        <h2 class="page-heading">Настройки</h2>
        <div class="settings-columns">
        <div>
        <h2>Канал</h2>
        <div class="card">
          <form method="post" action="/settings/channel">{csrf_field(request)}
            <div class="row" style="align-items:flex-end;">
              <div style="flex:1;"><label for="settings-channel">@канал или числовой id</label>
                <input type="text" id="settings-channel" name="channel" value="{_e(pub.channel)}" placeholder="@my_news_channel"></div>
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
            <div class="ai-options">{ai_options_html}</div>
            <div class="card-actions"><button class="primary" type="submit">Сохранить</button></div>
          </form>
        </div>
        </div>
        </div>

        <h2>Параметры публикации</h2>
        <div class="card">
          <form method="post" action="/settings/general">{csrf_field(request)}
            <div class="field-groups">{groups_html}</div>
            <h3 style="margin-top:14px;">Поведение</h3>
            <div class="toggle-grid">{toggles_html}</div>
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
        <h2>Согласование</h2>
        <div class="card">
          <div class="line">Сейчас: <span class="pill {'on' if pub.moderation else 'neutral'}">{'включено' if pub.moderation else 'выключено'}</span></div>
          <p class="muted">Готовые посты не публикуются сами — ждут одобрения в
            <a href="/queue">очереди</a>, кроме отложенных на конкретное время: те
            публикуются сами по расписанию, даже на паузе. {'В отладке не действует.' if pub.debug else ''}</p>
          <form method="post" action="/settings/moderation">{csrf_field(request)}
            <div class="card-actions">
              <button class="{'' if pub.moderation else 'primary'}" type="submit">
                {'Выключить' if pub.moderation else 'Включить согласование'}</button>
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
              <div style="flex:1;"><label for="settings-vk-group">id сообщества (числовой)</label>
                <input type="text" id="settings-vk-group" name="vk_group_id" value="{_e(st.get('vk_group_id'))}" placeholder="123456789"></div>
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

    # ключ → человекочитаемая подпись (GENERAL_GROUPS уже их содержит —
    # переиспользуем вместо голых snake_case-имён в сообщениях об ошибках).
    _FIELD_LABELS = {k: label for _, fields in GENERAL_GROUPS for k, label, _, _ in fields}

    def _ascii_digits(v: str) -> bool:
        # str.isdigit() пропускает не-ASCII цифры (напр. "٣٥", "²"), которые
        # int() не всегда разбирает так, как ожидает пользователь — тогда
        # панель бы сказала «сохранено» со значением, которое на деле не
        # действует (get_int молча падает в дефолт на не-ASCII вводе).
        return v.isascii() and v.isdigit()

    async def settings_general(request: web.Request) -> web.Response:
        st: Storage = app["st"]
        form = request["form"]
        # Всё, что ввёл админ в этой отправке — если валидация упадёт на
        # одном поле, форма перерисуется этим (а не старыми значениями из
        # БД) и правки во всех остальных полях не потеряются молча.
        prefill = {k: str(form.get(k, "")).strip() for k in SETTINGS_EDITABLE if k in form}
        # Сначала проверяем всё и только потом пишем — иначе при ошибке в
        # одном поле часть остальных уже была бы сохранена, а флеш говорит
        # "ничего не сохранено", что вводит в заблуждение.
        to_set: dict[str, str] = {}
        for k in SETTINGS_EDITABLE:
            if k not in form:
                continue
            v = str(form.get(k, "")).strip()
            label = _FIELD_LABELS.get(k, k)
            if k == "alert_thresholds":
                parts = [p for p in v.replace(" ", "").split(",") if p]
                if not parts or not all(_ascii_digits(p) and 1 <= int(p) <= 100 for p in parts):
                    return await settings_get(request, f"«{label}» — числа 1-100 через запятую.", "err",
                                              prefill=prefill)
                v = ",".join(str(int(p)) for p in sorted({int(p) for p in parts}))
            elif k == "max_images":
                if not _ascii_digits(v) or not (1 <= int(v) <= 10):
                    return await settings_get(request, "Картинок в альбом — число от 1 до 10.", "err",
                                              prefill=prefill)
            elif not _ascii_digits(v):
                return await settings_get(request, f"«{label}» должно быть числом.", "err", prefill=prefill)
            to_set[k] = v
        for k, v in to_set.items():
            st.set(k, v)
        for k in SETTINGS_TOGGLES:
            st.set(k, "1" if form.get(k) == "1" else "0")
        return await settings_get(request, "Настройки сохранены.")

    async def settings_debug(request: web.Request) -> web.Response:
        st: Storage = app["st"]
        st.set("debug", "0" if st.get("debug") == "1" else "1")
        return _redirect("/settings")

    async def settings_moderation(request: web.Request) -> web.Response:
        st: Storage = app["st"]
        turning_on = st.get("moderation") != "1"
        st.set("moderation", "1" if turning_on else "0")
        if turning_on:
            pending = st.count_moderation()
            flash = ("✅ Согласование включено. Новости больше не публикуются сами — "
                    "собираются в очереди («Согласование» в меню). Уже опубликованное "
                    "не трогается.")
            if pending:
                flash += f" В очереди уже есть {pending} карточек с прошлого раза."
        else:
            pending = st.count_moderation()
            flash = "✅ Согласование выключено, публикую в канал автоматически."
            if pending:
                flash += (f" В очереди осталось {pending} карточек — сами они не уйдут "
                         f"(кроме уже поставленных на конкретное время — те опубликуются "
                         f"по расписанию независимо от этой настройки), разберите "
                         f"остальное вручную («Согласование» в меню).")
        return await settings_get(request, flash)

    async def settings_vk(request: web.Request) -> web.Response:
        st: Storage = app["st"]
        form = request["form"]
        group = str(form.get("vk_group_id", "")).strip().lstrip("-")
        if group:
            if not _ascii_digits(group):
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
        pub: Publisher = app["publisher"]
        mode = str(form.get("mode", "normal"))
        # Проверяем ключ ДО записи в базу — иначе флаг claude_mode/gemini_mode
        # уже стоит "1", хотя карточка тут же говорит "переключение не
        # подействует": состояние в базе и то, что видно на экране, расходятся,
        # а как только ключ появится в .env, режим включится молча без ведома
        # админа (bump перезапуска подхватит уже сохранённый флаг).
        if mode == "claude" and not (pub.claude and pub.claude.api_key):
            return await settings_get(request, "Выбран Claude, но не хватает CLAUDE_API_KEY в .env — режим не сохранён.", "err")
        if mode == "gemini" and not (pub.gemini and pub.gemini.api_key):
            return await settings_get(request, "Выбран Gemini, но не хватает GEMINI_API_KEY в .env — режим не сохранён.", "err")
        st.set("claude_mode", "1" if mode == "claude" else "0")
        st.set("gemini_mode", "1" if mode == "gemini" else "0")
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
        body = (f"<h2 class='page-heading'>Последние посты "
               f"<span class='muted' style='font-weight:400;'>(до 30)</span></h2>"
               f"<div class='list'>{list_html}</div>")
        return web.Response(text=_layout("Посты", body, active="/posts"), content_type="text/html")

    async def post_detail(request: web.Request, draft: str | None = None,
                          flash: str = "", flash_kind: str = "ok") -> web.Response:
        st: Storage = app["st"]
        post_id = int(request.match_info["id"])
        row = st.post(post_id)
        if row is None:
            raise web.HTTPNotFound(text="Пост не найден")
        if draft is None:
            pending = app["regen_drafts"].get(post_id)
            if pending is not None:
                draft, flash, flash_kind = pending[0], "Черновик готов — не забудьте сохранить.", "ok"
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
                              onsubmit="return tgConfirmSubmit(this, 'Удалить картинку №{i} из поста?')">{csrf_field(request)}
                          <button class="icon" type="submit" title="Удалить картинку" aria-label="Удалить картинку">✕</button></form>
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
        app["regen_drafts"].pop(post_id, None)
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
        # Redirect (не рендерим черновик прямо в ответе на POST): обновление
        # страницы после POST иначе переотправило бы этот же запрос и снова
        # дёрнуло бы ИИ — см. app["regen_drafts"] выше.
        now = time.time()
        drafts = app["regen_drafts"]
        drafts[post_id] = (text, now)
        # Заодно чистим чужие устаревшие черновики — если админ ушёл со
        # страницы, не сохранив и не открыв её заново, drafts иначе рос бы
        # без ограничения годами работы бота (единственная другая точка
        # удаления — успешное /save, которое случается не всегда).
        stale = [pid for pid, (_, ts) in drafts.items() if now - ts > REGEN_DRAFT_TTL]
        for pid in stale:
            del drafts[pid]
        return _redirect(f"/posts/{post_id}")

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
        pub: Publisher = app["publisher"]
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
                      if row["image"] and _is_http_url(row["image"]) else "")
        publish_label = ("✅ Обработать (пойдёт на согласование)" if pub.moderation and not pub.debug
                         else "✅ Опубликовать всё же")
        debug_hint = ('<p class="field-hint" style="margin:6px 0 0; color:var(--red);">Включена '
                     'отладка (/debug) — публикация отсюда сейчас откажет, выключите отладку.</p>'
                     if pub.debug else "")
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
          {debug_hint}
          <div class="card-actions">
            <form method="post" action="/duplicates/{row['id']}/publish">{csrf_field(request)}
              <button class="primary" type="submit">{publish_label}</button></form>
            <form method="post" action="/duplicates/{row['id']}/delete"
                  onsubmit="return tgConfirmSubmit(this, 'Удалить из очереди? Новость останется неопубликованной.')">{csrf_field(request)}
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
        # В обеих ветках: успех publish_now — либо реальная публикация,
        # либо (при включённом согласовании) постановка в очередь на него,
        # но в обоих случаях эта запись здесь, в очереди дублей, больше не
        # актуальна — оставлять её значило бы предлагать разобрать ту же
        # новость ещё раз.
        st.delete_dedup_candidate(cid)
        # pub.debug тут гарантированно False: publish_now отказывает ошибкой
        # при включённой отладке ещё до публикации, и мы бы уже вернулись
        # выше по error.
        if pub.moderation and feed is not None:
            return await feeds_get(request, flash="Не дубль — поставлено на согласование "
                                                  "(«Согласование» в меню), а не сразу в канал: "
                                                  "включён режим согласования.")
        return await feeds_get(request, flash="Опубликовано.")

    async def duplicate_delete(request: web.Request) -> web.Response:
        st: Storage = app["st"]
        cid = int(request.match_info["id"])
        st.delete_dedup_candidate(cid)
        return await feeds_get(request, flash="Убрано из очереди.")

    # --- отложенные из-за отказа ИИ ----------------------------------------
    async def postponed_detail(request: web.Request) -> web.Response:
        st: Storage = app["st"]
        pub: Publisher = app["publisher"]
        pid = int(request.match_info["id"])
        row = st.postponed_item(pid)
        if row is None:
            raise web.HTTPNotFound(text="Запись не найдена — возможно, уже обработана")
        when_first = time.strftime("%d.%m %H:%M", time.localtime(row["first_failed_at"]))
        when_last = time.strftime("%d.%m %H:%M", time.localtime(row["last_failed_at"]))
        image_html = (f'<img src="{_safe_href(row["image"])}" alt="" '
                      f'style="max-width:100%; border-radius:10px; margin-top:10px;">'
                      if row["image"] and _is_http_url(row["image"]) else "")
        retry_hint = ('<p class="field-hint" style="margin:6px 0 0; color:var(--red);">Включена '
                     'отладка (/debug) — повтор отсюда сейчас откажет, выключите отладку.</p>'
                     if pub.debug else
                     '<p class="field-hint" style="margin:6px 0 0;">Включено согласование — если '
                     'модель ответит, новость уйдёт на согласование, а не сразу в канал.</p>'
                     if pub.moderation else "")
        body = f"""
        <div><a href="/feeds#postponed" class="back-link">‹ Ленты</a></div>
        <h2 class="page-heading after-back">Отложено #{row['id']}</h2>
        <div class="card">
          <div class="line"><b>{_e(row['title'])}</b></div>
          <div class="muted">{_e(row['feed_title'] or 'лента удалена')} ·
            <a href="{_safe_href(row['link'])}" target="_blank" rel="noopener">исходная новость</a></div>
          <div class="muted" style="margin-top:6px;">Первый отказ {when_first}, последний {when_last},
            попыток: {row['attempts']}</div>
          <div class="muted" style="color:var(--red); margin-top:6px;">{_e(row['error'])}</div>
          {image_html}
          <hr class="sep">
          <div class="field-hint" style="margin:0 0 6px;">Как есть в ленте, без обработки ИИ:</div>
          <pre class="post">{_e(row['summary'] or '(пусто)')}</pre>
          <div class="card-actions">
            <form method="post" action="/postponed/{row['id']}/retry">{csrf_field(request)}
              <button class="primary" type="submit">🔄 Повторить сейчас</button></form>
          </div>
          {retry_hint}
          <div class="card-actions" style="margin-top:0;">
            <form method="post" action="/postponed/{row['id']}/delete"
                  onsubmit="return tgConfirmSubmit(this, 'Отказаться от публикации? Новость больше не будет предложена.')">{csrf_field(request)}
              <button class="link-btn" type="submit">Не публиковать</button></form>
          </div>
        </div>
        """
        return web.Response(text=_layout(f"Отложено #{row['id']}", body, active="/feeds"), content_type="text/html")

    async def postponed_retry(request: web.Request) -> web.Response:
        st: Storage = app["st"]
        pub: Publisher = app["publisher"]
        pid = int(request.match_info["id"])
        row = st.postponed_item(pid)
        feed = st.feed(row["feed_id"]) if row is not None else None
        error = await pub.retry_postponed(pid)
        if error:
            return await feeds_get(request, flash=error, flash_kind="err")
        # pub.debug тут гарантированно False — см. комментарий в duplicate_publish.
        if pub.moderation and feed is not None:
            return await feeds_get(request, flash="Модель справилась — новость поставлена на "
                                                  "согласование («Согласование» в меню), а не сразу "
                                                  "в канал: включён режим согласования.")
        return await feeds_get(request, flash="Опубликовано.")

    async def postponed_delete(request: web.Request) -> web.Response:
        st: Storage = app["st"]
        pid = int(request.match_info["id"])
        row = st.postponed_item(pid)
        if row is not None:
            # Убираем и из очереди, и из «непрочитанного» — иначе запись
            # молча вернулась бы в канал на следующем автопроходе, хотя
            # админ только что явно сказал «не публиковать».
            st.mark_seen(row["feed_id"], row["key"])
            st.delete_postponed(pid)
        return await feeds_get(request, flash="Отклонено — публиковаться не будет.")

    # --- очередь ручного согласования (self.moderation) ---------------------
    QUEUE_PER_PAGE = 20

    def _queue_row_html(request: web.Request, r: sqlite3.Row, page: int) -> str:
        thumb = (f'<img class="dupe-thumb" src="{_safe_href(r["image"])}" alt="" '
                f'style="object-fit:cover;">'
                if r["image"] and _is_http_url(r["image"])
                else '<div class="dupe-thumb" style="background:var(--field-bg);"></div>')
        when = time.strftime("%d.%m %H:%M", time.localtime(r["queued_at"]))
        badges = ""
        if r["status"] == "publishing":
            badges += ' <span class="pill warn">публикуется…</span>'
        if r["error"]:
            badges += ' <span class="pill off">ошибка публикации</span>'
        if r["edited_at"]:
            badges += ' <span class="pill neutral">ред.</span>'
        if r["scheduled_at"]:
            sched = time.strftime("%d.%m %H:%M", time.localtime(r["scheduled_at"]))
            badges += f' <span class="pill" style="background:var(--purple-dim); color:var(--purple);">🕒 {sched}</span>'
        # Заголовок исходной новости из ленты — ориентир в списке; но
        # публикуется сгенерированный текст (r["text"]), который отличается
        # и без которого непонятно, что реально готово уйти в канал.
        preview = _e(strip_html(r["text"])[:100])
        page_field = f'<input type="hidden" name="page" value="{page}">' if page else ""
        # Открыть карточку можно тапом по всей строке — ссылка накрывает её
        # целиком (position:absolute поверх контента, см. .list-item.
        # actionable), а кнопки быстрых действий лежат выше по z-index и
        # получают клик первыми. Публикация/отклонение отсюда — без захода
        # в карточку; для отказа не спрашиваем подтверждение — есть «Отменить»
        # в флеше (см. queue_reject/queue_undo), а не для публикации — та
        # необратима, доверяем разовому tgConfirmSubmit.
        return f"""<div class="list-item actionable">
          <a href="/queue/{r['id']}?page={page}" class="list-item-cover" aria-label="Открыть #{r['id']}"></a>
          {thumb}
          <div class="list-item-info">
            <div class="list-item-title">{_e(r['title'][:140])}{badges}</div>
            <div class="muted">{_e(r['feed_title'] or 'лента удалена')} · {when}</div>
            <div class="muted" style="margin-top:2px;">{preview}</div>
          </div>
          <div class="list-item-actions">
            <form method="post" action="/queue/{r['id']}/publish"
                  onsubmit="return tgConfirmSubmit(this, 'Опубликовать в канал прямо сейчас?')">
              {csrf_field(request)}{page_field}
              <button class="icon" type="submit" title="Опубликовать" aria-label="Опубликовать"
                {'disabled' if r['status'] == 'publishing' else ''}>✅</button></form>
            <form method="post" action="/queue/{r['id']}/reject">{csrf_field(request)}{page_field}
              <button class="icon" type="submit" title="Отклонить" aria-label="Отклонить"
                {'disabled' if r['status'] == 'publishing' else ''}>🚫</button></form>
          </div>
        </div>"""

    def _undo_flash_action(request: web.Request, page: str = "") -> str:
        page_field = f'<input type="hidden" name="page" value="{_e(page)}">' if page else ""
        return (f'<form method="post" action="/queue/undo">{csrf_field(request)}{page_field}'
               f'<button type="submit" class="undo-btn">Отменить</button></form>')

    async def queue_get(request: web.Request, flash: str = "", flash_kind: str = "ok",
                        page_override: int | None = None, flash_action: str = "") -> web.Response:
        st: Storage = app["st"]
        pub: Publisher = app["publisher"]
        if page_override is not None:
            page = max(1, page_override)
        else:
            try:
                page = max(1, int(request.query.get("page", "1")))
            except ValueError:
                page = 1
        total = st.count_moderation()
        pages = max(1, -(-total // QUEUE_PER_PAGE))
        page = min(page, pages)
        rows = st.moderation_list(limit=QUEUE_PER_PAGE, offset=(page - 1) * QUEUE_PER_PAGE)
        items = "".join(_queue_row_html(request, r, page) for r in rows) if rows else (
            "<div style='padding:28px 16px; text-align:center;'>"
            "<div style='font-size:28px; margin-bottom:8px;'>🖐</div>"
            "<div class='muted'>Очередь пуста.</div></div>"
        )
        pager = ""
        if pages > 1:
            prev_html = (f'<a href="/queue?page={page - 1}" class="btn">‹ Назад</a>'
                        if page > 1 else '<span class="btn" style="visibility:hidden;">‹ Назад</span>')
            next_html = (f'<a href="/queue?page={page + 1}" class="btn">Вперёд ›</a>'
                        if page < pages else '<span class="btn" style="visibility:hidden;">Вперёд ›</span>')
            pager = (f'<div class="pager">{prev_html}'
                    f'<span class="muted">Стр. {page} из {pages}</span>{next_html}</div>')
        if not total:
            hint = ("" if pub.moderation else
                   "<div class='section-hint'>Режим выключен, новости публикуются сами — включить "
                   "можно в Настройках. Когда очередь появится, карточки будут ждать здесь.</div>")
        elif pub.moderation:
            hint = ("<div class='section-hint'>Новости не уходят в канал, пока вы их не одобрите "
                    "здесь — откройте карточку, чтобы отредактировать, опубликовать или отклонить.</div>")
        else:
            hint = ("<div class='section-hint'>Согласование сейчас выключено — новые новости "
                   "публикуются автоматически. Здесь то, что накопилось раньше — само не уйдёт, "
                   "разберите вручную или включите режим обратно в Настройках.</div>")
        body = f"""
        <h2 class="page-heading">Согласование <span class="muted" style="font-weight:400;">({total})</span></h2>
        {hint}
        <div class="list">{items}</div>
        {pager}
        """
        return web.Response(text=_layout("Согласование", body, flash, flash_kind, active="/queue",
                                         flash_action=flash_action),
                            content_type="text/html")

    async def queue_detail(request: web.Request, draft: str | None = None,
                           flash: str = "", flash_kind: str = "ok", flash_action: str = "",
                           page: str = "", item_id_override: int | None = None) -> web.Response:
        st: Storage = app["st"]
        # item_id_override — переход к следующей карточке сразу после
        # публикации/отклонения (см. queue_publish/queue_reject), без
        # промежуточного захода в список: id из URL был бы уже не той
        # карточки, которую нужно показать.
        item_id = item_id_override if item_id_override is not None else int(request.match_info["id"])
        row = st.moderation_item(item_id)
        if row is None:
            raise web.HTTPNotFound(text="Запись не найдена — возможно, уже обработана (опубликована, "
                                        "отклонена или её взял в работу другой администратор).")
        # ?page= — с какой страницы списка открыли карточку, чтобы кнопки
        # ниже (и «‹ Согласование») вернули туда же, а не всегда на первую.
        page = page or request.query.get("page", "")
        back_href = f"/queue?page={page}" if page else "/queue"
        page_field = f'<input type="hidden" name="page" value="{_e(page)}">' if page else ""
        if draft is None and request.query.get("regen") == "1":
            flash, flash_kind = "Черновик обновлён через ИИ.", "ok"
        text = draft if draft is not None else row["text"]
        draft_note = ('<p class="muted">⚠️ Черновик после перегенерации — ещё не сохранён отдельно, '
                     'но уже виден ниже; жмите «Опубликовать» или поправьте и сохраните.</p>'
                     if draft is not None else "")
        when = time.strftime("%d.%m %H:%M", time.localtime(row["queued_at"]))
        error_html = (f'<div class="muted" style="color:var(--red); margin-top:6px;">'
                     f'Не удалось опубликовать в прошлый раз: {_e(row["error"])}</div>'
                     if row["error"] else "")
        publishing = row["status"] == "publishing"
        status_html = ('<div class="muted" style="color:var(--amber); margin-top:6px;">'
                      '⏳ Уже публикуется — либо вы только что нажали «Опубликовать», либо это '
                      'делает другой администратор. Кнопки ниже разблокируются, если публикация '
                      'оборвалась и не завершилась за несколько минут.</div>' if publishing else "")
        disabled = "disabled" if publishing else ""
        scheduled_html = ""
        schedule_time_value = "12:00"
        if row["scheduled_at"]:
            schedule_time_value = time.strftime("%H:%M", time.localtime(row["scheduled_at"]))
            scheduled_html = (
                f'<div class="muted" style="color:var(--purple); margin-top:6px;">'
                f'🕒 Запланировано на {time.strftime("%d.%m %H:%M", time.localtime(row["scheduled_at"]))} '
                f'— опубликуется само.</div>')
        unschedule_form = (
            f'<form method="post" action="/queue/{row["id"]}/unschedule" style="margin-top:8px;">'
            f'{csrf_field(request)}{page_field}'
            f'<button type="submit" class="link-btn" {disabled}>Отменить план</button></form>'
            if row["scheduled_at"] else "")

        urls = [u for u in [row["image"], *row["extra_images"].split("\n")] if u]
        gallery = ""
        if len(urls) == 1 and _is_http_url(urls[0]):
            gallery = (f'<a href="{_safe_href(urls[0])}" target="_blank" rel="noopener">'
                      f'<img src="{_safe_href(urls[0])}" alt="" '
                      f'style="max-width:100%; border-radius:10px; margin-top:10px;"></a>')
        elif urls:
            thumbs = "".join(
                f'<a href="{_safe_href(u)}" target="_blank" rel="noopener">'
                f'<img src="{_safe_href(u)}" alt="" style="width:100%; border-radius:8px; '
                f'aspect-ratio:1; object-fit:cover;">{" <span class=\"pill neutral\">1-я, с подписью</span>" if i == 0 else ""}</a>'
                for i, u in enumerate(urls) if _is_http_url(u)
            )
            gallery = (f'<div style="display:grid; grid-template-columns:repeat(auto-fill,minmax(100px,1fr)); '
                      f'gap:8px; margin-top:10px;">{thumbs}</div>'
                      f'<div class="muted" style="margin-top:4px;">Альбом — уйдёт без подписи под каждой '
                      f'картинкой, текст отдельным сообщением следом.</div>' if len(urls) > 1 else "")

        caption_note = ("Альбом — картинки уйдут без общей подписи, текст отдельным сообщением следом."
                        if row["multi"] and len(urls) > 1 else
                        f"С картинкой и текстом длиннее {TG_CAPTION_LIMIT} — уйдёт текстом, "
                        f"картинка станет превью-ссылкой над ним.")

        body = f"""
        <div><a href="{back_href}" class="back-link">‹ Согласование</a></div>
        <h2 class="page-heading after-back">На согласовании #{row['id']}</h2>
        <div class="card">
          <div class="line"><b>{_e(row['title'])}</b></div>
          <div class="muted">{_e(row['feed_title'] or 'лента удалена')} · поставлено {when} ·
            <a href="{_safe_href(row['link'])}" target="_blank" rel="noopener">исходная новость</a></div>
          {error_html}
          {status_html}
          {scheduled_html}
          {gallery}
          <details style="margin-top:10px;">
            <summary class="disclosure">Как есть в ленте, без обработки ИИ</summary>
            <pre class="post">{_e(row['summary'] or '(пусто)')}</pre>
          </details>
          <hr class="sep">
          {draft_note}
          <form method="post" action="/queue/{row['id']}/publish" id="queue-publish-form">{csrf_field(request)}{page_field}
            <label>Текст поста — уйдёт в канал как есть</label>
            <textarea name="text" rows="{_rows_for(text, min_rows=8)}" maxlength="{TG_LIMIT}">{_e(text)}</textarea>
            <div class="muted" style="margin-top:4px;">Лимит: {TG_LIMIT} символов. {caption_note}</div>
            <div class="card-actions">
              <button class="primary" type="submit" {disabled}
                      onclick="return tgConfirmSubmit(this.form, 'Опубликовать в канал прямо сейчас?')">
                ✅ Опубликовать</button>
              <button type="submit" formaction="/queue/{row['id']}/save" {disabled}>💾 Сохранить черновик</button>
            </div>
          </form>
        </div>
        <div class="card">
          <form method="post" action="/queue/{row['id']}/regen?page={page}">{csrf_field(request)}
            <label>Перегенерировать через ИИ из исходной новости</label>
            <p class="field-hint" style="margin:0 0 8px;">Пожелание необязательно. Заменит текст выше —
              несохранённые правки в поле пропадут.</p>
            <div class="row">
              <input type="text" name="extra" placeholder="например: короче и без хештегов" style="flex:1;">
              <button type="submit" {disabled}>🤖 Перегенерировать</button>
            </div>
          </form>
        </div>
        <div class="card">
          <form method="post" action="/queue/{row['id']}/schedule">{csrf_field(request)}{page_field}
            <label>Отложить публикацию</label>
            <p class="field-hint" style="margin:0 0 8px;">Опубликуется само в выбранное время —
              возвращаться и нажимать «Опубликовать» вручную не нужно.</p>
            <div class="row">
              <select name="day">
                <option value="today">Сегодня</option>
                <option value="tomorrow">Завтра</option>
              </select>
              <input type="time" name="time" value="{schedule_time_value}" required style="flex:1;">
              <button type="submit" {disabled}>🕒 Запланировать</button>
            </div>
          </form>
          {unschedule_form}
        </div>
        <div class="card">
          <div class="card-actions">
            <form method="post" action="/queue/{row['id']}/preview">{csrf_field(request)}{page_field}
              <button type="submit">👁 Показать в личке</button></form>
            <form method="post" action="/queue/{row['id']}/reject">{csrf_field(request)}{page_field}
              <button class="link-btn" type="submit" {disabled}>🚫 Отклонить</button></form>
          </div>
        </div>
        <script>
        (function () {{
          // Родные кнопки Telegram (thumb zone внизу экрана, вне прокрутки) —
          // необязательное улучшение поверх уже рабочих кнопок на странице:
          // сама форма и её onclick/tgConfirmSubmit остаются рабочим
          // способом опубликовать, если у клиента нет MainButton/BackButton
          // или сеть/версия не подтянули API. Всё в try/catch — не должно
          // ронять страницу, если что-то из этого недоступно.
          var tg = window.Telegram && window.Telegram.WebApp;
          if (!tg) return;
          var publishing = {json.dumps(publishing)};
          try {{
            if (tg.BackButton) {{
              tg.BackButton.show();
              tg.BackButton.onClick(function () {{ location.href = {json.dumps(back_href)}; }});
            }}
          }} catch (e) {{}}
          var form = document.getElementById('queue-publish-form');
          if (!publishing && tg.MainButton && form) {{
            try {{
              tg.MainButton.setText('✅ Опубликовать');
              tg.MainButton.show();
              tg.MainButton.onClick(function () {{
                if (tg.HapticFeedback) {{ try {{ tg.HapticFeedback.notificationOccurred('success'); }} catch (e) {{}} }}
                if (window.tgConfirmSubmit) {{ tgConfirmSubmit(form, 'Опубликовать в канал прямо сейчас?'); }}
                else {{ form.submit(); }}
              }});
            }} catch (e) {{}}
          }}
        }})();
        </script>
        """
        return web.Response(text=_layout(f"Согласование #{row['id']}", body, flash, flash_kind, active="/queue",
                                         flash_action=flash_action),
                            content_type="text/html")

    def _form_page(request: web.Request) -> str:
        """Номер страницы списка, с которой открыли карточку — только цифры,
        чтобы не тащить произвольный текст в query string."""
        v = str(request["form"].get("page", "")).strip()
        return v if v.isdigit() else ""

    def _refuse_if_publishing(row: sqlite3.Row) -> str | None:
        """claim_moderation переводит карточку в status='publishing' на время
        publish_moderated (ручной клик или расписание) — claim там атомарный
        (UPDATE ... WHERE status='queued' ...), но эта проверка на других
        действиях (отклонить/сохранить/перегенерировать/перепланировать)
        отсутствовала: правило «карточку, которую сейчас публикуют, трогать
        нельзя» было реализовано только для повторной публикации. Без него
        админ мог отклонить карточку ровно в момент, когда run_scheduled_
        publishes или другой админ её уже отправляет в канал — в панели
        было бы «Отклонено», а в канале реальный пост, и «Отменить» после
        такого отклонения создал бы в канале дубль."""
        if row["status"] == "publishing":
            return "Карточка сейчас публикуется — подождите несколько секунд и обновите страницу."
        return None

    async def queue_save(request: web.Request) -> web.Response:
        st: Storage = app["st"]
        item_id = int(request.match_info["id"])
        row = st.moderation_item(item_id)
        if row is None:
            raise web.HTTPNotFound(text="Запись не найдена — возможно, уже обработана")
        page = _form_page(request)
        guard = _refuse_if_publishing(row)
        if guard:
            return await queue_detail(request, flash=guard, flash_kind="err", page=page)
        text = str(request["form"].get("text", "")).strip()
        if not text:
            return await queue_detail(request, flash="Пустой текст не сохранён.", flash_kind="err", page=page)
        if tg_len(text) > TG_LIMIT:
            return await queue_detail(request, draft=text, page=page,
                                      flash=f"Текст длиннее лимита ({tg_len(text)} из {TG_LIMIT}) — не сохранено.",
                                      flash_kind="err")
        problem = html_problem(text)
        if problem:
            return await queue_detail(request, draft=text, page=page,
                                      flash=f"Разметка не годится: {problem}", flash_kind="err")
        st.update_moderation_text(item_id, text)
        return await queue_detail(request, flash="Сохранено.", page=page)

    async def queue_publish(request: web.Request) -> web.Response:
        st: Storage = app["st"]
        pub: Publisher = app["publisher"]
        item_id = int(request.match_info["id"])
        row = st.moderation_item(item_id)
        if row is None:
            raise web.HTTPNotFound(text="Запись не найдена — возможно, уже обработана")
        page = _form_page(request)
        # Публикуем ровно то, что сейчас в поле формы, а не то, что лежало в
        # БД до открытия страницы — иначе несохранённая правка молча
        # терялась бы при нажатии «Опубликовать» (пост ушёл бы старым текстом).
        # Из строки списка text не передаётся вовсе (там нет textarea) —
        # это ожидаемо, публикуется как есть сохранённый в карточке текст.
        text = str(request["form"].get("text", "")).strip()
        if text and text != row["text"]:
            if tg_len(text) > TG_LIMIT:
                return await queue_detail(request, draft=text, page=page,
                                          flash=f"Текст длиннее лимита ({tg_len(text)} из {TG_LIMIT}) — "
                                               f"не опубликовано.", flash_kind="err")
            problem = html_problem(text)
            if problem:
                return await queue_detail(request, draft=text, page=page,
                                          flash=f"Разметка не годится: {problem}", flash_kind="err")
            st.update_moderation_text(item_id, text)
        # До публикации — после неё строки уже не будет, искать в ней «следующую» поздно.
        next_id = st.moderation_neighbor(item_id)
        error = await pub.publish_moderated(item_id)
        if error:
            return await queue_detail(request, flash=error, flash_kind="err", page=page)
        if next_id is not None:
            return await queue_detail(request, item_id_override=next_id, page=page,
                                      flash="Опубликовано. Следующая карточка:")
        return await queue_get(request, flash="Опубликовано — очередь разобрана.",
                               page_override=int(page) if page else None)

    async def queue_reject(request: web.Request) -> web.Response:
        st: Storage = app["st"]
        item_id = int(request.match_info["id"])
        page = _form_page(request)
        row = st.moderation_item(item_id)
        if row is not None:
            guard = _refuse_if_publishing(row)
            if guard:
                return await queue_detail(request, flash=guard, flash_kind="err", page=page)
        next_id = st.moderation_neighbor(item_id) if row is not None else None
        if row is not None:
            # На отмену — «Отменить» во флеше вместо confirm()-диалога на
            # каждое отклонение (см. queue_undo). В памяти, не персистентно:
            # окно всего UNDO_TTL секунд, переживать рестарт процесса ему
            # незачем. Раньше был один слот на всю панель — «Отменить» у
            # одного админа могло тихо вернуть отклонение ДРУГОГО админа
            # (два человека работают с очередью параллельно). Стэш по
            # токену сессии — свой слот каждому залогиненному, без общей
            # системы личностей (см. WebAuth: пароль один на всех, разбирать
            # пользователей никогда не требовалось ни для чего другого).
            token = request.cookies.get(SESSION_COOKIE)
            if token:
                stash: dict[str, tuple[dict, float]] = app["undo_stash"]
                now = time.time()
                for k in [k for k, (_, ts) in stash.items() if now - ts > UNDO_TTL]:
                    stash.pop(k, None)
                stash[token] = (dict(row), now)
            st.delete_moderation(item_id)
        if next_id is not None:
            return await queue_detail(request, item_id_override=next_id, page=page,
                                      flash="Отклонено.", flash_action=_undo_flash_action(request, page))
        return await queue_get(request, flash="Отклонено — очередь разобрана.",
                               page_override=int(page) if page else None,
                               flash_action=_undo_flash_action(request, page))

    async def queue_regen(request: web.Request) -> web.Response:
        st: Storage = app["st"]
        pub: Publisher = app["publisher"]
        item_id = int(request.match_info["id"])
        page = request.query.get("page", "")
        row = st.moderation_item(item_id)
        if row is not None:
            guard = _refuse_if_publishing(row)
            if guard:
                return await queue_detail(request, flash=guard, flash_kind="err", page=page)
        extra = str(request["form"].get("extra", "")).strip()
        error = await pub.regen_moderated(item_id, extra)
        if error:
            return await queue_detail(request, flash=error, flash_kind="err", page=page)
        # Redirect, а не прямой рендер: обновление страницы после POST иначе
        # переотправило бы этот же запрос и снова дёрнуло бы платную ИИ-модель
        # (текст уже сохранён в карточке — черновик-в-памяти тут не нужен,
        # в отличие от /posts/{id}/regen, см. Publisher.regen_moderated).
        suffix = f"&page={page}" if page else ""
        return _redirect(f"/queue/{item_id}?regen=1{suffix}")

    def _parse_hhmm(value: str) -> tuple[int, int] | None:
        parts = value.strip().split(":")
        if len(parts) != 2:
            return None
        try:
            hour, minute = int(parts[0]), int(parts[1])
        except ValueError:
            return None
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return hour, minute

    async def queue_schedule(request: web.Request) -> web.Response:
        st: Storage = app["st"]
        item_id = int(request.match_info["id"])
        row = st.moderation_item(item_id)
        if row is None:
            raise web.HTTPNotFound(text="Запись не найдена — возможно, уже обработана")
        page = _form_page(request)
        guard = _refuse_if_publishing(row)
        if guard:
            return await queue_detail(request, flash=guard, flash_kind="err", page=page)
        hhmm = _parse_hhmm(str(request["form"].get("time", "")))
        if hhmm is None:
            return await queue_detail(request, flash="Укажите время в формате ЧЧ:ММ.",
                                      flash_kind="err", page=page)
        hour, minute = hhmm
        day = str(request["form"].get("day", "today"))
        # Локальное время сервера — тем же временем везде в панели подписаны
        # даты постов/лент (time.localtime), отдельного часового пояса для
        # планирования заводить незачем, лишняя настройка ради путаницы.
        target_date = datetime.now().date()
        if day == "tomorrow":
            target_date += timedelta(days=1)
        target_dt = datetime.combine(target_date, datetime.min.time()).replace(hour=hour, minute=minute)
        scheduled_ts = int(target_dt.timestamp())
        if scheduled_ts <= int(time.time()):
            return await queue_detail(request, page=page, flash_kind="err",
                                      flash="Это время уже прошло — выберите время позже или «Завтра».")
        st.schedule_moderation(item_id, scheduled_ts)
        return await queue_detail(request, page=page,
                                  flash=f"Запланировано на {target_dt.strftime('%d.%m %H:%M')} — "
                                       f"опубликуется само, без напоминаний.")

    async def queue_unschedule(request: web.Request) -> web.Response:
        st: Storage = app["st"]
        item_id = int(request.match_info["id"])
        page = _form_page(request)
        row = st.moderation_item(item_id)
        if row is not None:
            guard = _refuse_if_publishing(row)
            if guard:
                return await queue_detail(request, flash=guard, flash_kind="err", page=page)
        st.unschedule_moderation(item_id)
        return await queue_detail(request, flash="План снят — карточка снова ждёт ручного решения.", page=page)

    async def queue_preview(request: web.Request) -> web.Response:
        pub: Publisher = app["publisher"]
        item_id = int(request.match_info["id"])
        page = _form_page(request)
        error = await pub.preview_moderated(item_id)
        if error:
            return await queue_detail(request, flash=error, flash_kind="err", page=page)
        return await queue_detail(request, flash="Отправлено в личку — если панель открыта как Mini App, "
                                                  "сверните её, чтобы увидеть сообщение.", page=page)

    async def queue_undo(request: web.Request) -> web.Response:
        """Возвращает последнюю отклонённую карточку — см. queue_reject.
        Работает только в окне UNDO_TTL и только для самого недавнего
        отклонения ЭТОЙ ЖЕ сессии; более раннее и чужие уже не отменить."""
        st: Storage = app["st"]
        token = request.cookies.get(SESSION_COOKIE)
        stash = app["undo_stash"].pop(token, None) if token else None
        page = _form_page(request)
        if stash is None:
            return await queue_get(request, flash="Отменять уже нечего.", flash_kind="err",
                                   page_override=int(page) if page else None)
        row, stashed_at = stash
        if time.time() - stashed_at > UNDO_TTL:
            return await queue_get(request, flash="Слишком поздно — карточка уже удалена насовсем.",
                                   flash_kind="err", page_override=int(page) if page else None)
        # Если карточка была отклонена уже ПОСЛЕ запланированного времени
        # публикации (или отмена случилась настолько близко к границе TTL,
        # что план успел протухнуть) — восстанавливать с этим scheduled_at
        # нельзя: run_scheduled_publishes на следующем же проходе опубликует
        # её немедленно, без единого шанса на повторный ручной разбор,
        # хотя admin ожидает увидеть карточку снова в обычной очереди.
        was_overdue = bool(row.get("scheduled_at")) and row["scheduled_at"] <= int(time.time())
        if was_overdue:
            row = dict(row)
            row["scheduled_at"] = None
        st.restore_moderation(row)
        flash = "Восстановлено."
        if was_overdue:
            flash += " План публикации снят — время уже прошло, требуется новое решение."
        return await queue_detail(request, item_id_override=row["id"], page=page, flash=flash)

    app.router.add_get("/login", login_get)
    app.router.add_post("/login", login_post)
    app.router.add_post("/tg-login", tg_login_post)
    app.router.add_post("/logout", logout_post)
    app.router.add_get("/", dashboard)
    app.router.add_post("/pause", pause_post)
    app.router.add_post("/checknow", checknow_post)
    app.router.add_get("/feeds", feeds_get)
    app.router.add_post("/feeds/add", feeds_add)
    app.router.add_post("/feeds/add-search", feeds_add_search)
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
    app.router.add_post("/settings/moderation", settings_moderation)
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
    app.router.add_get("/postponed/{id}", postponed_detail)
    app.router.add_post("/postponed/{id}/retry", postponed_retry)
    app.router.add_post("/postponed/{id}/delete", postponed_delete)
    app.router.add_get("/queue", queue_get)
    app.router.add_get("/queue/{id}", queue_detail)
    app.router.add_post("/queue/{id}/save", queue_save)
    app.router.add_post("/queue/{id}/publish", queue_publish)
    app.router.add_post("/queue/{id}/reject", queue_reject)
    app.router.add_post("/queue/{id}/regen", queue_regen)
    app.router.add_post("/queue/{id}/schedule", queue_schedule)
    app.router.add_post("/queue/{id}/unschedule", queue_unschedule)
    app.router.add_post("/queue/{id}/preview", queue_preview)
    app.router.add_post("/queue/undo", queue_undo)

    return app


async def run_web_panel(storage: Storage, publisher: Publisher, bot: Bot,
                        password: str, port: int, host: str = "0.0.0.0",
                        admin_ids: set[int] | None = None, secure_cookies: bool = False
                        ) -> tuple[web.AppRunner, web.TCPSite]:
    app = create_app(storage, publisher, bot, password, admin_ids=admin_ids,
                     secure_cookies=secure_cookies)
    runner = web.AppRunner(app, access_log=log)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    return runner, site
