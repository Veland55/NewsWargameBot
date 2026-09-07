"""Тесты Storage (bot/db.py) — самая рискованная часть: очередь согласования
(гонки claim/release), дедуп-кандидаты, отложенные, стабильность entry_key,
миграции схемы поверх старой базы, дефолты настроек.
"""
from __future__ import annotations

import sqlite3
import time

import pytest

from bot.db import DEFAULTS, Storage, entry_key


# --- entry_key ------------------------------------------------------------

def test_entry_key_stable_for_same_parts():
    assert entry_key("feed:1", "guid:abc") == entry_key("feed:1", "guid:abc")


def test_entry_key_differs_for_different_parts():
    assert entry_key("feed:1", "guid:abc") != entry_key("feed:1", "guid:xyz")


def test_entry_key_ignores_empty_parts():
    # render() и вызывающий код собирают key_parts из необязательных полей —
    # пустая строка не должна незаметно менять хэш по сравнению с её
    # отсутствием вовсе (join фильтрует пустые части).
    assert entry_key("a", "", "b") == entry_key("a", "b")


def test_entry_key_is_short_hex():
    key = entry_key("любой", "текст")
    assert len(key) == 32
    int(key, 16)  # не бросает — валидный hex


# --- настройки: DEFAULTS ----------------------------------------------------

def test_get_returns_default_when_key_absent(storage: Storage):
    assert storage.get("interval") == DEFAULTS["interval"]
    assert storage.get("moderation_max_queue") == DEFAULTS["moderation_max_queue"]


def test_get_returns_stored_value_over_default(storage: Storage):
    storage.set("interval", "42")
    assert storage.get("interval") == "42"


def test_get_int_falls_back_to_default_on_garbage(storage: Storage):
    # Админ мог вписать в /set нечисловое значение вручную — get_int не
    # должен падать, а тихо откатываться на дефолт настройки.
    storage.set("interval", "не число")
    assert storage.get_int("interval") == int(DEFAULTS["interval"])


def test_get_int_unknown_key_without_default_is_zero(storage: Storage):
    assert storage.get_int("совсем неизвестный ключ") == 0


def test_set_if_absent_only_writes_once(storage: Storage):
    assert storage.set_if_absent("flag:x", "1") is True
    assert storage.get("flag:x") == "1"
    # Повторная попытка с другим значением не должна перезаписать —
    # именно это защищает Quota.check_and_alert от гонки двух проходов.
    assert storage.set_if_absent("flag:x", "2") is False
    assert storage.get("flag:x") == "1"


# --- очередь согласования: базовые операции --------------------------------

def _add_moderation(storage: Storage, *, feed_id: int = 1, key: str = "k1",
                    **overrides) -> int:
    fields = dict(feed_id=feed_id, key=key, title="t", summary="s", link="https://x/1",
                  source="src", published="", text="готовый текст", image="",
                  extra_images="", multi=False)
    fields.update(overrides)
    item_id = storage.add_moderation(**fields)
    assert item_id is not None
    return item_id


def test_add_moderation_unique_by_feed_and_key(storage: Storage):
    first = storage.add_moderation(feed_id=1, key="dup", title="t", summary="s",
                                   link="l", source="s", published="", text="txt",
                                   image="", extra_images="", multi=False)
    assert first is not None
    # Тот же (feed_id, key) — гонка автопрохода и /checknow: вторая
    # постановка должна тихо провалиться (см. UNIQUE(feed_id, key)), а не
    # завести дубликат карточки.
    second = storage.add_moderation(feed_id=1, key="dup", title="t2", summary="s2",
                                    link="l2", source="s2", published="", text="txt2",
                                    image="", extra_images="", multi=False)
    assert second is None
    assert storage.count_moderation() == 1


def test_moderation_list_orders_newest_first(storage: Storage):
    id1 = _add_moderation(storage, key="a")
    time.sleep(1.01)  # queued_at — секундная точность
    id2 = _add_moderation(storage, key="b")
    rows = storage.moderation_list(limit=10, offset=0)
    assert [r["id"] for r in rows] == [id2, id1]


