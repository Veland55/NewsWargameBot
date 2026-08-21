"""Публикация постов на стену сообщества VK.

Работает с ключом доступа сообщества (Управление → Работа с API →
Создать ключ, права «Стена» и «Фотографии»).

Картинка — отдельная история. Прямую загрузку фото на стену VK ключу
сообщества не разрешает: photos.getWallUploadServer и photos.getUploadServer
отвечают ошибкой 27 «method is unavailable with group auth». Поэтому:

1. VK_USER_TOKEN, если задан — штатная загрузка, фото видно в записи;
2. иначе ссылка на новость вложением — VK соберёт карточку из og:image
   источника; если и её он не примет, запись уходит голым текстом.

Проверенный тупик: фото можно загрузить ключом сообщества через
photos.getMessagesUploadServer, и wall.post такое вложение принимает без
ошибки — но в опубликованной записи оно не отображается. Приёмка вложения
API не означает, что оно видно; повторять этот путь не стоит.
"""
from __future__ import annotations

import asyncio
import html as html_mod
import json
import logging
import re

import aiohttp

from .rss import PAGE_UA

log = logging.getLogger(__name__)

API_URL = "https://api.vk.com/method/"
API_VERSION = "5.131"
VK_TEXT_LIMIT = 16000
MAX_IMAGE_BYTES = 20 * 1024 * 1024   # ограничение VK на фото — 50 МБ, берём с запасом
MAX_PHOTOS_PER_POST = 10             # столько вложений VK принимает в одной записи

# Ошибки, которые лечатся повтором: 6 — слишком часто, 1 — временный сбой,
# 10 — внутренняя ошибка сервера VK.
RETRY_CODES = {1, 6, 10}
# 9 — флуд-контроль: тот же текст уже публиковали. Повтор не поможет.
FLOOD_CODE = 9
# 27 — метод недоступен ключу сообщества. Ключ ни при чём, нужен другой тип.
GROUP_AUTH_CODE = 27


class VKError(RuntimeError):
    pass


class VKUploadRejected(VKError):
    """Сервер загрузки вернул пустой photo.

    Отдельный тип нужен, чтобы отличать «файл не понравился» от отказа API:
    на практике это чаще всего временно — та же картинка через минуту
    загружается, — поэтому такую ошибку имеет смысл повторить.
    """


# Сигнатуры форматов, которые принимает VK. Заодно защита от страниц-заглушек,
# отданных с заголовком image/*.
MAGIC = {
    b"\xff\xd8\xff": ("jpg", "image/jpeg"),
    b"\x89PNG\r\n\x1a\n": ("png", "image/png"),
    b"GIF87a": ("gif", "image/gif"),
    b"GIF89a": ("gif", "image/gif"),
    b"RIFF": ("webp", "image/webp"),
}
UPLOAD_ATTEMPTS = 3


def sniff(data: bytes) -> tuple[str, str] | None:
    """Формат по первым байтам, а не по заголовку сервера."""
    for magic, kind in MAGIC.items():
        if data.startswith(magic):
            return kind
    return None


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


