"""
Загрузка видео в Telegram через Pyrogram.

После скачивания отправляет файл в выбранный диалог.
Если файл превышает лимит — нарезает через splitter.
"""

import asyncio
import contextlib
import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

import cv2
from pyrogram import Client
from pyrogram.errors import FloodWait, PeerIdInvalid, SlowmodeWait
from pyrogram.types import Message

from config.settings import get as cfg_get
from telegram.splitter import split_video

logger = logging.getLogger(__name__)

_NETWORK_ERRORS = (ConnectionError, TimeoutError, OSError)
_FLOOD_MAX_WAIT_S = 300
_FLOOD_TOTAL_LIMIT_S = 1800


class TelegramOfflineError(Exception):
    def __init__(self, *args, backoff_s: Optional[float] = None):
        super().__init__(*args)
        self.backoff_s = backoff_s


class UploadSendError(Exception):
    """
    Постоянная ошибка Telegram при отправке (peer/media/RPC).
    Файл уже скачан, но отправить его сейчас нельзя. Молча терять такой файл
    нельзя: задача должна стать BLOCKED (видно в UI, ряд останавливается,
    доступен ручной ретрай), а НЕ «успешной без отправки».
    """


def _fit_box(w: int, h: int, max_w: int, max_h: int) -> tuple[int, int]:
    if w <= max_w and h <= max_h:
        return w, h
    scale = min(max_w / w, max_h / h)
    return max(1, int(round(w * scale))), max(1, int(round(h * scale)))


def _crop_cover(img, ar_w: int, ar_h: int):
    if ar_w <= 0 or ar_h <= 0 or img is None:
        return img
    h, w = img.shape[:2]
    target = ar_w / ar_h
    cur = w / h
    if target > cur:
        nh = max(1, int(w / target))
        y0 = (h - nh) // 2
        return img[y0:y0 + nh]
    if target < cur:
        nw = max(1, int(h * target))
        x0 = (w - nw) // 2
        return img[:, x0:x0 + nw]
    return img


