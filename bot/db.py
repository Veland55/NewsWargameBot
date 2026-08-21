"""SQLite-хранилище: ленты, настройки, отметки о прочитанном.

Все запросы короткие, поэтому используем синхронный sqlite3 под локом —
это дешевле и проще, чем тащить async-драйвер.
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS feeds (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    url           TEXT NOT NULL UNIQUE,
    title         TEXT NOT NULL DEFAULT '',
    enabled       INTEGER NOT NULL DEFAULT 1,
    template      TEXT,              -- переопределение шаблона для этой ленты
    etag          TEXT,              -- условный GET, чтобы не качать лишнее
    modified      TEXT,
    pending       INTEGER NOT NULL DEFAULT 0,  -- остался непубликованный хвост
    last_check    INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    multi_images  INTEGER NOT NULL DEFAULT 0,  -- несколько картинок альбомом вместо одной
    added_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS seen (
    feed_id   INTEGER NOT NULL,
    key       TEXT NOT NULL,
    seen_at   INTEGER NOT NULL,
    PRIMARY KEY (feed_id, key)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Картинки, вытащенные со страниц новостей (og:image). Пустая строка —
-- «смотрели, там нет»: это тоже кэшируем, чтобы не ходить повторно.
CREATE TABLE IF NOT EXISTS page_image (
    url        TEXT PRIMARY KEY,
    image      TEXT NOT NULL,
    checked_at INTEGER NOT NULL
) WITHOUT ROWID;

-- Расход по дням UTC: лимиты бесплатных моделей OpenRouter суточные,
-- сбрасываются в 00:00 UTC.
CREATE TABLE IF NOT EXISTS usage (
    day        TEXT PRIMARY KEY,
    requests   INTEGER NOT NULL DEFAULT 0,
    tokens_in  INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cost       REAL    NOT NULL DEFAULT 0
) WITHOUT ROWID;

-- Уже опубликованные посты — чтобы их можно было найти и отредактировать
-- (/posts, /edit, /setpost, /regen). Пишется только для настоящих публикаций
-- в канал, не для превью /test и не для отладочных постов в личку.
CREATE TABLE IF NOT EXISTS posts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_id    INTEGER,
    chat_id    TEXT NOT NULL,
    message_id INTEGER NOT NULL,   -- сообщение, у которого редактируется текст/подпись
    kind       TEXT NOT NULL,      -- 'text' | 'photo' | 'album' — какой метод edit_message_* нужен
    title      TEXT NOT NULL DEFAULT '',
    summary    TEXT NOT NULL DEFAULT '',
    link       TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT '',
    published  TEXT NOT NULL DEFAULT '',
    text       TEXT NOT NULL DEFAULT '',   -- текущий текст поста, каким он сейчас в канале
    -- message_id второй и следующих картинок альбома (первая — message_id
    -- выше, у неё подпись), через запятую. Только у kind='album'; позволяет
    -- удалить отдельную картинку из уже опубликованного альбома.
    extra_message_ids TEXT NOT NULL DEFAULT '',
    posted_at  INTEGER NOT NULL,
    edited_at  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_posts_posted ON posts (posted_at DESC);

-- Новости, похожие на уже опубликованный пост (с другой ленты или под
-- другим guid этой же) — не публикуются сами, ждут ручного разбора в
-- веб-панели (раздел «Ленты»): посмотреть и опубликовать, если совпадение
-- ложное, или удалить, если дубль настоящий. См. Publisher._drop_duplicates.
CREATE TABLE IF NOT EXISTS dedup_candidates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_id         INTEGER,
    title           TEXT NOT NULL DEFAULT '',
    summary         TEXT NOT NULL DEFAULT '',
    link            TEXT NOT NULL DEFAULT '',
    source          TEXT NOT NULL DEFAULT '',
    published       TEXT NOT NULL DEFAULT '',
    image           TEXT NOT NULL DEFAULT '',
    matched_post_id INTEGER,        -- пост, с которым засчитано совпадение (может быть уже удалён)
    score           REAL NOT NULL DEFAULT 0,   -- итоговая схожесть 0-1, для прозрачности в интерфейсе
    detected_at     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dedup_detected ON dedup_candidates (detected_at DESC);
"""

