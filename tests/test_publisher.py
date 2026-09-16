"""Тесты Publisher (bot/publisher.py) — постановка в очередь согласования,
защита от гонки при публикации карточки, дедупликация похожих новостей.

Никаких настоящих сетевых вызовов: bot/llm — Mock, отправка в Telegram
(_send) подменяется на предсказуемый фейк.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.db import Storage
from bot.publisher import (DEDUP_MIN_SIGNAL, Post, Publisher, _dedup_similarity,
                           _shares_named_run)
from tests.conftest import make_entry


def make_publisher(storage: Storage, **overrides) -> Publisher:
    # AsyncMock, не MagicMock: bot.send_message/send_photo — реальные async-методы
    # aiogram.Bot, _send_vk_ready (см. publisher.py) их await-ит напрямую, а не
    # через мокнутый Publisher._send.
    bot = AsyncMock()
    llm = MagicMock()
    llm.model = "test-model"
    llm.on_usage = None
    kwargs = dict(bot=bot, storage=storage, llm=llm, default_channel="@testchannel",
                 admin_ids={1, 2})
    kwargs.update(overrides)
    return Publisher(**kwargs)


def fake_message(message_id: int = 555, chat_id: int = -100123, photo=None):
    return SimpleNamespace(message_id=message_id, chat=SimpleNamespace(id=chat_id), photo=photo)


# --- сходство/дедуп: чистые функции -----------------------------------------

def test_dedup_similarity_identical_text_is_one():
    assert _dedup_similarity("Привет мир", "Привет мир") == 1.0


def test_dedup_similarity_disjoint_text_is_zero():
    assert _dedup_similarity("кошка собака", "стол стул") == 0.0


def test_dedup_similarity_empty_is_zero():
    assert _dedup_similarity("", "что угодно") == 0.0


def test_shares_named_run_finds_common_product_name():
    # Реальный случай из комментария в publisher.py: разные сайты, общее
    # собственное имя анонса, совсем разные остальные слова.
    a = "New Space Marine Captain On Bike Rides Into Warhammer 40,000"
    b = "New Space Marine Captain on Bike revealed"
    assert _shares_named_run(a, b) is True


def test_shares_named_run_false_for_unrelated_titles():
    assert _shares_named_run("Новый набор миниатюр вышел", "Отчёт о турнире выходных") is False


def test_shares_named_run_false_when_only_stopwords_shared():
    # Общий "New Warhammer Age of Sigmar" — только стоп-слова, не считается
    # значимым совпадением (см. _DEDUP_STOPWORDS/_NAMED_RUN_MIN_SIGNAL).
    a = "New Warhammer Age of Sigmar box announced"
    b = "New Warhammer Age of Sigmar starter revealed"
    assert _shares_named_run(a, b) is False


# --- _find_duplicate ---------------------------------------------------------

def test_find_duplicate_matches_on_named_run_even_below_threshold(storage: Storage):
    storage.set("dedup_threshold", "55")
    pub = make_publisher(storage)
    entry = make_entry(title="New Space Marine Captain on Bike revealed",
                       summary="Совершенно другими словами описание анонса")
    candidates = [{"id": 42,
                  "title": "New Space Marine Captain On Bike Rides Into Warhammer 40,000",
                  "summary": "Иное описание, ни одного общего слова с оригиналом почти"}]
    match = pub._find_duplicate(entry, candidates)
    assert match is not None
    matched_id, score = match
    assert matched_id == 42
    assert score >= 0.55


def test_find_duplicate_none_when_nothing_similar(storage: Storage):
    pub = make_publisher(storage)
    entry = make_entry(title="Совсем другая новость", summary="Ни на что не похоже")
    candidates = [{"id": 1, "title": "Абсолютно из другой оперы", "summary": "Ничего общего тут"}]
    assert pub._find_duplicate(entry, candidates) is None


# --- _queue_for_review --------------------------------------------------------

def _feed_row(storage: Storage, url: str = "https://example.com/rss") -> object:
    feed_id = storage.add_feed(url, title="Тестовая лента")
    return storage.feed(feed_id)


@pytest.mark.asyncio
async def test_queue_for_review_adds_card_and_marks_seen(storage: Storage):
    pub = make_publisher(storage)
    feed = _feed_row(storage)
    entry = make_entry(key="k1")
    item_id = await pub._queue_for_review(feed, "k1", entry, "готовый текст поста")
    assert item_id is not None
    assert storage.count_moderation() == 1
    assert storage.is_seen(feed["id"], "k1") is True
    assert pub._queued and pub._queued[0][2] == item_id


@pytest.mark.asyncio
async def test_queue_for_review_race_returns_none_but_still_marks_seen(storage: Storage):
    """Гонка автопрохода и /checknow: одна и та же запись пытается встать в
    очередь дважды (тот же feed_id+key). Storage.add_moderation защищает
    от дубля карточки — но раньше при этом не помечал запись прочитанной,
    из-за чего она "протухала" в seen после prune и заходила по кругу на
    каждый следующий проход (см. комментарий в _queue_for_review)."""
    pub = make_publisher(storage)
    feed = _feed_row(storage)
    entry = make_entry(key="k1")
    first = await pub._queue_for_review(feed, "k1", entry, "текст 1")
    second = await pub._queue_for_review(feed, "k1", entry, "текст 2")
    assert first is not None
    assert second is None
    assert storage.count_moderation() == 1
    assert storage.is_seen(feed["id"], "k1") is True


# --- publish_moderated: гонка публикации -------------------------------------

def _queued_row(storage: Storage, **overrides) -> int:
    fields = dict(feed_id=1, key="k", title="t", summary="s", link="https://x/1",
                  source="src", published="", text="пост", image="", extra_images="",
                  multi=False)
    fields.update(overrides)
    item_id = storage.add_moderation(**fields)
    assert item_id is not None
    return item_id


@pytest.mark.asyncio
async def test_publish_moderated_second_concurrent_click_is_rejected(storage: Storage, monkeypatch):
    pub = make_publisher(storage)
    item_id = _queued_row(storage)
    send_mock = AsyncMock(return_value=fake_message())
    monkeypatch.setattr(pub, "_send", send_mock)
    # Кто-то (другой админ/планировщик) уже держит захват карточки.
    assert storage.claim_moderation(item_id, "someone-else", stale_after=600) is True

    error = await pub.publish_moderated(item_id, actor="web")

    assert error == "Эту новость уже публикует или опубликовал кто-то другой."
    send_mock.assert_not_called()
    # Карточка остаётся под тем же захватом — вторая попытка не должна была
    # ни отправить сообщение, ни тронуть статус чужого захвата.
    row = storage.moderation_item(item_id)
    assert row["claimed_by"] == "someone-else"


@pytest.mark.asyncio
async def test_publish_moderated_success_deletes_card_and_records_post(storage: Storage, monkeypatch):
    pub = make_publisher(storage)
    item_id = _queued_row(storage, text="Готовый пост")
    send_mock = AsyncMock(return_value=fake_message())
    monkeypatch.setattr(pub, "_send", send_mock)

    error = await pub.publish_moderated(item_id, actor="web")

    assert error is None
    send_mock.assert_awaited_once()
    assert storage.moderation_item(item_id) is None
    posts = storage.posts(limit=10)
    assert len(posts) == 1
    assert posts[0]["text"] == "Готовый пост"


@pytest.mark.asyncio
async def test_publish_moderated_send_failure_releases_claim_for_retry(storage: Storage, monkeypatch):
    pub = make_publisher(storage)
    item_id = _queued_row(storage)
    send_mock = AsyncMock(return_value=None)  # канал недоступен
    monkeypatch.setattr(pub, "_send", send_mock)

    error = await pub.publish_moderated(item_id, actor="web")

    assert error == "Не удалось опубликовать — канал недоступен или не задан."
    row = storage.moderation_item(item_id)
    assert row is not None
    assert row["status"] == "queued"          # не осталась висеть в 'publishing'
    assert row["error"] == "канал недоступен или не задан"


@pytest.mark.asyncio
async def test_publish_moderated_missing_row_returns_error(storage: Storage):
    # incomplete claim_moderation UPDATE затрагивает 0 строк на несуществующем
    # id, так что claim проваливается раньше проверки на None — сообщение
    # то же, что у гонки с другим админом, а не "не найдена". В реальном
    # UI это не проявляется: queue_publish в web.py сам проверяет
    # moderation_item() и отдаёт 404 раньше, чем вызвать publish_moderated.
    pub = make_publisher(storage)
    error = await pub.publish_moderated(999999, actor="web")
    assert error == "Эту новость уже публикует или опубликовал кто-то другой."


# --- retry_postponed/publish_now: защита от повторной отправки --------------
# См. DUPLICATE_GUARD_SECONDS в publisher.py и инцидент 30.08 в проде:
# /postponed/1/retry дважды подряд (клиент не увидел ответ вовремя из-за
# медленной сети) — оба вызова прошли _manual_publish_locks (первый уже
# успел его снять к моменту второго) и отправили один и тот же пост в канал
# дважды.

def _postponed_row_id(storage: Storage, *, link: str = "https://x/1") -> int:
    storage.add_postponed(feed_id=1, key="k", title="t", summary="s", link=link,
                          published="", image="", error="проверка")
    row = storage.postponed_list(limit=50)[0]
    return row["id"]


@pytest.mark.asyncio
async def test_retry_postponed_skips_resend_when_already_posted(storage: Storage, monkeypatch):
    pub = make_publisher(storage)
    item_id = _postponed_row_id(storage, link="https://x/dup")
    # Первая попытка уже реально опубликовала (сообщение до клиента просто
    # не дошло вовремя) — есть свежий пост с той же ссылкой.
    storage.add_post(feed_id=1, chat_id="@testchannel", message_id=1, kind="text",
                     title="t", summary="s", link="https://x/dup", source="src",
                     published="", text="пост")
    send_mock = AsyncMock(return_value=fake_message())
    monkeypatch.setattr(pub, "_send", send_mock)

    error = await pub.retry_postponed(item_id)

    assert error is None
    send_mock.assert_not_called()
    assert storage.postponed_item(item_id) is None  # разобрана, не висит дальше


@pytest.mark.asyncio
async def test_retry_postponed_sends_when_nothing_posted_yet(storage: Storage, monkeypatch):
    pub = make_publisher(storage)
    item_id = _postponed_row_id(storage, link="https://x/fresh")
    send_mock = AsyncMock(return_value=fake_message())
    monkeypatch.setattr(pub, "_send", send_mock)
    monkeypatch.setattr(pub, "build_post", AsyncMock(return_value=SimpleNamespace(
        text="пост", image="", images=[], link="https://x/fresh")))

    error = await pub.retry_postponed(item_id)

    assert error is None
    send_mock.assert_awaited_once()
    assert storage.postponed_item(item_id) is None


@pytest.mark.asyncio
async def test_publish_now_skips_resend_when_already_posted(storage: Storage, monkeypatch):
    pub = make_publisher(storage)
    storage.add_post(feed_id=1, chat_id="@testchannel", message_id=1, kind="text",
                     title="t", summary="s", link="https://x/dup2", source="src",
                     published="", text="пост")
    entry = make_entry(link="https://x/dup2")
    send_mock = AsyncMock(return_value=fake_message())
    monkeypatch.setattr(pub, "_send", send_mock)

    error = await pub.publish_now(entry, feed=None)

    assert error is None
    send_mock.assert_not_called()


# --- _send_vk_ready: пересылка готового поста админам для ручной публикации в VK -


@pytest.mark.asyncio
async def test_send_vk_ready_text_only_sends_plain_message_to_each_admin(storage: Storage):
    pub = make_publisher(storage)  # admin_ids={1, 2}
    post = Post(text="<b>Заголовок</b><br>Текст с & амперсандом", image="", images=[])

    await pub._send_vk_ready(post)

    assert pub.bot.send_photo.await_count == 0
    assert pub.bot.send_message.await_count == 2
    sent_ids = {c.kwargs["chat_id"] for c in pub.bot.send_message.await_args_list}
    assert sent_ids == {1, 2}
    for c in pub.bot.send_message.await_args_list:
        assert c.kwargs["parse_mode"] is None  # иначе "<b>"/"&" сорвали бы отправку
        assert "<b>" not in c.kwargs["text"]  # to_plain уже снял разметку
        assert "Текст с & амперсандом" in c.kwargs["text"]


@pytest.mark.asyncio
async def test_send_vk_ready_with_image_sends_photo_then_text(storage: Storage):
    pub = make_publisher(storage)
    post = Post(text="Новость с картинкой", image="", images=[(b"fake-bytes", "image/jpeg")])

    await pub._send_vk_ready(post)

    assert pub.bot.send_photo.await_count == 2  # по одному на каждого из 2 админов
    assert pub.bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_send_vk_ready_empty_post_sends_nothing(storage: Storage):
    pub = make_publisher(storage)
    post = Post(text="", image="", images=[])

    await pub._send_vk_ready(post)

    assert pub.bot.send_photo.await_count == 0
    assert pub.bot.send_message.await_count == 0


@pytest.mark.asyncio
async def test_send_vk_ready_no_admins_sends_nothing(storage: Storage):
    pub = make_publisher(storage, admin_ids=set())
    post = Post(text="Новость", image="", images=[])

    await pub._send_vk_ready(post)

    pub.bot.send_message.assert_not_awaited()