def _thumb_from_img(img, out: Path, aspect: Optional[tuple[int, int]] = None,
                    max_side: int = 320, max_bytes: int = 200 * 1024) -> Optional[Path]:
    if img is None:
        return None
    img = _crop_cover(img, *aspect) if aspect else img
    h, w = img.shape[:2]
    if not w or not h:
        return None
    tw, th = _fit_box(w, h, max_side, max_side)
    for q in (85, 70, 55, 40):
        resized = cv2.resize(img, (tw, th), interpolation=cv2.INTER_AREA)
        if not cv2.imwrite(str(out), resized, [cv2.IMWRITE_JPEG_QUALITY, q]):
            continue
        if out.exists() and out.stat().st_size <= max_bytes:
            return out
        out.unlink(missing_ok=True)
    logger.warning("Превью не влезло в %d КБ — не передаю: %s", max_bytes // 1024, out)
    return None


def _poster_thumb(poster: Path, out: Path, aspect: Optional[tuple[int, int]]) -> Optional[Path]:
    try:
        img = cv2.imread(str(poster), cv2.IMREAD_COLOR)
        return _thumb_from_img(img, out, aspect=aspect)
    except Exception as e:
        logger.debug("Ошибка превью из постера (%s): %s", poster.name, e)
        return None


def _frame_thumb(video: Path, out: Path, aspect: Optional[tuple[int, int]]) -> Optional[Path]:
    try:
        cap = cv2.VideoCapture(str(video))
        if not cap.isOpened():
            logger.debug("Не открыть видео → нет кадр-превью: %s", video)
            return None
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        target = int(n * 0.1) if n > 1 else 0
        cap.set(cv2.CAP_PROP_POS_FRAMES, target)
        ok, frame = cap.read()
        cap.release()
        if not ok or frame is None:
            logger.debug("Нет кадра → нет кадр-превью: %s", video)
            return None
        return _thumb_from_img(frame, out, aspect=aspect)
    except Exception as e:
        logger.debug("Ошибка кадр-превью (%s): %s", video, e)
        return None


def _prepare_thumb(video: Path, poster: Optional[Path]) -> Optional[Path]:
    vw, vh, _ = _get_video_info(video)
    aspect = (vw, vh) if vw and vh else None
    if poster and poster.exists() and poster.is_file():
        gen = poster.parent / (poster.stem + "_th.jpg")
        if _poster_thumb(poster, gen, aspect):
            return gen
    gen = video.parent / (video.stem + "_th.jpg")
    return _frame_thumb(video, gen, aspect)


def _mp4_boxes(f, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        head = f.read(16)
        if len(head) < 8:
            return
        sz = int.from_bytes(head[0:4], "big")
        typ = head[4:8]
        if sz == 1:
            if len(head) < 16:
                return
            sz = int.from_bytes(head[8:16], "big")
            hdr = 16
        elif sz == 0:
            sz = end - pos
            hdr = 8
        else:
            hdr = 8
        if sz < hdr:
            return
        yield typ, sz, pos, hdr
        if pos + sz >= end:
            return
        pos += sz


def _trak_display_size(f, start: int, end: int) -> Optional[tuple[int, int]]:
    for typ, sz, pos, hdr in _mp4_boxes(f, start, end):
        if typ != b"tkhd" or sz - hdr < 84:
            continue
        f.seek(pos + hdr)
        body = f.read(sz - hdr)
        ver = body[0] if body else 0
        if ver == 1:
            woff, hoff = 88, 92
        else:
            woff, hoff = 76, 80
        if woff + 4 > len(body) or hoff + 4 > len(body):
            continue
        w = int.from_bytes(body[woff:woff + 4], "big") >> 16
        h = int.from_bytes(body[hoff:hoff + 4], "big") >> 16
        if w > 1 and h > 1:
            return w, h
    return None


def _parse_mp4_display_size(file_path: Path) -> Optional[tuple[int, int]]:
    try:
        with open(file_path, "rb") as f:
            f.seek(0, 2)
            file_end = f.tell()
            moov = None
            for typ, sz, pos, hdr in _mp4_boxes(f, 0, file_end):
                if typ == b"moov":
                    moov = (pos + hdr, pos + sz)
                    break
            if moov is None:
                return None
            for typ, sz, pos, hdr in _mp4_boxes(f, *moov):
                if typ == b"trak":
                    res = _trak_display_size(f, pos + hdr, pos + sz)
                    if res:
                        return res
    except Exception as e:
        logger.debug("mp4 tkhd parse (%s): %s", file_path.name, e)
    return None


def _cv2_duration(file_path: str | Path) -> Optional[int]:
    try:
        cap = cv2.VideoCapture(str(file_path))
        fps = cap.get(cv2.CAP_PROP_FPS)
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        cap.release()
        if fps > 0 and frame_count > 0:
            return int(frame_count / fps)
    except Exception:
        pass
    return None


def _get_video_info(file_path: str | Path) -> tuple[Optional[int], Optional[int], Optional[int]]:
    p = Path(file_path)

    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height,duration,sample_aspect_ratio",
                "-show_entries", "stream_side_data=rotation",
                "-show_entries", "format=duration",
                "-of", "json",
                str(p),
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            info = json.loads(result.stdout)
            streams = info.get("streams") or []
            if streams:
                s = streams[0]
                w = s.get("width")
                h = s.get("height")
                if w and h:
                    sar = s.get("sample_aspect_ratio", "1:1")
                    try:
                        sn, sd = (float(x) for x in sar.split(":"))
                        if sd and sn != sd:
                            w = int(round(w * sn / sd))
                    except (ValueError, ZeroDivisionError):
                        pass
                    rot = 0
                    for sd_ in s.get("side_data_list") or []:
                        if "rotation" in sd_:
                            try:
                                rot = int(float(sd_["rotation"])) % 360
                            except (ValueError, TypeError):
                                pass
                    if rot in (90, 270):
                        w, h = h, w
                    dur = s.get("duration")
                    if not dur:
                        dur = (info.get("format") or {}).get("duration")
                    try:
                        dur = int(float(dur)) if dur else None
                    except (TypeError, ValueError):
                        dur = None
                    return w, h, dur
    except Exception as e:
        logger.debug("ffprobe не дал размеры (%s): %s", p.name, e)

    ds = _parse_mp4_display_size(p)
    if ds is not None:
        w, h = ds
        return w, h, _cv2_duration(p)

    try:
        cap = cv2.VideoCapture(str(p))
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = cap.get(cv2.CAP_PROP_FPS)
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        cap.release()
        dur = int(frame_count / fps) if fps > 0 and frame_count > 0 else None
        if w > 0 and h > 0:
            return w, h, dur
    except Exception as e:
        logger.debug("Не удалось получить информацию о видео (cv2): %s", e)
    return None, None, None



async def upload_file(
    client: Client,
    file_path: str | Path,
    chat_id: Optional[int | str] = None,
    progress_callback: Optional[Callable] = None,
    thumb_path: Optional[str | Path] = None,
    caption: Optional[str] = None,
) -> list[Message]:
    dest = Path(file_path)
    if not dest.exists():
        logger.error("Файл не найден: %s", file_path)
        return []

    if chat_id is None:
        chat_id = cfg_get("telegram.target_dialog", "")
    if not chat_id:
        logger.warning("Не указан чат для отправки")
        return []

    split_mb = cfg_get("telegram.split_mb", 1900)
    file_size_mb = dest.stat().st_size / (1024 * 1024)

    poster = Path(thumb_path) if thumb_path else None
    made_thumb: Optional[Path] = None
    try:
        made_thumb = await asyncio.to_thread(_prepare_thumb, dest, poster)
        send_thumb = str(made_thumb) if made_thumb else None

        messages = []

        if file_size_mb > split_mb:
            logger.info("Файл %s (%.1f МБ) превышает лимит %d МБ, нарезаю...",
                        dest.name, file_size_mb, split_mb)
            parts = await asyncio.to_thread(split_video, str(dest), split_mb)

            for i, part_path in enumerate(parts):
                msg = await _send_with_retry(client, part_path, chat_id, i + 1, len(parts),
                                             thumb_path=send_thumb, caption=caption)
                if not msg:
                    raise UploadSendError(f"Часть {i + 1}/{len(parts)} не отправлена")
                messages.append(msg)
        else:
            msg = await _send_with_retry(client, str(dest), chat_id, thumb_path=send_thumb, caption=caption)
            if not msg:
                raise UploadSendError(f"Файл не отправлен: {dest.name}")
            messages.append(msg)

        return messages
    finally:
        if made_thumb and made_thumb.exists():
            try:
                made_thumb.unlink()
            except Exception:
                pass


async def _send_with_retry(
    client: Client, file_path: str, chat_id: int | str,
    part_num: int = 0, total_parts: int = 0,
    thumb_path: Optional[str | Path] = None, caption: Optional[str] = None,
) -> Optional[Message]:
    offline_wait = float(cfg_get("telegram.upload_offline_wait_s", 90))
    deadline = time.monotonic() + offline_wait
    while client is None or not getattr(client, "is_connected", False):
        if time.monotonic() >= deadline:
            raise TelegramOfflineError(
                f"Telegram оффлайн {offline_wait:.0f} с — откладываю {Path(file_path).name}"
            )
        await asyncio.sleep(5)

    retries = int(cfg_get("telegram.upload_retries", 3))
    for attempt in range(1, retries + 1):
        try:
            return await _send_single(
                client, file_path, chat_id, part_num, total_parts,
                thumb_path=thumb_path, caption=caption,
            )
        except _NETWORK_ERRORS as e:
            if attempt == retries:
                if not getattr(client, "is_connected", False):
                    raise TelegramOfflineError(
                        f"Telegram отвалился при отправке {Path(file_path).name}: {e}"
                    )
                logger.error(
                    "Сетевые обрывы при отправке %s (%d попыток): %s",
                    Path(file_path).name, retries, e,
                )
                return None
            wait = attempt * 15
            logger.warning(
                "Сетевая ошибка при отправке %s (попытка %d/%d): %s. Жду %d с…",
                Path(file_path).name, attempt, retries, e, wait,
            )
            await asyncio.sleep(wait)
    return None


async def _upload_with_watch(
    client: Client,
    kwargs: dict,
    file_path: str,
) -> Message:
    stall_s = float(cfg_get("telegram.upload_stall_s", 90))
    probe_s = float(cfg_get("telegram.upload_probe_s", 60))
    min_total = float(cfg_get("telegram.upload_min_total_mb", 5.0)) * 1024 * 1024
    backoff = float(cfg_get("telegram.upload_flood_backoff_s", 600))

    state = {"start": time.monotonic(), "ts": time.monotonic(), "sent": 0}

    def _on_progress(current: int, total: int) -> None:
        state["ts"] = time.monotonic()
        state["sent"] = current

    task = asyncio.create_task(client.send_video(**kwargs, progress=_on_progress))
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=5)
            if done:
                exc = task.exception()
                if exc:
                    raise exc
                return task.result()
            now = time.monotonic()
            if now - state["ts"] > stall_s:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
                raise TelegramOfflineError(
                    f"Загрузка {Path(file_path).name} «зависла»: {stall_s:.0f} с без "
                    f"прогресса (устойчивый FloodWait SaveBigFilePart). Откладываю — "
                    f"бэкофф {backoff:.0f} с.",
                    backoff_s=backoff,
                )
            if now - state["start"] > probe_s and state["sent"] < min_total:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
                raise TelegramOfflineError(
                    f"Загрузка {Path(file_path).name} за {probe_s:.0f} с залила только "
                    f"{state['sent'] / 1024 / 1024:.1f} МБ (< {min_total / 1024 / 1024:.0f} МБ) — "
                    f"упорный чанк-флуд. Откладываю — бэкофф {backoff:.0f} с.",
                    backoff_s=backoff,
                )
    finally:
        if not task.done():
            task.cancel()


async def _send_single(
    client: Client,
    file_path: str,
    chat_id: int | str,
    part_num: int = 0,
    total_parts: int = 0,
    thumb_path: Optional[str | Path] = None,
    caption: Optional[str] = None,
) -> Optional[Message]:
    if not caption:
        caption_text = Path(file_path).name
    elif total_parts > 1:
        caption_text = f"({part_num}/{total_parts}) {caption}"
    else:
        caption_text = caption

    kwargs = dict(
        chat_id=chat_id,
        video=file_path,
        caption=caption_text,
        supports_streaming=True,
    )

    if thumb_path and Path(thumb_path).exists():
        if total_parts <= 1 or part_num == 1:
            kwargs["thumb"] = str(thumb_path)

    if client.me is None:
        logger.warning("client.me is None — пробуем загрузить пользователя")
        try:
            await client.get_me()
        except Exception as e:
            logger.error("Не удалось загрузить me: %s", e)

    if total_parts <= 1 or part_num == 1:
        vw, vh, vdur = await asyncio.to_thread(_get_video_info, file_path)
        if vw and vh:
            kwargs["width"] = vw
            kwargs["height"] = vh
            logger.debug("Размеры видео: %dx%d", vw, vh)
        if vdur:
            kwargs["duration"] = vdur
            logger.debug("Длительность видео: %d сек", vdur)

    peer_retried = False
    flood_total = 0.0
    flood_streak = 0
    while True:
        try:
            msg = await _upload_with_watch(client, kwargs, file_path)
            flood_streak = 0
            logger.info("Отправлен %s в %s", Path(file_path).name, chat_id)
            return msg
        except (FloodWait, SlowmodeWait) as e:
            wait = min(int(e.value) + 2, _FLOOD_MAX_WAIT_S)
            flood_total += wait
            flood_streak += 1
            if flood_streak >= 3:
                wait += 30
            if flood_total > _FLOOD_TOTAL_LIMIT_S:
                logger.error(
                    "FloodWait суммарно > %d с по %s — откладываю задачу",
                    _FLOOD_TOTAL_LIMIT_S, Path(file_path).name,
                )
                backoff = float(cfg_get("telegram.upload_flood_backoff_s", 600))
                raise TelegramOfflineError(
                    f"FloodWait затянулся (> {_FLOOD_TOTAL_LIMIT_S} с суммарно) — "
                    f"откладываю {Path(file_path).name}",
                    backoff_s=backoff,
                )
            logger.warning(
                "FloodWait %d с при отправке %s — жду %d с (итого %d)",
                e.value, Path(file_path).name, wait, int(flood_total),
            )
            await asyncio.sleep(wait)
            continue
        except PeerIdInvalid:
            if peer_retried:
                logger.error("Ошибка отправки %s (после retry): %s", file_path, chat_id)
                return None
            peer_retried = True
            logger.warning("PEER_ID_INVALID — пере-резолвинг peer")
            try:
                    from pyrogram import raw, utils as pyro_utils
                    cid = int(chat_id) if not isinstance(chat_id, int) else chat_id
                    if cid < 0 and str(cid).startswith("-100"):
                        try:
                            raw_channel_id = pyro_utils.get_channel_id(cid)
                            r = await client.invoke(
                                raw.functions.channels.GetChannels(
                                    id=[raw.types.InputChannel(channel_id=raw_channel_id, access_hash=0)]
                                )
                            )
                            await client.fetch_peers(r.chats)
                            await client.resolve_peer(cid)
                            logger.info("Peer канала закэширован: %s", cid)
                        except Exception as e:
                            logger.debug("retry: не канал: %s", e)
                    if cid < 0:
                        try:
                            r = await client.invoke(raw.functions.messages.GetChats(id=[-cid]))
                            if r.chats:
                                chat = r.chats[0]
                                migrated = getattr(chat, "migrated_to", None)
                                if migrated:
                                    new_channel_id = pyro_utils.get_channel_id(migrated.channel_id)
                                    logger.info(
                                        "retry: чат %s мигрирован в супергруппу %s,"
                                        " обновляю ID", cid, new_channel_id,
                                    )
                                    r2 = await client.invoke(
                                        raw.functions.channels.GetChannels(
                                            id=[raw.types.InputChannel(
                                                channel_id=migrated.channel_id,
                                                access_hash=0,
                                            )]
                                        )
                                    )
                                    await client.fetch_peers(r2.chats)
                                    chat_id = new_channel_id
                                    kwargs["chat_id"] = new_channel_id
                                else:
                                    await client.fetch_peers(r.chats)
                                    await client.resolve_peer(cid)
                                    logger.info("Peer чата закэширован: %s", cid)
                        except Exception as e:
                            logger.debug("retry: не чат: %s", e)
                    else:
                        try:
                            r = await client.invoke(
                                raw.functions.users.GetUsers(
                                    id=[raw.types.InputUser(user_id=cid, access_hash=0)]
                                )
                            )
                            await client.fetch_peers(r)
                            await client.resolve_peer(cid)
                            logger.info("Peer пользователя закэширован: %s", cid)
                        except Exception as e:
                            logger.debug("retry: не пользователь: %s", e)
            except Exception as e:
                logger.debug("retry: исключение: %s", e)
            continue
        except _NETWORK_ERRORS:
            raise
        except Exception as e:
            logger.error("Ошибка отправки %s: %s", file_path, e)
            return None
