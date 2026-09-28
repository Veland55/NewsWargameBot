"""Регрессионные тесты на исправленные баги аудита: republish, сравнение
паролей, явные значения тумблеров,
дочитывание страниц сайтов без RSS, повтор битого ответа LLM и др.

Сеть не трогаем: Telegram/LLM/страницы — моки, панель поднимается
aiohttp.test_utils на локальном порту без внешних запросов."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiohttp.test_utils import TestClient, TestServer

from bot import rss
from bot.db import Storage
from bot.handlers import _setting_int
from bot.llm import LLMClient
from bot.publisher import HYDRATE_MAX_FAILS
from bot.web import SESSION_COOKIE, _secret_eq, create_app
from tests.conftest import make_entry
from tests.test_publisher import _queued_row, fake_message, make_publisher


# --- republish_post ------------------------------------------------------------

def _post(storage: Storage, message_id: int = 100) -> int:
    return storage.add_post(feed_id=None, chat_id="-100", message_id=message_id, kind="text",
                            title="t", summary="s", link="https://example.com/a",
                            source="src", published="", text="старый")


async def test_republish_sends_even_if_original_post_is_fresh(storage: Storage):
    """Баг: posted_recently(link) находил сам исходный пост, и если тот был
    опубликован меньше 10 минут назад, republish «удавался» без отправки."""
    pub = make_publisher(storage)
    pub._send = AsyncMock(return_value=fake_message(message_id=777))
    pub._image_of = AsyncMock(return_value="")
    pub._send_vk_ready = AsyncMock()
    post_id = _post(storage, message_id=100)

    assert await pub.republish_post(post_id, "новый", expected_message_id=100) is None
    pub._send.assert_awaited_once()
    assert storage.post(post_id)["message_id"] == 777


async def test_republish_repeat_submit_does_not_send_twice(storage: Storage):
    pub = make_publisher(storage)
    pub._send = AsyncMock(return_value=fake_message(message_id=777))
    pub._image_of = AsyncMock(return_value="")
    pub._send_vk_ready = AsyncMock()
    post_id = _post(storage, message_id=100)

    await pub.republish_post(post_id, "новый", expected_message_id=100)
    # Та же форма отправлена повторно — запись уже указывает на 777.
    assert await pub.republish_post(post_id, "новый", expected_message_id=100) is None
    assert pub._send.await_count == 1


# --- _send: блокировка прохода только за отказ по адресату ----------------------

async def test_send_non_delivery_bad_request_does_not_block_pass(storage: Storage):
    pub = make_publisher(storage)
    pub.bot.send_message = AsyncMock(side_effect=TelegramBadRequest(
        method=MagicMock(), message="Bad Request: message is too long"))
    assert await pub._send("текст") is None
    assert pub._blocked is False


async def test_send_delivery_error_still_blocks_pass(storage: Storage):
    pub = make_publisher(storage)
    pub.bot.send_message = AsyncMock(side_effect=TelegramBadRequest(
        method=MagicMock(), message="Bad Request: chat not found"))
    assert await pub._send("текст") is None
    assert pub._blocked is True


# --- сайты без RSS: нечитаемые страницы ------------------------------------------

async def test_hydrate_gives_up_after_repeated_failures(storage: Storage, monkeypatch):
    pub = make_publisher(storage)
    feed_id = storage.add_feed("https://site.example/", kind="search")
    monkeypatch.setattr("bot.publisher.fetch_article_entry", AsyncMock(return_value=None))
    fresh = [("k1", make_entry(key="k1", link="https://site.example/a"))]

    for _ in range(HYDRATE_MAX_FAILS - 1):
        assert await pub._hydrate_search_entries(feed_id, fresh) == []
        assert storage.is_seen(feed_id, "k1") is False
    await pub._hydrate_search_entries(feed_id, fresh)
    assert storage.is_seen(feed_id, "k1") is True
    assert not pub._hydrate_fails


async def test_fetch_article_entry_sets_published_string_from_jsonld(monkeypatch):
    page = ('<html><head><meta property="og:title" content="Заголовок"></head><body>'
            '<script>{"datePublished": "2026-09-20T10:30:00Z"}</script></body></html>')
    monkeypatch.setattr(rss, "_fetch_text", AsyncMock(return_value=(200, page)))
    entry = await rss.fetch_article_entry("https://site.example/a", 123.0, "")
    assert entry.published == "2026-09-20 10:30 UTC"
    assert entry.published_ts == rss._parse_iso_date("2026-09-20T10:30:00Z")


# --- publish_moderated: сбой после отправки -----------------------------------

async def test_publish_moderated_removes_card_even_if_record_fails(storage: Storage):
    pub = make_publisher(storage)
    item_id = _queued_row(storage)
    pub._send = AsyncMock(return_value=fake_message())
    pub._record_post = AsyncMock(side_effect=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        await pub.publish_moderated(item_id)
    assert storage.moderation_item(item_id) is None


async def test_record_post_survives_vk_forward_failure(storage: Storage):
    pub = make_publisher(storage)
    pub._send_vk_ready = AsyncMock(side_effect=RuntimeError("vk"))
    from bot.publisher import Post
    await pub._record_post(None, make_entry(), None, Post(text="x"), fake_message())
    assert len(storage.posts()) == 1


# --- LLM: битый ответ с кодом 200 повторяется ---------------------------------

class _Resp:
    def __init__(self, status: int, body: str):
        self.status, self._body = status, body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def test_llm_retries_non_json_200(monkeypatch):
    client = LLMClient("https://llm.example/v1", "key", "m", retries=1)
    bodies = iter([_Resp(200, "<html>proxy</html>"),
                   _Resp(200, '{"choices":[{"message":{"content":"ответ"},"finish_reason":"stop"}]}')])
    session = MagicMock()
    session.post = lambda *a, **kw: next(bodies)
    monkeypatch.setattr(client, "_get_session", AsyncMock(return_value=session))
    monkeypatch.setattr("bot.llm.asyncio.sleep", AsyncMock())
    assert await client.complete("вопрос") == "ответ"


# --- /set: те же правила чисел, что и в панели -----------------------------------

def test_setting_int_rejects_signs_and_underscores():
    assert _setting_int("15") == 15
    for bad in ("-5", "+5", "1_000", " 5", "²", ""):
        assert _setting_int(bad) is None


# --- веб-панель -----------------------------------------------------------------

def test_secret_eq_handles_non_ascii():
    assert _secret_eq("пароль", "пароль") is True
    assert _secret_eq("пароль", "parol") is False


@pytest.fixture
async def panel(storage: Storage):
    publisher = MagicMock()
    publisher.debug = False
    publisher.moderation = False
    app = create_app(storage, publisher, MagicMock(token="1:x"), "secret", admin_ids={1})
    client = TestClient(TestServer(app))
    await client.start_server()
    yield client, app
    await client.close()


async def _login(client: TestClient) -> str:
    resp = await client.post("/login", data={"password": "secret"}, allow_redirects=False)
    assert resp.status == 302
    token = resp.cookies[SESSION_COOKIE].value
    csrf = client.app["auth"].verify(token)["csrf"]
    return csrf


async def test_login_with_cyrillic_password_is_401_not_500(panel):
    client, _ = panel
    resp = await client.post("/login", data={"password": "пароль"})
    assert resp.status == 401


async def test_api_is_gone_even_with_old_bearer(panel):
    """JSON API десктоп-клиента удалён: /api/* — обычный путь панели,
    без сессии уходит на вход, Bearer-пароль ничего не открывает."""
    client, _ = panel
    resp = await client.get("/api/config", headers={"Authorization": "Bearer secret"},
                            allow_redirects=False)
    assert resp.status == 302
    assert resp.headers["Location"].startswith("/login")


async def test_non_numeric_id_is_404(panel):
    client, _ = panel
    await _login(client)
    resp = await client.get("/posts/abc")
    assert resp.status == 404


async def test_pause_toggle_uses_explicit_value(panel, storage: Storage):
    client, _ = panel
    csrf = await _login(client)
    for _ in range(2):   # двойной клик «Приостановить» не должен снимать паузу
        await client.post("/pause", data={"csrf": csrf, "value": "1"}, allow_redirects=False)
    assert storage.get("paused") == "1"


async def test_huge_id_is_404_not_500(panel):
    """19+ цифр не влезают в INTEGER SQLite — раньше OverflowError → 500."""
    client, _ = panel
    await _login(client)
    resp = await client.get("/posts/99999999999999999999999")
    assert resp.status == 404


async def test_redirect_is_plain_302(panel):
    client, _ = panel
    resp = await client.get("/", allow_redirects=False)
    assert resp.status == 302
    assert resp.headers["Location"].startswith("/login")


# --- сбитая лента: flood_guard раньше дедупликации ------------------------------

async def test_flood_guard_runs_before_dedup(storage: Storage, monkeypatch):
    """Сменилась схема guid — вся лента выглядит новой, но это те же уже
    опубликованные статьи. Раньше дедуп успевал отправить их все в очередь
    дублей до того, как flood_guard их отсекал."""
    from bot.rss import FetchResult
    pub = make_publisher(storage)
    feed_id = storage.add_feed("https://example.com/rss", title="Лента")
    storage.mark_seen(feed_id, "old")          # не первый опрос
    storage.set("flood_guard", "15")
    storage.set("max_age_days", "0")
    titles = [f"Новый набор миниатюр номер {i} для армии хаоса" for i in range(20)]
    for t in titles:
        storage.add_post(feed_id=feed_id, chat_id="-1", message_id=1, kind="text", title=t,
                         summary=t, link="", source="", published="", text="x")
    entries = [make_entry(key=f"new-guid-{i}", title=t, summary=t, link=f"https://example.com/{i}")
               for i, t in enumerate(titles)]
    monkeypatch.setattr("bot.publisher.fetch", AsyncMock(return_value=FetchResult(entries=entries)))
    pub.build_post = AsyncMock(side_effect=AssertionError("дубль не должен дойти до модели"))

    await pub._process_feed(storage.feed(feed_id))
    assert storage.count_dedup_candidates() <= 1


# --- × на миниатюре в карточке согласования (queue_image_delete) -----------

_IMGS = ["https://x/a.jpg", "https://x/bg.png", "https://x/c.jpg"]


async def test_queue_card_shows_delete_button_on_each_image(panel, storage: Storage):
    client, _ = panel
    await _login(client)
    item_id = _queued_row(storage, image=_IMGS[0], extra_images="\n".join(_IMGS[1:]), multi=True)
    html = await (await client.get(f"/queue/{item_id}")).text()
    assert html.count(f'formaction="/queue/{item_id}/image-delete"') == 3


async def test_queue_image_delete_removes_only_that_image(panel, storage: Storage):
    client, _ = panel
    csrf = await _login(client)
    item_id = _queued_row(storage, image=_IMGS[0], extra_images="\n".join(_IMGS[1:]), multi=True)
    resp = await client.post(f"/queue/{item_id}/image-delete",
                             data={"csrf": csrf, "url": _IMGS[1], "text": "пост"})
    assert resp.status == 200
    row = storage.moderation_item(item_id)
    assert row["image"] == _IMGS[0]
    assert row["extra_images"] == _IMGS[2]


async def test_queue_image_delete_first_image_promotes_next(panel, storage: Storage):
    client, _ = panel
    csrf = await _login(client)
    item_id = _queued_row(storage, image=_IMGS[0], extra_images=_IMGS[1], multi=True)
    await client.post(f"/queue/{item_id}/image-delete", data={"csrf": csrf, "url": _IMGS[0]})
    row = storage.moderation_item(item_id)
    assert (row["image"], row["extra_images"]) == (_IMGS[1], "")
    await client.post(f"/queue/{item_id}/image-delete", data={"csrf": csrf, "url": _IMGS[1]})
    row = storage.moderation_item(item_id)
    assert (row["image"], row["extra_images"]) == ("", "")


async def test_queue_image_delete_keeps_unsaved_text_and_ignores_unknown_url(panel, storage: Storage):
    client, _ = panel
    csrf = await _login(client)
    item_id = _queued_row(storage, image=_IMGS[0])
    resp = await client.post(f"/queue/{item_id}/image-delete",
                             data={"csrf": csrf, "url": "https://x/other.jpg", "text": "правка"})
    html = await resp.text()
    assert "Этой картинки в посте уже нет" in html
    assert "правка" in html                       # несохранённый текст не потерян
    row = storage.moderation_item(item_id)
    assert row["image"] == _IMGS[0] and row["text"] == "пост"


async def test_queue_image_delete_refused_while_publishing(panel, storage: Storage):
    client, _ = panel
    csrf = await _login(client)
    item_id = _queued_row(storage, image=_IMGS[0])
    assert storage.claim_moderation(item_id, "test", stale_after=600)
    await client.post(f"/queue/{item_id}/image-delete", data={"csrf": csrf, "url": _IMGS[0]})
    assert storage.moderation_item(item_id)["image"] == _IMGS[0]