class VKClient:
    def __init__(self, token: str, group_id: str = "", user_token: str = "", *,
                 timeout: int = 60, api_version: str = API_VERSION, retries: int = 2):
        self.token = (token or "").strip()
        self.group_id = str(group_id or "").strip().lstrip("-")
        # Нужен только для загрузки фото — публикует всё равно сообщество.
        self.user_token = (user_token or "").strip()
        self.api_version = api_version
        self.retries = retries
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    # --- инфраструктура ---------------------------------------------------
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    @property
    def configured(self) -> bool:
        return bool(self.token and self.group_id.isdigit())

    @property
    def owner_id(self) -> int:
        """Стена сообщества — это отрицательный owner_id."""
        return -int(self.group_id)

    @property
    def can_upload_photo(self) -> bool:
        """Видимое в записи фото даёт только пользовательский ключ."""
        return bool(self.user_token)

    @property
    def photo_mode(self) -> str:
        return ("фото" if self.user_token
                else "карточка-ссылка (для настоящего фото нужен VK_USER_TOKEN)")

    async def _call(self, method: str, _token: str = "", **params) -> object:
        token = _token or self.token
        if not token:
            raise VKError("VK_TOKEN не задан")
        payload = {k: str(v) for k, v in params.items() if v not in (None, "")}
        payload["access_token"] = token
        payload["v"] = self.api_version

        last_error = "неизвестная ошибка"
        for attempt in range(1, self.retries + 2):
            try:
                session = await self._get_session()
                async with session.post(API_URL + method, data=payload) as resp:
                    body = await resp.text()
                data = json.loads(body)
                if "error" in data:
                    err = data["error"] or {}
                    code = int(err.get("error_code") or 0)
                    msg = str(err.get("error_msg") or body[:200])
                    if code == FLOOD_CODE:
                        raise VKError(f"флуд-контроль VK ({msg}) — "
                                      f"такой пост уже публиковался")
                    if code == GROUP_AUTH_CODE:
                        raise VKError(f"{method} недоступен ключу сообщества "
                                      f"— нужен пользовательский ключ "
                                      f"(VK_USER_TOKEN)")
                    if code not in RETRY_CODES:
                        raise VKError(f"{method}: ошибка {code} — {msg}")
                    last_error = f"{method}: ошибка {code} — {msg}"
                else:
                    return data.get("response")
            except asyncio.TimeoutError:
                last_error = f"{method}: таймаут запроса к VK"
            except (aiohttp.ClientError, ValueError) as exc:
                last_error = f"{method}: {type(exc).__name__}: {exc}"

            if attempt <= self.retries:
                delay = 3 * attempt
                log.warning("VK: %s, повтор через %ss", last_error, delay)
                await asyncio.sleep(delay)

        raise VKError(last_error)

    # --- публичный API ----------------------------------------------------
    async def group_name(self) -> str:
        """Проверка ключа: заодно возвращает название сообщества."""
        resp = await self._call("groups.getById", group_id=self.group_id)
        # 5.131 отдаёт список, свежие версии — {"groups": [...]}.
        groups = resp.get("groups") if isinstance(resp, dict) else resp
        if isinstance(groups, list) and groups:
            return str(groups[0].get("name") or "")
        return ""

    async def post(self, text: str, image: str = "", link: str = "",
                   images: list[tuple[bytes, str]] | None = None) -> int | None:
        """Публикует запись на стену. Возвращает id записи.

        `images` — уже скачанные байты нескольких картинок (режим «несколько
        картинок» ленты, см. Publisher._images_of_page): грузим их все,
        запись выходит с галереей, как и в Telegram-альбоме. Без него —
        `image`, одна картинка по ссылке (как раньше). `link` — адрес
        новости: он идёт вложением, когда настоящее фото загрузить нечем.
        VK сам вытянет из страницы картинку и заголовок, так что запись не
        остаётся голым текстом.
        """
        if not self.configured:
            raise VKError("VK не настроен: нужны VK_TOKEN и VK_GROUP_ID")

        attachments: list[str] = []
        if images and self.user_token:
            for i, (data, _ctype) in enumerate(images[:MAX_PHOTOS_PER_POST], start=1):
                try:
                    attachments.append(await self._upload_photo_bytes(data))
                except (VKError, aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                    # Одна неудачная картинка — не повод терять остальные и
                    # тем более всю новость.
                    log.warning("VK: картинка %s из %s не загрузилась (%s) — пропускаю",
                                i, len(images), exc)
        elif image and self.user_token:
            try:
                att = await self._upload_photo(image, referer=link)
                if att:
                    attachments.append(att)
            except (VKError, aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                # Картинка — не повод терять новость. Адрес пишем в журнал:
                # без него потом не понять, на какой именно картинке сорвалось.
                log.warning("VK: картинку загрузить не удалось (%s) — "
                            "пробую вложить ссылку; картинка: %s", exc, image[:150])

        attachment = ",".join(attachments)
        if not attachment and link:
            attachment = link

        message = text[:VK_TEXT_LIMIT]
        try:
            resp = await self._call("wall.post", owner_id=self.owner_id,
                                    from_group=1, message=message,
                                    attachments=attachment)
        except VKError as exc:
            # Вложение VK не устроило — чаще всего это ссылка, из которой он
            # не смог собрать карточку (link_photo_sizing_rule). Новость из-за
            # оформления терять нельзя: публикуем голым текстом.
            if not attachment or not self._is_attachment_error(exc):
                raise
            log.warning("VK: вложение отклонено (%s) — публикую без него", exc)
            resp = await self._call("wall.post", owner_id=self.owner_id,
                                    from_group=1, message=message)

        post_id = resp.get("post_id") if isinstance(resp, dict) else None
        return int(post_id) if post_id else None

    @staticmethod
    def _is_attachment_error(exc: VKError) -> bool:
        text = str(exc).lower()
        return "ошибка 100" in text or "attach" in text or "link" in text

    # --- загрузка фото ----------------------------------------------------
    async def _download(self, url: str, referer: str = "") -> tuple[bytes, str]:
        # Заголовки браузера обязательны: часть CDN на запрос без них отвечает
        # 451/403. Referer подставляем страницу новости — некоторые хостинги
        # отдают картинку только «со своего» сайта.
        headers = {
            "User-Agent": PAGE_UA,
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        if referer:
            headers["Referer"] = referer
        session = await self._get_session()
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                raise VKError(f"картинка недоступна: HTTP {resp.status}")
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
            if ctype and not ctype.startswith("image/"):
                raise VKError(f"по адресу не картинка, а {ctype}")
            # Читаем именно до конца потока. resp.content.read(N) вернул бы
            # первую подошедшую порцию, а не N байт: крупные картинки уходили
            # в VK обрезанными, и он их отвергал.
            chunks: list[bytes] = []
            size = 0
            async for chunk in resp.content.iter_chunked(64 * 1024):
                size += len(chunk)
                if size > MAX_IMAGE_BYTES:
                    raise VKError("картинка больше 20 МБ")
                chunks.append(chunk)
        data = b"".join(chunks)
        if not data:
            raise VKError("пустой ответ")
        # Заголовок Content-Length врёт редко, но обрыв соединения бывает:
        # неполный файл VK всё равно не примет, лучше сказать об этом внятно.
        declared = resp.headers.get("Content-Length")
        if declared and declared.isdigit() and len(data) < int(declared):
            raise VKError(f"картинка скачалась не полностью "
                          f"({len(data)} из {declared} Б)")
        return data, ctype or "image/jpeg"

    async def _push_file(self, upload_url: str, data: bytes, ext: str,
                         ctype: str) -> dict:
        form = aiohttp.FormData()
        form.add_field("photo", data, filename=f"image.{ext}", content_type=ctype)
        session = await self._get_session()
        async with session.post(upload_url, data=form) as resp:
            status = resp.status
            body = await resp.text()
        try:
            uploaded = json.loads(body)
        except ValueError as exc:
            # Сервер загрузки изредка отдаёт вместо JSON пустоту или страницу
            # ошибки. Это временное — та же картинка через минуту грузится, —
            # поэтому ошибка должна попадать под повтор, а не хоронить фото.
            raise VKUploadRejected(
                f"сервер загрузки ответил не JSON (HTTP {status}, "
                f"{body[:80]!r})") from exc
        # Пустой photo — VK файл не взял. Формат мы проверили заранее, так что
        # причина почти всегда временная.
        if not uploaded.get("photo") or uploaded.get("photo") == "[]":
            raise VKUploadRejected("сервер загрузки вернул пустой ответ")
        return uploaded

    @staticmethod
    def _attachment(item: dict) -> str:
        """photo<owner>_<id> и ключ доступа, если VK его выдал."""
        att = f"photo{item['owner_id']}_{item['id']}"
        return f"{att}_{item['access_key']}" if item.get("access_key") else att

    async def _upload_photo(self, url: str, referer: str = "") -> str:
        """Скачиваем картинку и кладём на стену: по ссылке VK её не берёт.

        Требует пользовательского ключа — см. пояснение в начале модуля.
        """
        if not self.user_token:
            raise VKError("нет VK_USER_TOKEN")
        data, _ctype = await self._download(url, referer)
        return await self._upload_photo_bytes(data)

    async def _upload_photo_bytes(self, data: bytes) -> str:
        """Кладёт на стену уже скачанные байты — не заново качает картинку,
        если она уже была скачана для другого адресата (Telegram-альбом,
        см. Publisher._images_of_page)."""
        if not self.user_token:
            raise VKError("нет VK_USER_TOKEN")
        kind = sniff(data)
        if kind is None:
            raise VKError(f"это не картинка: первые байты {data[:8].hex()}")
        ext, ctype = kind

        last: Exception | None = None
        for attempt in range(1, UPLOAD_ATTEMPTS + 1):
            try:
                return await self._upload_once(data, ext, ctype)
            # Обрыв связи и таймаут — такие же временные помехи, как отказ
            # сервера загрузки: повторяем их наравне.
            except (VKUploadRejected, aiohttp.ClientError,
                    asyncio.TimeoutError) as exc:
                last = exc
                if attempt < UPLOAD_ATTEMPTS:
                    log.warning("VK: %s (%s, %s КБ) — попытка %s из %s",
                                exc or type(exc).__name__, ext,
                                len(data) // 1024, attempt + 1, UPLOAD_ATTEMPTS)
                    await asyncio.sleep(2 * attempt)
        raise VKError(f"картинка не загрузилась за {UPLOAD_ATTEMPTS} попытки: "
                      f"{last}") from last

    async def _upload_once(self, data: bytes, ext: str, ctype: str) -> str:
        # Адрес загрузки берём заново на каждую попытку: он одноразовый.
        server = await self._call("photos.getWallUploadServer",
                                  _token=self.user_token, group_id=self.group_id)
        upload_url = server.get("upload_url") if isinstance(server, dict) else None
        if not upload_url:
            raise VKError("VK не выдал адрес для загрузки фото")
        uploaded = await self._push_file(upload_url, data, ext, ctype)
        saved = await self._call(
            "photos.saveWallPhoto",
            _token=self.user_token,
            group_id=self.group_id,
            server=uploaded.get("server"),
            photo=uploaded.get("photo"),
            hash=uploaded.get("hash"),
        )
        if not isinstance(saved, list) or not saved:
            raise VKUploadRejected("VK не сохранил картинку")
        return self._attachment(saved[0])
