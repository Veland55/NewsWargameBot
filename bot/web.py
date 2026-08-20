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

import html as html_mod
import logging
import secrets
import time
from typing import Awaitable, Callable

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import LinkPreviewOptions
from aiohttp import web

from .db import DEFAULTS, Storage
from .llm import LLMError
from .publisher import (TG_CAPTION_LIMIT, TG_LIMIT, Publisher, html_problem,
                        tg_len)
from .quota import until_reset
from .rss import fetch

log = logging.getLogger(__name__)

SESSION_COOKIE = "bot_session"
SESSION_TTL = 7 * 24 * 3600      # неделя — снова логиниться каждый день утомительно
LOGIN_MAX_FAILS = 5              # неудачных попыток с одного адреса
LOGIN_LOCKOUT = 15 * 60          # прежде чем снова можно пробовать

SETTINGS_EDITABLE = (
    "interval", "max_per_cycle", "post_delay", "backfill",
    "max_age_days", "flood_guard", "keep_seen",
    "alert_thresholds", "free_daily_limit", "claude_max_images",
)
SETTINGS_TOGGLES = ("require_russian", "disable_preview", "images", "og_image")


def _e(text: object) -> str:
    return html_mod.escape(str(text if text is not None else ""))


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
STYLE = """
:root { color-scheme: dark; --tg-top: 0px; --tg-bottom: 0px; }
* { box-sizing: border-box; }
body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; background: #14151a;
       color: #e6e6e6; margin: 0; padding: 0 0 calc(40px + var(--tg-bottom)); }
header { background: #1c1e26; padding: calc(14px + var(--tg-top)) 20px 14px; border-bottom: 1px solid #2c2f3a;
         display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px; }
header h1 { font-size: 17px; margin: 0; }
nav a { color: #9ecbff; text-decoration: none; margin-right: 16px; font-size: 14px; }
nav a:hover { text-decoration: underline; }
main { max-width: 880px; margin: 24px auto; padding: 0 20px; }
h2 { font-size: 16px; color: #ffd76a; border-bottom: 1px solid #2c2f3a; padding-bottom: 8px; }
.card { background: #1c1e26; border: 1px solid #2c2f3a; border-radius: 10px;
        padding: 16px 18px; margin-bottom: 18px; }
.row { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-bottom: 8px; }
label { display: block; font-size: 12.5px; color: #9a9ea8; text-transform: uppercase;
        letter-spacing: .4px; margin-bottom: 4px; }
input[type=text], input[type=password], input[type=number], textarea, select {
  background: #111216; color: #e6e6e6; border: 1px solid #33374a; border-radius: 7px;
  padding: 8px 10px; font-size: 14px; width: 100%; font-family: inherit;
}
textarea { min-height: 120px; resize: vertical; font-family: ui-monospace, monospace; }
button, .btn { background: #33374a; color: #e6e6e6; border: 1px solid #454a63;
       border-radius: 7px; padding: 8px 14px; font-size: 13.5px; cursor: pointer; }
button:hover, .btn:hover { background: #454a63; }
button.primary { background: #3a63c8; border-color: #3a63c8; }
button.primary:hover { background: #4b74d6; }
button.danger { background: #7a2c2c; border-color: #7a2c2c; }
button.danger:hover { background: #932f2f; }
.pill { display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: 12px; }
.pill.on { background: #1f4a2c; color: #7be79b; }
.pill.off { background: #4a2020; color: #ff9d9d; }
.flash { padding: 10px 14px; border-radius: 8px; margin-bottom: 16px; font-size: 14px; }
.flash.ok { background: #1f4a2c; color: #b7f3c6; }
.flash.err { background: #4a2020; color: #ffbcbc; }
table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
td, th { text-align: left; padding: 7px 6px; border-bottom: 1px solid #262838; vertical-align: top; }
.muted { color: #85899a; font-size: 12.5px; }
.mono { font-family: ui-monospace, monospace; font-size: 12.5px; word-break: break-all; }
pre.post { white-space: pre-wrap; background: #111216; border: 1px solid #2c2f3a;
           border-radius: 8px; padding: 10px 12px; font-size: 13px; }
form.inline { display: inline; }
"""


