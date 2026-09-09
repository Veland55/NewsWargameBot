"""Временный публичный прокси для картинок VK-постов.

Публикуя запись со ссылкой-вложением, VK сам скачивает og:image со страницы
по этой ссылке — своим краулером, отдельно от нашего HTTP-клиента. Если
источник новости блокирует ботов, отдаёт картинку только по referer/своим
заголовкам или временно недоступен — VK не может собрать карточку, и запись
уходит голым текстом (см. VKClient.post: ошибка 100, link_photo_sizing_rule).
Именно это происходило с VK_USER_TOKEN, заблокированным флуд-контролем
VK на уровне аккаунта: фото не грузится, а ссылка на источник тоже не всегда
принимается краулером VK.

Мы уже скачали те же самые байты картинки для Telegram — значит, отдать их
ещё раз проблемы нет. Вместо ссылки на чужой сайт вкладываем в wall.post
ссылку на страницу НА СВОЁМ домене с заранее готовым og:image на эти же
байты: наш сервер VK не блокирует и не капризничает, а сами байты уже
проверены — тем же файлом только что публиковались в Telegram. Работает
только ключом сообщества (VK_TOKEN), без photos.* и без VK_USER_TOKEN —
не зависит ни от флуд-контроля личного токена, ни от ошибки 27 (метод
недоступен ключу сообщества).
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

_TTL_SECONDS = 6 * 3600   # с запасом: VK иногда перекрёсывает карточку не сразу
_MAX_ITEMS = 200          # столько недавних картинок держим одновременно


@dataclass
class Entry:
    data: bytes
    content_type: str
    title: str
    expires_at: float


_cache: dict[str, Entry] = {}


def put(data: bytes, content_type: str = "image/jpeg", title: str = "") -> str:
    """Кладёт байты картинки в кэш, возвращает токен для URL /vk-img/<token>."""
    _prune()
    token = uuid.uuid4().hex
    _cache[token] = Entry(data, content_type or "image/jpeg", title,
                           time.monotonic() + _TTL_SECONDS)
    return token


def get(token: str) -> Entry | None:
    entry = _cache.get(token)
    if entry is None:
        return None
    if entry.expires_at < time.monotonic():
        _cache.pop(token, None)
        return None
    return entry


def _prune() -> None:
    now = time.monotonic()
    expired = [k for k, v in _cache.items() if v.expires_at < now]
    for k in expired:
        _cache.pop(k, None)
    if len(_cache) >= _MAX_ITEMS:
        # переполнение важнее точного LRU — выкидываем самые старые по TTL
        oldest = sorted(_cache.items(), key=lambda kv: kv[1].expires_at)
        for k, _ in oldest[: len(_cache) - _MAX_ITEMS + 1]:
            _cache.pop(k, None)
