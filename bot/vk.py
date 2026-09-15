"""Текст для VK: HTML-пост Telegram → простой текст.

Используется только RSS-лентой для VK-импорта (см. web.py: /rss/vk.xml,
Storage.recent_posts_for_rss) — само сообщество публикует запись со своей
стороны, разбирая ленту (Управление сообществом → Импорт). Раньше здесь
жил VKClient, публиковавший через Wall API напрямую: VK всё сильнее
ограничивает доступ к photos.*/wall.* (ключ сообщества не может грузить
фото вовсе, личные токены либо блокируются флуд-контролем, либо давно не
выдаются с нужными правами через самостоятельную регистрацию приложения) —
прямая публикация оказалась ненадёжной структурно, не из-за конкретной
ошибки, и её заменил RSS-импорт.
"""
from __future__ import annotations

import html as html_mod
import re

VK_TEXT_LIMIT = 16000

_A_RE = re.compile(r"<a\s[^>]*href=[\"']([^\"']*)[\"'][^>]*>(.*?)</a>", re.I | re.S)
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_BLOCK_RE = re.compile(r"</(p|div|li|blockquote)\s*>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def to_plain(text: str) -> str:
    """HTML-пост для Telegram → простой текст: VK разметку не понимает.

    Ссылки не выбрасываем, а разворачиваем в текст — иначе из поста пропал бы
    адрес источника, ради которого ссылка и ставилась.
    """
    def link(match: re.Match) -> str:
        url = html_mod.unescape(match.group(1)).strip()
        label = html_mod.unescape(_TAG_RE.sub("", match.group(2))).strip()
        if not url:
            return label
        if not label or label in url or url in label:
            return url
        return f"{label}: {url}"

    out = _A_RE.sub(link, text)
    out = _BR_RE.sub("\n", out)
    out = _BLOCK_RE.sub("\n", out)
    out = _TAG_RE.sub("", out)
    out = html_mod.unescape(out)
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()[:VK_TEXT_LIMIT]