DEFAULT_TEMPLATE = """\
Ты — редактор Telegram-канала с новостями. Перепиши новость на русском языке.

Требования:
- 2-4 коротких предложения, только факты из источника;
- ничего не выдумывай, не добавляй оценок и воды;
- без markdown и без ссылок в тексте;
- в конце — 2-3 тематических хештега.

Новость:
Заголовок: {title}
Источник: {source}
Дата: {published}
Текст: {summary}"""

DEFAULT_FORMAT = """\
{ai}

<a href="{link}">Источник</a>"""

DEFAULTS: dict[str, str] = {
    "template": DEFAULT_TEMPLATE,
    "post_format": DEFAULT_FORMAT,
    "interval": "15",          # минуты между проверками
    "max_per_cycle": "3",      # сколько новостей максимум брать из одной ленты за проход
    "post_delay": "5",         # пауза между публикациями, сек
    "backfill": "1",           # сколько новостей опубликовать при первом опросе новой ленты
    "max_age_days": "7",       # новости старше — не публиковать (0 = публиковать любые)
    "flood_guard": "15",       # столько новых записей разом — считать выдачу ленты сбитой
    "channel_id": "",          # переопределяет CHANNEL_ID из .env
    "model": "",               # переопределяет LLM_MODEL из .env (пусто = из .env)
    "paused": "0",             # глобальная пауза публикаций
    # skip = отложить до следующего прохода (новость не помечается прочитанной
    # и будет обработана снова), raw = опубликовать исходник без обработки.
    # По умолчанию skip: необработанная новость в канале хуже её отсутствия.
    "on_llm_error": "skip",
    "require_russian": "1",    # ответ не на русском считать отказом модели
    "disable_preview": "0",
    "images": "1",             # прикладывать картинку из новости к посту
    "og_image": "1",           # если в ленте картинки нет — взять со страницы новости
    "max_images": "6",         # сколько картинок скачивать за раз, если у ленты
                               # включено «несколько картинок» (feeds.multi_images, 1-10)
    "vk_enabled": "1",         # дублировать посты в VK, если задан VK_TOKEN
    "vk_group_id": "",         # переопределяет VK_GROUP_ID из .env
    "claude_mode": "0",        # обработка через платный Claude вместо LLM_* из .env
    "gemini_mode": "0",        # обработка через Gemini вместо LLM_* из .env (обычно бесплатно);
                               # взаимоисключим с claude_mode — включение одного гасит другой
    "keep_seen": "500",        # сколько отметок хранить на ленту
    "debug": "0",              # отладка: посты уходят в личку админам, а не в канал
    "alert_thresholds": "70,90",  # при каком % расхода лимита предупреждать
    "free_daily_limit": "0",   # суточный лимит запросов; 0 = определить автоматически
    "dedup_enabled": "1",      # не публиковать новость, похожую на уже опубликованную с другой ленты
    "dedup_window_days": "3",  # за сколько последних дней сравнивать посты
    "dedup_threshold": "55",   # % схожести (заголовок+summary), после которого считаем дублем
}

# Лимиты бесплатных моделей OpenRouter (значения из их документации).
FREE_RPD_NO_CREDITS = 50     # если кредитов куплено меньше порога
FREE_RPD_WITH_CREDITS = 1000  # если куплено 10+ кредитов
FREE_RPM = 20


def entry_key(*parts: str) -> str:
    """Стабильный идентификатор записи ленты."""
    raw = "\x00".join(p for p in parts if p)
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:32]


