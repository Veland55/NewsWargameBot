"""Общие фикстуры для тестов бота.

Никаких настоящих сетевых вызовов — Telegram/LLM/VK всегда мокаются
(unittest.mock). Storage — временная SQLite-база в tmp_path, никогда не
настоящий файл проекта: тесты должны быть независимы друг от друга и не
портить рабочую data/bot.db.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from bot.db import Storage
from bot.rss import Entry


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    st = Storage(tmp_path / "test.db")
    yield st
    st.close()


def make_entry(*, key: str = "k1", title: str = "Заголовок",
               link: str = "https://example.com/a", summary: str = "Текст новости",
               published: str = "2024-01-01 00:00 UTC", published_ts: float = 0,
               image: str = "") -> Entry:
    """Короткий конструктор Entry с разумными дефолтами — большинству тестов
    важны только 1-2 конкретных поля, писать все семь каждый раз незачем."""
    return Entry(key_parts=(key,), title=title, link=link, summary=summary,
                published=published, published_ts=published_ts, image=image)