def test_moderation_neighbor_walks_list_order_with_id_tiebreak(storage: Storage):
    # Три карточки одного прохода — queued_at совпадает секунда в секунду,
    # тай-брейк обязан идти по id (см. комментарий в moderation_neighbor),
    # иначе порядок "следующей карточки" был бы не определён.
    id1 = _add_moderation(storage, key="a")
    id2 = _add_moderation(storage, key="b")
    id3 = _add_moderation(storage, key="c")
    # moderation_list: ORDER BY queued_at DESC, id DESC -> [id3, id2, id1]
    assert storage.moderation_neighbor(id3) == id2
    assert storage.moderation_neighbor(id2) == id1
    assert storage.moderation_neighbor(id1) is None  # последняя — соседа нет


def test_moderation_neighbor_unknown_id_returns_none(storage: Storage):
    assert storage.moderation_neighbor(999999) is None


# --- claim/release: защита от гонки при публикации --------------------------

def test_claim_moderation_second_concurrent_claim_fails(storage: Storage):
    item_id = _add_moderation(storage)
    # Два админа почти одновременно жмут "Опубликовать" на одну карточку —
    # первый захват должен пройти, второй (тот же миг, тот же item) — нет.
    assert storage.claim_moderation(item_id, "admin-A", stale_after=600) is True
    assert storage.claim_moderation(item_id, "admin-B", stale_after=600) is False
    row = storage.moderation_item(item_id)
    assert row["status"] == "publishing"
    assert row["claimed_by"] == "admin-A"


def test_claim_moderation_stale_claim_can_be_reclaimed(storage: Storage, monkeypatch):
    item_id = _add_moderation(storage)
    monkeypatch.setattr("bot.db.time.time", lambda: 1_000_000)
    assert storage.claim_moderation(item_id, "admin-A", stale_after=600) is True
    # Процесс "упал" во время отправки — claim протух через stale_after:
    # карточка не должна остаться в 'publishing' навечно.
    monkeypatch.setattr("bot.db.time.time", lambda: 1_000_601)
    assert storage.claim_moderation(item_id, "admin-B", stale_after=600) is True
    row = storage.moderation_item(item_id)
    assert row["claimed_by"] == "admin-B"


def test_claim_moderation_not_yet_stale_cannot_be_reclaimed(storage: Storage, monkeypatch):
    item_id = _add_moderation(storage)
    monkeypatch.setattr("bot.db.time.time", lambda: 1_000_000)
    assert storage.claim_moderation(item_id, "admin-A", stale_after=600) is True
    monkeypatch.setattr("bot.db.time.time", lambda: 1_000_599)  # ещё не протухло
    assert storage.claim_moderation(item_id, "admin-B", stale_after=600) is False


def test_release_moderation_returns_to_queued_with_error(storage: Storage):
    item_id = _add_moderation(storage)
    storage.claim_moderation(item_id, "admin-A", stale_after=600)
    storage.release_moderation(item_id, "канал недоступен")
    row = storage.moderation_item(item_id)
    assert row["status"] == "queued"
    assert row["error"] == "канал недоступен"


def test_reset_stuck_moderation_on_startup(storage: Storage, tmp_path):
    item_id = _add_moderation(storage)
    storage.claim_moderation(item_id, "admin-A", stale_after=600)
    assert storage.moderation_item(item_id)["status"] == "publishing"
    # Новый процесс на той же базе (перезапуск) — reset_stuck_moderation
    # вызывается в __init__ и обязан снять зависший с прошлого раза claim,
    # иначе карточка навсегда "публикуется" и её нельзя ни опубликовать,
    # ни отклонить.
    st2 = Storage(tmp_path / "test.db")
    try:
        row = st2.moderation_item(item_id)
        assert row["status"] == "queued"
        assert row["claimed_at"] == 0
    finally:
        st2.close()


def test_delete_moderation_then_claim_is_noop(storage: Storage):
    item_id = _add_moderation(storage)
    storage.delete_moderation(item_id)
    # Двойной клик "Отклонить" — вторая попытка на уже удалённую карточку
    # не должна падать.
    assert storage.claim_moderation(item_id, "admin-A", stale_after=600) is False
    assert storage.moderation_item(item_id) is None


# --- restore_moderation (Undo после отклонения) -----------------------------

def test_restore_moderation_reinserts_same_id(storage: Storage):
    item_id = _add_moderation(storage, key="restore-me")
    row = storage.moderation_item(item_id)
    storage.delete_moderation(item_id)
    assert storage.moderation_item(item_id) is None
    assert storage.restore_moderation(row) is True
    restored = storage.moderation_item(item_id)
    assert restored is not None
    assert restored["id"] == item_id
    assert restored["status"] == "queued"
    assert restored["claimed_by"] == ""