class Storage:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            # Потолок для WAL: после крупной транзакции файл ужимается обратно,
            # а не остаётся раздутым до перезапуска.
            self._conn.execute("PRAGMA journal_size_limit=4194304")
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._migrate_settings_keys()
            self._migrate_multi_images_to_feeds()
            self._conn.commit()

    def _migrate(self) -> None:
        """Добавляет колонки, появившиеся после создания базы.

        CREATE TABLE IF NOT EXISTS не меняет существующую таблицу, поэтому
        новые поля доносим руками — иначе обновление ломает старую базу.
        """
        added: dict[str, list[tuple[str, str]]] = {
            "feeds": [("pending", "INTEGER NOT NULL DEFAULT 0"),
                      ("multi_images", "INTEGER NOT NULL DEFAULT 0")],
            "posts": [("extra_message_ids", "TEXT NOT NULL DEFAULT ''")],
        }
        for table, columns in added.items():
            have = {
                row["name"]
                for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for name, decl in columns:
                if name not in have:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def _migrate_multi_images_to_feeds(self) -> None:
        """«Несколько картинок» была общей настройкой на всех, теперь это
        свойство каждой ленты. Кто уже включил её глобально — не должен
        молча потерять картинки после обновления: разово переносим на все
        существующие ленты, затем сам глобальный ключ удаляем (разовое
        дело — как в _migrate_settings_keys, старый ключ больше не нужен
        даже если переносить было нечего)."""
        row = self._conn.execute(
            "SELECT value FROM settings WHERE key = 'multi_images'"
        ).fetchone()
        if row is not None:
            if row["value"] == "1":
                self._conn.execute("UPDATE feeds SET multi_images = 1")
            self._conn.execute("DELETE FROM settings WHERE key = 'multi_images'")

    def _migrate_settings_keys(self) -> None:
        """Переименования ключей в settings, случившиеся после того, как ими
        кто-то уже мог попользоваться — иначе на старой базе значение молча
        подменялось бы дефолтом из DEFAULTS. Разовое дело: старый ключ каждый
        раз удаляется, даже если переносить было уже нечего."""
        renames = {"claude_max_images": "max_images"}  # картинки альбомом стали общей настройкой
        for old, new in renames.items():
            row = self._conn.execute("SELECT value FROM settings WHERE key = ?", (old,)).fetchone()
            if row is not None:
                exists = self._conn.execute("SELECT 1 FROM settings WHERE key = ?", (new,)).fetchone()
                if exists is None:
                    self._conn.execute(
                        "INSERT INTO settings (key, value) VALUES (?, ?)", (new, row["value"])
                    )
                self._conn.execute("DELETE FROM settings WHERE key = ?", (old,))

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- настройки -------------------------------------------------------
    def get(self, key: str) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else DEFAULTS.get(key, "")

    def get_int(self, key: str) -> int:
        try:
            return int(self.get(key))
        except ValueError:
            return int(DEFAULTS.get(key, "0") or 0)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )
            self._conn.commit()

    # --- ленты -----------------------------------------------------------
    def add_feed(self, url: str, title: str = "") -> int | None:
        """Возвращает id новой ленты или None, если такая уже есть."""
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO feeds (url, title, added_at) VALUES (?, ?, ?)",
                    (url, title, int(time.time())),
                )
                self._conn.commit()
                return int(cur.lastrowid)
            except sqlite3.IntegrityError:
                return None

    def feeds(self, only_enabled: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM feeds"
        if only_enabled:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY id"
        with self._lock:
            return self._conn.execute(sql).fetchall()

    def feed(self, feed_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM feeds WHERE id = ?", (feed_id,)
            ).fetchone()

    def delete_feed(self, feed_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM feeds WHERE id = ?", (feed_id,))
            self._conn.execute("DELETE FROM seen WHERE feed_id = ?", (feed_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def set_enabled(self, feed_id: int, enabled: bool) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE feeds SET enabled = ? WHERE id = ?", (int(enabled), feed_id)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def set_multi_images(self, feed_id: int, enabled: bool) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE feeds SET multi_images = ? WHERE id = ?", (int(enabled), feed_id)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def update_feed(self, feed_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE feeds SET {cols} WHERE id = ?", (*fields.values(), feed_id)
            )
            self._conn.commit()

    # --- дедупликация ----------------------------------------------------
    def is_seen(self, feed_id: int, key: str) -> bool:
        with self._lock:
            return (
                self._conn.execute(
                    "SELECT 1 FROM seen WHERE feed_id = ? AND key = ?", (feed_id, key)
                ).fetchone()
                is not None
            )

    def mark_seen(self, feed_id: int, keys: list[str] | str) -> None:
        if isinstance(keys, str):
            keys = [keys]
        if not keys:
            return
        now = int(time.time())
        with self._lock:
            self._conn.executemany(
                "INSERT OR IGNORE INTO seen (feed_id, key, seen_at) VALUES (?, ?, ?)",
                [(feed_id, k, now) for k in keys],
            )
            self._conn.commit()

    def seen_count(self, feed_id: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM seen WHERE feed_id = ?", (feed_id,)
            ).fetchone()
        return int(row["n"])

    # --- кэш картинок со страниц ------------------------------------------
    def page_image(self, url: str) -> str | None:
        """None — страницу ещё не смотрели; "" — смотрели, картинки нет."""
        with self._lock:
            row = self._conn.execute(
                "SELECT image FROM page_image WHERE url = ?", (url,)
            ).fetchone()
        return row["image"] if row else None

    def set_page_image(self, url: str, image: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO page_image (url, image, checked_at) VALUES (?, ?, ?) "
                "ON CONFLICT(url) DO UPDATE SET image = excluded.image, "
                "checked_at = excluded.checked_at",
                (url, image, int(time.time())),
            )
            self._conn.commit()

    def prune_page_images(self, keep: int = 500) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM page_image WHERE url NOT IN "
                "(SELECT url FROM page_image ORDER BY checked_at DESC LIMIT ?)",
                (keep,),
            )
            self._conn.commit()

    # --- учёт расхода LLM ------------------------------------------------
    def bump_usage(self, day: str, tokens_in: int = 0, tokens_out: int = 0,
                   cost: float = 0.0) -> int:
        """Плюс один запрос за день `day`. Возвращает новое число запросов."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO usage (day, requests, tokens_in, tokens_out, cost) "
                "VALUES (?, 1, ?, ?, ?) "
                "ON CONFLICT(day) DO UPDATE SET "
                "  requests   = requests + 1,"
                "  tokens_in  = tokens_in + excluded.tokens_in,"
                "  tokens_out = tokens_out + excluded.tokens_out,"
                "  cost       = cost + excluded.cost",
                (day, tokens_in, tokens_out, cost),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT requests FROM usage WHERE day = ?", (day,)
            ).fetchone()
        return int(row["requests"]) if row else 0

    def usage(self, day: str) -> dict[str, float]:
        with self._lock:
            row = self._conn.execute(
                "SELECT requests, tokens_in, tokens_out, cost FROM usage WHERE day = ?",
                (day,),
            ).fetchone()
        if not row:
            return {"requests": 0, "tokens_in": 0, "tokens_out": 0, "cost": 0.0}
        return {
            "requests": int(row["requests"]),
            "tokens_in": int(row["tokens_in"]),
            "tokens_out": int(row["tokens_out"]),
            "cost": float(row["cost"]),
        }

    def prune_usage(self, keep_days: int = 60) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM usage WHERE day NOT IN "
                "(SELECT day FROM usage ORDER BY day DESC LIMIT ?)",
                (keep_days,),
            )
            self._conn.commit()

    # --- опубликованные посты (/posts, /edit, /setpost, /regen) -----------
    def add_post(self, *, feed_id: int | None, chat_id: str, message_id: int,
                kind: str, title: str, summary: str, link: str, source: str,
                published: str, text: str, extra_message_ids: str = "") -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO posts (feed_id, chat_id, message_id, kind, title, "
                "summary, link, source, published, text, extra_message_ids, posted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (feed_id, chat_id, message_id, kind, title, summary, link,
                 source, published, text, extra_message_ids, int(time.time())),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def posts(self, limit: int = 10) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM posts ORDER BY posted_at DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()

    def post(self, post_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM posts WHERE id = ?", (post_id,)
            ).fetchone()

    def post_extra_ids(self, post_id: int) -> list[int]:
        """message_id второй и следующих картинок альбома, по порядку.
        Пустой список — не альбом или в нём осталась только первая картинка."""
        row = self.post(post_id)
        if row is None or not row["extra_message_ids"]:
            return []
        return [int(x) for x in row["extra_message_ids"].split(",") if x]

    def remove_post_extra_id(self, post_id: int, message_id: int) -> bool:
        """Убирает одну картинку из списка альбома после её удаления в Telegram.

        Если картинок в альбоме после этого не осталось, пост становится
        обычным «фото» — первая картинка со своей подписью никуда не делась,
        разницы в редактировании между 'photo' и 'album' всё равно нет.
        Возвращает True, если id действительно был в списке.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT extra_message_ids, kind FROM posts WHERE id = ?", (post_id,)
            ).fetchone()
            if row is None:
                return False
            ids = [int(x) for x in (row["extra_message_ids"] or "").split(",") if x]
            if message_id not in ids:
                return False
            ids.remove(message_id)
            new_kind = "photo" if not ids and row["kind"] == "album" else row["kind"]
            self._conn.execute(
                "UPDATE posts SET extra_message_ids = ?, kind = ?, edited_at = ? WHERE id = ?",
                (",".join(str(i) for i in ids), new_kind, int(time.time()), post_id),
            )
            self._conn.commit()
            return True

    def update_post_text(self, post_id: int, text: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE posts SET text = ?, edited_at = ? WHERE id = ?",
                (text, int(time.time()), post_id),
            )
            self._conn.commit()

    def prune_posts(self, keep: int = 500) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM posts WHERE id NOT IN "
                "(SELECT id FROM posts ORDER BY posted_at DESC, id DESC LIMIT ?)",
                (keep,),
            )
            self._conn.commit()

    def recent_posts(self, since_ts: int) -> list[sqlite3.Row]:
        """title/summary опубликованных постов за последние since_ts секунд —
        база для сравнения на схожесть с новыми записями (см. dedup)."""
        with self._lock:
            return self._conn.execute(
                "SELECT id, title, summary FROM posts WHERE posted_at >= ?", (since_ts,)
            ).fetchall()

    # --- дубли между лентами (/duplicates в веб-панели) --------------------
    def add_dedup_candidate(self, *, feed_id: int | None, title: str, summary: str,
                            link: str, source: str, published: str, image: str,
                            matched_post_id: int | None, score: float) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO dedup_candidates (feed_id, title, summary, link, source, "
                "published, image, matched_post_id, score, detected_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (feed_id, title, summary, link, source, published, image,
                 matched_post_id, score, int(time.time())),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def dedup_candidates(self, limit: int = 50) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM dedup_candidates ORDER BY detected_at DESC LIMIT ?", (limit,)
            ).fetchall()

    def dedup_candidate(self, candidate_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM dedup_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()

    def delete_dedup_candidate(self, candidate_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM dedup_candidates WHERE id = ?", (candidate_id,))
            self._conn.commit()

    def count_dedup_candidates(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM dedup_candidates").fetchone()
            return int(row["n"]) if row else 0

    def prune_dedup_candidates(self, keep_days: int = 30) -> None:
        """Дубли, которые никто не разобрал месяц, уже неактуальны — чистим,
        чтобы очередь не росла вечно, если админ туда не заглядывает."""
        cutoff = int(time.time() - keep_days * 86400)
        with self._lock:
            self._conn.execute("DELETE FROM dedup_candidates WHERE detected_at < ?", (cutoff,))
            self._conn.commit()

    def drop_alerts_except(self, day: str) -> None:
        """Чистим отметки об отправленных предупреждениях за прошлые дни."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM settings WHERE key LIKE 'alerted:%' AND key NOT LIKE ?",
                (f"alerted:{day}:%",),
            )
            self._conn.commit()

    def maintain(self, keep_seen: int, keep_usage_days: int = 60) -> dict[str, int]:
        """Периодическая уборка: обрезает таблицы и ужимает WAL.

        Без неё отметки о прочитанном подчищались только у тех лент, которые
        только что опубликовались, а WAL после крупных транзакций так и
        оставался раздутым до перезапуска.
        """
        before = self.db_bytes()
        for row in self.feeds():
            self.prune_seen(row["id"], keep_seen)
        self.prune_usage(keep_usage_days)
        self.prune_page_images()
        self.prune_posts()
        self.prune_dedup_candidates()
        with self._lock:
            # TRUNCATE возвращает файл WAL к нулю, а не просто помечает
            # содержимое переиспользуемым.
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._conn.execute("PRAGMA optimize")
            self._conn.commit()
        return {"before": before, "after": self.db_bytes()}

    def db_bytes(self) -> int:
        """Размер базы вместе с WAL — то, что реально занято на диске."""
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += (self._path.parent / (self._path.name + suffix)).stat().st_size
            except OSError:
                pass
        return total

    def prune_seen(self, feed_id: int, keep: int) -> None:
        """Оставляем только последние `keep` отметок, чтобы база не пухла."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM seen WHERE feed_id = ? AND key NOT IN ("
                "  SELECT key FROM seen WHERE feed_id = ? ORDER BY seen_at DESC LIMIT ?"
                ")",
                (feed_id, feed_id, keep),
            )
            self._conn.commit()