# Открыто как Telegram Mini App (кнопка /panel или меню бота) — SDK молча
# ни на что не влияет вне Telegram. ready()/expand() разворачивают на весь
# экран, а --tg-top/--tg-bottom дают шапке отступ от родного заголовка
# Telegram в полноэкранном режиме (см. STYLE выше).
TG_INIT_SCRIPT = """<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script>
(function () {
  var tg = window.Telegram && window.Telegram.WebApp;
  if (!tg) return;
  try { tg.ready(); tg.expand(); } catch (e) {}
  function applyInsets() {
    var sa = tg.safeAreaInset || {}, csa = tg.contentSafeAreaInset || {};
    document.documentElement.style.setProperty('--tg-top', ((sa.top||0)+(csa.top||0)) + 'px');
    document.documentElement.style.setProperty('--tg-bottom', ((sa.bottom||0)+(csa.bottom||0)) + 'px');
  }
  applyInsets();
  if (tg.onEvent) { tg.onEvent('safeAreaChanged', applyInsets); tg.onEvent('contentSafeAreaChanged', applyInsets); }
})();
</script>"""


def _layout(title: str, body: str, flash: str = "", flash_kind: str = "ok") -> str:
    flash_html = f'<div class="flash {flash_kind}">{flash}</div>' if flash else ""
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)} — bot panel</title>
{TG_INIT_SCRIPT}
<style>{STYLE}</style></head><body>
<header>
  <h1>📰 RSS → канал</h1>
  <nav>
    <a href="/">Статус</a>
    <a href="/feeds">Ленты</a>
    <a href="/template">Шаблон</a>
    <a href="/format">Формат</a>
    <a href="/settings">Настройки</a>
    <a href="/posts">Посты</a>
    <a href="/usage">Расход</a>
    <form class="inline" method="post" action="/logout"><button>Выйти</button></form>
  </nav>
</header>
<main>{flash_html}{body}</main>
</body></html>"""


def _login_page(error: str = "") -> str:
    err_html = f'<div class="flash err">{_e(error)}</div>' if error else ""
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Вход — bot panel</title>
{TG_INIT_SCRIPT}
<style>{STYLE}</style></head><body>
<main style="max-width:360px; margin-top:80px;">
  <h2>Вход в панель</h2>
  {err_html}
  <div class="card">
    <form method="post" action="/login">
      <label>Пароль</label>
      <input type="password" name="password" autofocus required>
      <div style="margin-top:12px;"><button class="primary" type="submit">Войти</button></div>
    </form>
  </div>
</main></body></html>"""


def _redirect(path: str) -> web.HTTPFound:
    return web.HTTPFound(path)