def test_restore_moderation_conflict_returns_false(storage: Storage):
    # Пока карточка была в UNDO-стэше, лента успела поставить ту же новость
    # в очередь заново (тот же feed_id+key) — восстановление ПОД СТАРЫМ id
    # должно провалиться (UNIQUE(feed_id, key)), а не тихо создать дубль
    # или упасть с sqlite3.IntegrityError наружу.
    item_id = _add_moderation(storage, feed_id=7, key="k")
    row = storage.moderation_item(item_id)
    storage.delete_moderation(item_id)
    _add_moderation(storage, feed_id=7, key="k")  # новая карточка с тем же ключом
    assert storage.restore_moderation(row) is False


# --- prune_moderation --------------------------------------------------------

def test_prune_moderation_zero_keep_days_disables_pruning(storage: Storage):
    item_id = _add_moderation(storage)
    old = int(time.time() - 100 * 86400)
    storage._conn.execute("UPDATE moderation SET queued_at = ? WHERE id = ?", (old, item_id))
    storage._conn.commit()
    # keep_days<=0 значит "выключено", а не "агрессивнее всех" — раньше
    # код подстраховывался через max(1, ...) и 0 включал самый жёсткий
    # режим вместо отключения (см. комментарий в prune_moderation).
    assert storage.prune_moderation(keep_days=0) == 0
    assert storage.moderation_item(item_id) is not None


def test_prune_moderation_removes_only_old_unclaimed_unscheduled(storage: Storage):
    old = int(time.time() - 30 * 86400)
    id_old = _add_moderation(storage, key="old")
    id_publishing = _add_moderation(storage, key="publishing")
    id_scheduled = _add_moderation(storage, key="scheduled")
    for iid in (id_old, id_publishing, id_scheduled):
        storage._conn.execute("UPDATE moderation SET queued_at = ? WHERE id = ?", (old, iid))
    storage._conn.execute("UPDATE moderation SET status = 'publishing' WHERE id = ?", (id_publishing,))
    storage._conn.execute("UPDATE moderation SET scheduled_at = ? WHERE id = ?",
                          (int(time.time() + 3600), id_scheduled))
    storage._conn.commit()
    removed = storage.prune_moderation(keep_days=14)
    assert removed == 1
    assert storage.moderation_item(id_old) is None
    assert storage.moderation_item(id_publishing) is not None
    assert storage.moderation_item(id_scheduled) is not None


# --- moderation_due_ids (планирование публикации) ---------------------------

def test_moderation_due_ids_excludes_publishing_status(storage: Storage):
    due_id = _add_moderation(storage, key="due")
    claimed_id = _add_moderation(storage, key="claimed")
    future_id = _add_moderation(storage, key="future")
    now = int(time.time())
    storage.schedule_moderation(due_id, now - 1)
    storage.schedule_moderation(claimed_id, now - 1)
    storage.schedule_moderation(future_id, now + 3600)
    storage.claim_moderation(claimed_id, "admin", stale_after=600)
    due = storage.moderation_due_ids(now)
    assert due == [due_id]


# --- дедуп-кандидаты и отложенные -------------------------------------------

def test_dedup_candidate_add_list_delete(storage: Storage):
    cid = storage.add_dedup_candidate(feed_id=1, title="t", summary="s", link="l",
                                      source="src", published="", image="",
                                      matched_post_id=5, score=0.7)
    assert storage.count_dedup_candidates() == 1
    row = storage.dedup_candidate(cid)
    assert row["matched_post_id"] == 5
    storage.delete_dedup_candidate(cid)
    assert storage.dedup_candidate(cid) is None
    assert storage.count_dedup_candidates() == 0


def test_postponed_upsert_bumps_attempts(storage: Storage):
    kwargs = dict(feed_id=1, key="k", title="t", summary="s", link="l",
                  published="", image="")
    storage.add_postponed(**kwargs, error="первый отказ")
    storage.add_postponed(**kwargs, error="второй отказ")
    row = storage.postponed_item(1)
    assert row["attempts"] == 2
    assert row["error"] == "второй отказ"


def test_remove_postponed(storage: Storage):
    storage.add_postponed(feed_id=1, key="k", title="t", summary="s", link="l",
                          published="", image="", error="e")
    assert storage.count_postponed() == 1
    storage.remove_postponed(1, "k")
    assert storage.count_postponed() == 0


# --- миграции: поднять Storage поверх старой базы ----------------------------