# ======================== приложение ========================
def create_app(storage: Storage, publisher: Publisher, bot: Bot, password: str) -> web.Application:
    auth = WebAuth(password)
    app = web.Application()
    app["auth"] = auth
    app["st"] = storage
    app["publisher"] = publisher
    app["bot"] = bot

    PUBLIC_PATHS = {"/login"}

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

    # --- статус -------------------------------------------------------------
    async def dashboard(request: web.Request) -> web.Response:
        st: Storage = app["st"]
        pub: Publisher = app["publisher"]
        feeds = st.feeds()
        active = sum(1 for f in feeds if f["enabled"])
        errors = [f for f in feeds if f["last_error"]]
        mode = "⏸ на паузе" if st.get("paused") == "1" else "▶️ работает"
        if pub.debug:
            mode = "🔧 отладка"
        body = f"""
        <h2>Состояние</h2>
        <div class="card">
          <div class="row">Публикация: <b>{_e(mode)}</b></div>
          <div class="row">Канал: <span class="mono">{_e(pub.channel or 'не задан')}</span></div>
          <div class="row">Лент: {active} активных из {len(feeds)}
            {f', с ошибками: {len(errors)}' if errors else ''}</div>
          <div class="row">Модель LLM: <span class="mono">{_e(pub.llm.model)}</span>
            <span class="pill {'on' if pub.llm.api_key else 'off'}">{'ключ задан' if pub.llm.api_key else 'нет ключа'}</span></div>
          <div class="row">Claude: <span class="pill {'on' if pub.claude_mode else 'off'}">
            {'включён, ' + _e(pub.claude.model) if pub.claude_mode else 'выключен'}</span></div>
          <div class="row">VK: <span class="pill {'on' if pub.vk_on else 'off'}">
            {'сообщество ' + _e(pub.vk_group) if pub.vk_on else 'выключен'}</span></div>
        </div>
        <div class="row">
          <form method="post" action="/pause"><input type="hidden" name="csrf" value="{_e(request['csrf'])}">
            <button class="{'primary' if st.get('paused')=='1' else 'danger'}" type="submit">
              {'▶️ Возобновить публикацию' if st.get('paused')=='1' else '⏸ Приостановить публикацию'}
            </button></form>
          <form method="post" action="/checknow"><input type="hidden" name="csrf" value="{_e(request['csrf'])}">
            <button type="submit">🔄 Проверить ленты сейчас</button></form>
        </div>
        """
        return web.Response(text=_layout("Статус", body), content_type="text/html")

    async def pause_post(request: web.Request) -> web.Response:
        st: Storage = app["st"]
        st.set("paused", "0" if st.get("paused") == "1" else "1")
        return _redirect("/")

    async def checknow_post(request: web.Request) -> web.Response:
        pub: Publisher = app["publisher"]
        pub.wake()
        return _redirect("/")

    # --- ленты ---------------------------------------------------------------
    async def feeds_get(request: web.Request, flash: str = "", flash_kind: str = "ok") -> web.Response:
        st: Storage = app["st"]
        rows = st.feeds()
        items = ""
        for f in rows:
            checked = time.strftime("%d.%m %H:%M", time.localtime(f["last_check"])) if f["last_check"] else "—"
            err = f'<div class="muted" style="color:#ff9d9d">{_e(f["last_error"][:150])}</div>' if f["last_error"] else ""
            items += f"""<tr>
              <td>#{f['id']}<br><span class="pill {'on' if f['enabled'] else 'off'}">{'вкл' if f['enabled'] else 'пауза'}</span></td>
              <td>{_e(f['title'] or '(без названия)')}<br><span class="mono muted">{_e(f['url'])}</span>{err}</td>
              <td class="muted">{checked}<br>в архиве: {st.seen_count(f['id'])}</td>
              <td>
                <form class="inline" method="post" action="/feeds/{f['id']}/toggle">{csrf_field(request)}
                  <button type="submit">{'⏸' if f['enabled'] else '▶️'}</button></form>
                <form class="inline" method="post" action="/feeds/{f['id']}/delete"
                      onsubmit="return confirm('Удалить ленту #{f['id']}?')">{csrf_field(request)}
                  <button class="danger" type="submit">✕</button></form>
              </td>
            </tr>"""
        table = (f"<table><tr><th>id</th><th>Лента</th><th>Проверена</th><th></th></tr>{items}</table>"
                if rows else "<p class='muted'>Лент пока нет.</p>")
        body = f"""
        <h2>Добавить ленту</h2>
        <div class="card">
          <form method="post" action="/feeds/add">{csrf_field(request)}
            <div class="row" style="align-items:flex-end;">
              <div style="flex:2;"><label>URL ленты</label><input type="text" name="url" placeholder="https://example.com/rss" required></div>
              <div style="flex:1;"><label>Название (необязательно)</label><input type="text" name="title"></div>
              <button class="primary" type="submit">Добавить</button>
            </div>
          </form>
        </div>
        <h2>Ленты</h2>
        <div class="card">{table}</div>
        """
        return web.Response(text=_layout("Ленты", body, flash, flash_kind), content_type="text/html")

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

    # --- шаблон / формат -----------------------------------------------------
    async def template_get(request: web.Request, flash: str = "", flash_kind: str = "ok") -> web.Response:
        st: Storage = app["st"]
        body = f"""
        <h2>Промпт для модели</h2>
        <p class="muted">Плейсхолдеры: <code>{{title}}</code> <code>{{summary}}</code>
          <code>{{link}}</code> <code>{{source}}</code> <code>{{published}}</code></p>
        <div class="card">
          <form method="post" action="/template">{csrf_field(request)}
            <textarea name="text" rows="14">{_e(st.get('template'))}</textarea>
            <div class="row" style="margin-top:10px;">
              <button class="primary" type="submit">Сохранить</button>
              <button type="submit" form="reset-template">Сбросить к умолчанию</button>
            </div>
          </form>
          <form id="reset-template" method="post" action="/template/reset">{csrf_field(request)}</form>
        </div>
        """
        return web.Response(text=_layout("Шаблон", body, flash, flash_kind), content_type="text/html")

    async def template_post(request: web.Request) -> web.Response:
        text = str(request["form"].get("text", "")).strip()
        if "{summary}" not in text and "{title}" not in text:
            return await template_get(request, "В промпте нет ни {title}, ни {summary} — не сохранено.", "err")
        app["st"].set("template", text)
        return await template_get(request, "Промпт сохранён.")

    async def template_reset(request: web.Request) -> web.Response:
        app["st"].set("template", DEFAULTS["template"])
        return _redirect("/template")

    async def format_get(request: web.Request, flash: str = "", flash_kind: str = "ok") -> web.Response:
        st: Storage = app["st"]
        body = f"""
        <h2>Формат поста</h2>
        <p class="muted">Плюс <code>{{ai}}</code> — ответ модели. Поддерживаются HTML-теги Telegram:
          b i u s code pre a blockquote.</p>
        <div class="card">
          <form method="post" action="/format">{csrf_field(request)}
            <textarea name="text" rows="8">{_e(st.get('post_format'))}</textarea>
            <div class="row" style="margin-top:10px;">
              <button class="primary" type="submit">Сохранить</button>
              <button type="submit" form="reset-format">Сбросить к умолчанию</button>
            </div>
          </form>
          <form id="reset-format" method="post" action="/format/reset">{csrf_field(request)}</form>
        </div>
        """
        return web.Response(text=_layout("Формат", body, flash, flash_kind), content_type="text/html")

    async def format_post(request: web.Request) -> web.Response:
        text = str(request["form"].get("text", "")).strip()
        if "{ai}" not in text:
            return await format_get(request, "Без {ai} в посте не будет текста от модели — не сохранено.", "err")
        problem = html_problem(text)
        if problem:
            return await format_get(request, f"Разметка не годится: {problem}", "err")
        app["st"].set("post_format", text)
        return await format_get(request, "Формат сохранён.")

    async def format_reset(request: web.Request) -> web.Response:
        app["st"].set("post_format", DEFAULTS["post_format"])
        return _redirect("/format")

    # --- настройки -------------------------------------------------------
    async def settings_get(request: web.Request, flash: str = "", flash_kind: str = "ok") -> web.Response:
        st: Storage = app["st"]
        pub: Publisher = app["publisher"]
        num_fields = "".join(
            f'<div style="flex:1; min-width:140px;"><label>{_e(k)}</label>'
            f'<input type="text" name="{_e(k)}" value="{_e(st.get(k))}"></div>'
            for k in SETTINGS_EDITABLE
        )
        toggle_fields = "".join(
            f'<label style="display:flex; align-items:center; gap:6px; text-transform:none;">'
            f'<input type="checkbox" name="{_e(k)}" value="1" {"checked" if st.get(k)=="1" else ""}> {_e(k)}</label>'
            for k in SETTINGS_TOGGLES
        )
        body = f"""
        <h2>Канал</h2>
        <div class="card">
          <form method="post" action="/settings/channel">{csrf_field(request)}
            <div class="row" style="align-items:flex-end;">
              <div style="flex:1;"><label>@канал или числовой id</label>
                <input type="text" name="channel" value="{_e(pub.channel)}" placeholder="@my_news_channel"></div>
              <button class="primary" type="submit">Сохранить</button>
            </div>
          </form>
        </div>

        <h2>Параметры публикации</h2>
        <div class="card">
          <form method="post" action="/settings/general">{csrf_field(request)}
            <div class="row">{num_fields}</div>
            <div class="row" style="margin-top:6px;">{toggle_fields}</div>
            <div style="margin-top:12px;"><button class="primary" type="submit">Сохранить</button></div>
          </form>
        </div>

        <h2>Отладка</h2>
        <div class="card">
          <p class="muted">Посты уходят в личку админам вместо канала, автоцикл в отладке молчит.</p>
          <form method="post" action="/settings/debug">{csrf_field(request)}
            <button class="{'primary' if pub.debug else ''}" type="submit">
              {'✅ Отладка включена — выключить' if pub.debug else '🔧 Включить отладку'}</button>
          </form>
        </div>

        <h2>VK</h2>
        <div class="card">
          <div class="row">Сейчас: <span class="pill {'on' if pub.vk_on else 'off'}">
            {'включено, сообщество ' + _e(pub.vk_group) if pub.vk_on else 'выключено'}</span>
            {'· ключ не задан (VK_TOKEN в .env)' if not (pub.vk and pub.vk.token) else ''}</div>
          <form method="post" action="/settings/vk">{csrf_field(request)}
            <div class="row" style="align-items:flex-end;">
              <div style="flex:1;"><label>id сообщества (числовой)</label>
                <input type="text" name="vk_group_id" value="{_e(st.get('vk_group_id'))}" placeholder="123456789"></div>
              <button type="submit" name="action" value="on" class="primary">Включить</button>
              <button type="submit" name="action" value="off">Выключить</button>
            </div>
          </form>
        </div>

        <h2>Claude</h2>
        <div class="card">
          <div class="row">Сейчас: <span class="pill {'on' if pub.claude_mode else 'off'}">
            {'включён, ' + _e(pub.claude.model) if pub.claude_mode else 'выключен'}</span>
            {'· CLAUDE_API_KEY не задан в .env' if not (pub.claude and pub.claude.api_key) else ''}</div>
          <form method="post" action="/settings/claude">{csrf_field(request)}
            <div class="row" style="align-items:flex-end;">
              <div style="flex:1;"><label>Картинок в альбом (1-10)</label>
                <input type="text" name="claude_max_images" value="{_e(st.get('claude_max_images'))}"></div>
              <button type="submit" name="action" value="on" class="primary">Включить</button>
              <button type="submit" name="action" value="off">Выключить</button>
            </div>
          </form>
        </div>
        """
        return web.Response(text=_layout("Настройки", body, flash, flash_kind), content_type="text/html")

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

    async def settings_claude(request: web.Request) -> web.Response:
        st: Storage = app["st"]
        form = request["form"]
        n = str(form.get("claude_max_images", "")).strip()
        if n:
            if not n.isdigit() or not (1 <= int(n) <= 10):
                return await settings_get(request, "Картинок в альбом — число от 1 до 10.", "err")
            st.set("claude_max_images", n)
        st.set("claude_mode", "1" if form.get("action") == "on" else "0")
        pub: Publisher = app["publisher"]
        if st.get("claude_mode") == "1" and not pub.claude_mode:
            return await settings_get(request, "Включил, но не хватает CLAUDE_API_KEY в .env — режим не заработает.", "err")
        return await settings_get(request, "Сохранено.")

    # --- посты -----------------------------------------------------------
    def _kind_label(kind: str) -> str:
        return _e({"text": "текст", "photo": "фото", "album": "альбом"}.get(kind, kind))

    async def posts_get(request: web.Request) -> web.Response:
        st: Storage = app["st"]
        rows = st.posts(30)
        items = "".join(
            f"""<tr>
              <td>#{r['id']}<br><span class="muted">{_kind_label(r['kind'])}{' ✏️' if r['edited_at'] else ''}</span></td>
              <td>{_e(r['title'][:120])}</td>
              <td class="muted">{time.strftime('%d.%m %H:%M', time.localtime(r['posted_at']))}</td>
              <td><a class="btn" href="/posts/{r['id']}">Открыть</a></td>
            </tr>""" for r in rows
        )
        table = (f"<table><tr><th>id</th><th>Заголовок</th><th>Опубликован</th><th></th></tr>{items}</table>"
                if rows else "<p class='muted'>Опубликованных постов пока нет.</p>")
        body = f"<h2>Последние посты</h2><div class='card'>{table}</div>"
        return web.Response(text=_layout("Посты", body), content_type="text/html")

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
        body = f"""
        <h2>Пост #{row['id']} ({_kind_label(row['kind'])})</h2>
        <div class="card">
          <div class="muted">Опубликован {time.strftime('%d.%m %H:%M', time.localtime(row['posted_at']))}{edited}</div>
          <div class="muted">{_e(row['title'])} · <a href="{_safe_href(row['link'])}" target="_blank" rel="noopener">исходная новость</a></div>
        </div>
        <div class="card">
          {draft_note}
          <form method="post" action="/posts/{row['id']}/save">{csrf_field(request)}
            <textarea name="text" rows="10" maxlength="{limit}">{_e(text)}</textarea>
            <div class="muted" style="margin-top:4px;">Лимит для этого поста: {limit} символов
              ({'подпись к фото' if row['kind'] in ('photo','album') else 'текстовое сообщение'})</div>
            <div class="row" style="margin-top:10px;">
              <button class="primary" type="submit">Сохранить в канал</button>
            </div>
          </form>
          <hr style="border-color:#2c2f3a; margin:16px 0;">
          <form method="post" action="/posts/{row['id']}/regen">{csrf_field(request)}
            <label>Перегенерировать через ИИ из исходной новости — пожелание (необязательно)</label>
            <div class="row">
              <input type="text" name="extra" placeholder="например: короче и без хештегов" style="flex:1;">
              <button type="submit">🤖 Перегенерировать</button>
            </div>
            <p class="muted">Не сохраняет сразу — покажет черновик выше, сохранить нужно отдельно.</p>
          </form>
        </div>
        """
        return web.Response(text=_layout(f"Пост #{row['id']}", body, flash, flash_kind), content_type="text/html")

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

    # --- расход ------------------------------------------------------------
    async def usage_get(request: web.Request) -> web.Response:
        pub: Publisher = app["publisher"]
        if pub.quota is None:
            body = "<p class='muted'>Учёт расхода недоступен.</p>"
            return web.Response(text=_layout("Расход", body), content_type="text/html")
        info = await pub.quota.snapshot(force=True)
        rows = [
            ("Запросов сегодня", f"{info.requests}" + (f" из {info.request_limit} ({info.request_pct:.0f}%)" if info.request_limit else "")),
            ("Токены", f"{info.tokens_in} вход / {info.tokens_out} выход"),
            ("Модель", info.model + (" (бесплатная)" if info.is_free_model else "")),
        ]
        if info.request_limit:
            rows.append(("Обнуление лимита", f"через {until_reset()} (00:00 UTC), источник: {info.limit_source}"))
        if info.credit_limit is not None:
            rows.append(("Кредиты на ключе", f"{info.credit_limit:.4f}, осталось {info.credit_remaining:.4f}"))
        body = "<h2>Расход за сутки (LLM/DeepSeek, не Claude)</h2><div class='card'><table>" + "".join(
            f"<tr><td class='muted'>{_e(k)}</td><td>{_e(v)}</td></tr>" for k, v in rows
        ) + "</table></div>"
        return web.Response(text=_layout("Расход", body), content_type="text/html")

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

    app.router.add_get("/login", login_get)
    app.router.add_post("/login", login_post)
    app.router.add_post("/logout", logout_post)
    app.router.add_get("/", dashboard)
    app.router.add_post("/pause", pause_post)
    app.router.add_post("/checknow", checknow_post)
    app.router.add_get("/feeds", feeds_get)
    app.router.add_post("/feeds/add", feeds_add)
    app.router.add_post("/feeds/{id}/delete", feeds_delete)
    app.router.add_post("/feeds/{id}/toggle", feeds_toggle)
    app.router.add_get("/template", template_get)
    app.router.add_post("/template", template_post)
    app.router.add_post("/template/reset", template_reset)
    app.router.add_get("/format", format_get)
    app.router.add_post("/format", format_post)
    app.router.add_post("/format/reset", format_reset)
    app.router.add_get("/settings", settings_get)
    app.router.add_post("/settings/channel", settings_channel)
    app.router.add_post("/settings/general", settings_general)
    app.router.add_post("/settings/debug", settings_debug)
    app.router.add_post("/settings/vk", settings_vk)
    app.router.add_post("/settings/claude", settings_claude)
    app.router.add_get("/posts", posts_get)
    app.router.add_get("/posts/{id}", post_detail)
    app.router.add_post("/posts/{id}/save", post_save)
    app.router.add_post("/posts/{id}/regen", post_regen)
    app.router.add_get("/usage", usage_get)

    return app


async def run_web_panel(storage: Storage, publisher: Publisher, bot: Bot,
                        password: str, port: int, host: str = "0.0.0.0"
                        ) -> tuple[web.AppRunner, web.TCPSite]:
    app = create_app(storage, publisher, bot, password)
    runner = web.AppRunner(app, access_log=log)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    return runner, site