def _make_legacy_db(path) -> None:
    """Схема "до" всех ALTER-миграций — без scheduled_at в moderation, без
    backend в usage, без новых колонок в feeds. Имитирует базу, созданную
    старой версией бота, чтобы проверить, что апгрейд не роняет процесс."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
    CREATE TABLE feeds (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL DEFAULT '',
        enabled INTEGER NOT NULL DEFAULT 1,
        template TEXT, etag TEXT, modified TEXT,
        last_check INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        added_at INTEGER NOT NULL
    );
    CREATE TABLE seen (feed_id INTEGER NOT NULL, key TEXT NOT NULL,
        seen_at INTEGER NOT NULL, PRIMARY KEY (feed_id, key));
    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE usage (day TEXT NOT NULL, requests INTEGER NOT NULL DEFAULT 0,
        tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0,
        cost REAL NOT NULL DEFAULT 0, PRIMARY KEY (day));
    CREATE TABLE posts (id INTEGER PRIMARY KEY AUTOINCREMENT, feed_id INTEGER,
        chat_id TEXT NOT NULL, message_id INTEGER NOT NULL, kind TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '',
        link TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
        published TEXT NOT NULL DEFAULT '', text TEXT NOT NULL DEFAULT '',
        posted_at INTEGER NOT NULL, edited_at INTEGER);
    CREATE TABLE moderation (
        id INTEGER PRIMARY KEY AUTOINCREMENT, feed_id INTEGER NOT NULL,
        key TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
        summary TEXT NOT NULL DEFAULT '', link TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT '', published TEXT NOT NULL DEFAULT '',
        text TEXT NOT NULL DEFAULT '', image TEXT NOT NULL DEFAULT '',
        extra_images TEXT NOT NULL DEFAULT '', multi INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'queued', claimed_at INTEGER NOT NULL DEFAULT 0,
        claimed_by TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
        queued_at INTEGER NOT NULL, edited_at INTEGER,
        UNIQUE (feed_id, key)
    );
    """)
    conn.execute("INSERT INTO feeds (url, title, added_at) VALUES ('https://x/rss', 'X', 100)")
    conn.execute("INSERT INTO usage (day, requests, tokens_in, tokens_out, cost) "
                 "VALUES ('2024-01-01', 5, 10, 20, 0.5)")
    conn.execute("INSERT INTO moderation (feed_id, key, text, queued_at) "
                 "VALUES (1, 'k', 'старый пост', 100)")
    conn.execute("INSERT INTO settings (key, value) VALUES ('multi_images', '1')")
    conn.commit()
    conn.close()


def test_storage_upgrades_legacy_schema_without_error(tmp_path):
    db_path = tmp_path / "legacy.db"
    _make_legacy_db(db_path)
    st = Storage(db_path)
    try:
        # Новые колонки должны появиться и быть читаемы без KeyError/OperationalError.
        feed = st.feed(1)
        assert feed["kind"] == "rss"
        assert feed["multi_images"] == 1  # перенесено из старой глобальной настройки

        item = st.moderation_item(1)
        assert item["text"] == "старый пост"
        assert item["scheduled_at"] is None  # новая колонка, дефолт NULL

        # usage мигрировала на составной ключ (day, backend='default'),
        # старые данные не потерялись.
        assert st.usage("2024-01-01", "default") == {
            "requests": 5, "tokens_in": 10, "tokens_out": 20, "cost": 0.5,
        }
        # multi_images как отдельная настройка больше не существует.
        assert st.get("multi_images") == ""
    finally:
        st.close()


def test_storage_migration_is_idempotent(tmp_path):
    # Открыть уже дважды смигрированную базу третий раз не должно падать
    # (ALTER TABLE ... ADD COLUMN на уже существующую колонку).
    db_path = tmp_path / "legacy.db"
    _make_legacy_db(db_path)
    Storage(db_path).close()
    Storage(db_path).close()
    st = Storage(db_path)
    st.close()


def test_migrate_settings_key_rename_preserves_value(tmp_path):
    db_path = tmp_path / "legacy.db"
    _make_legacy_db(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO settings (key, value) VALUES ('claude_max_images', '4')")
    conn.commit()
    conn.close()
    st = Storage(db_path)
    try:
        assert st.get("max_images") == "4"
        assert st.get("claude_max_images") == DEFAULTS.get("claude_max_images", "")
    finally:
        st.close()
