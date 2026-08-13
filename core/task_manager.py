import asyncio
import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from core.downloader import (
    cleanup_stale_markers,
    cleanup_stale_manifests,
    download_video,
    update_manifest as update_manifest_file,
    validate_downloaded_file,
)
from core.parser import parse_episode_meta, parse_episode_page, select_quality
from core.session import get_all_cookies
from core.url_fetcher import fetch_episode_sources
from config.settings import get as cfg_get, set_value as cfg_set
from telegram.uploader import TelegramOfflineError, UploadSendError, upload_file
from utils.models import Episode, EpisodeMeta, TaskStatus, VideoSource, VideoTask

logger = logging.getLogger(__name__)

QUEUE_FILE = Path("data/queue_state.json")

ACTIVITY_MAX = 150


def _sanitize_filename(slug: str, season: int, episode_number: int, max_len: int = 50) -> str:
    safe_slug = re.sub(r'[\\/:*?"<>|]', '_', slug)
    suffix = f"_S{season:02d}E{episode_number:04d}"
    basename = f"{safe_slug}{suffix}"
    if len(basename) > max_len:
        available = max_len - len(suffix)
        if available < 1:
            available = 1
        basename = f"{safe_slug[:available]}{suffix}"
    return f"{basename}.mp4"


async def _resolve_sources(url: str) -> Optional[list[VideoSource]]:
    sources = await asyncio.to_thread(parse_episode_page, url)
    if sources:
        return sources
    browser_timeout = float(cfg_get("download.browser_timeout_s", 45))
    return await fetch_episode_sources(url, timeout_s=browser_timeout)


class TaskManager:

    def __init__(self):
        self._tasks: dict[str, VideoTask] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._sse_queues: list[asyncio.Queue] = []
        self._stop_event = asyncio.Event()
        self._last_broadcast = 0.0
        self._stub_streak = 0
        self._cooldown_until = 0.0
        self._activity: list[dict] = []

    async def start(self):
        await self._restore_queue()

        download_path = Path(cfg_get("download.path", "data/downloads"))
        cleanup_stale_markers(download_path)
        cleanup_stale_manifests(download_path)

        worker_count = min(cfg_get("download.parallel", 1), 5)
        self._workers = [
            asyncio.create_task(self._worker_loop(i))
            for i in range(worker_count)
        ]
        logger.info("TaskManager запущен (%d задач, %d воркера)", len(self._tasks), worker_count)

    async def stop(self):
        self._stop_event.set()

        if self._workers:
            done, pending = await asyncio.wait(
                self._workers, timeout=30.0,
            )
            for w in pending:
                w.cancel()
            await asyncio.gather(*self._workers, return_exceptions=True)
            self._workers.clear()

        for q in self._sse_queues:
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass
        self._sse_queues.clear()

        await self._save_queue()
        logger.info("TaskManager остановлен")


    def add_task(
        self,
        episode: Episode,
        sources: Optional[list[VideoSource]] = None,
        preferred_quality: Optional[str] = None,
    ) -> VideoTask:
        existing = self._find_task_for_episode(episode)
        if existing is not None and existing.status not in (
            TaskStatus.DONE, TaskStatus.CANCELLED,
        ):
            logger.info(
                "Задача для %s S%02dE%04d уже есть (%s) — не дублирую",
                episode.slug, episode.season, episode.episode_number,
                existing.status.value,
            )
            return existing

        task_id = uuid.uuid4().hex[:12]
        quality = preferred_quality or cfg_get("download.quality", "720p")

        task = VideoTask(
            id=task_id,
            episode=episode,
            video_sources=sources or [],
            selected_quality=quality,
            status=TaskStatus.QUEUED,
        )
        self._tasks[task_id] = task
        self._queue.put_nowait(task_id)
        self._broadcast(force=True)
        logger.info("Задача %s: %s S%02dE%04d", task_id, episode.slug, episode.season, episode.episode_number)
        self._log_activity(
            text=f"В очередь: {episode.slug} S{episode.season:02d}E{episode.episode_number:04d} ({quality})",
            task_id=task_id,
        )
        return task

    def _find_task_for_episode(self, episode: Episode) -> Optional[VideoTask]:
        for t in self._tasks.values():
            if (t.episode.slug == episode.slug
                    and t.episode.season == episode.season
                    and t.episode.episode_number == episode.episode_number):
                return t
        return None

    def get_task(self, task_id: str) -> Optional[VideoTask]:
        return self._tasks.get(task_id)

    def get_tasks(self) -> list[VideoTask]:
        return list(self._tasks.values())

    def get_active_tasks(self) -> list[VideoTask]:
        return [t for t in self._tasks.values() if t.status in (
            TaskStatus.QUEUED, TaskStatus.DOWNLOADING)]

    async def cancel_task(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if not task:
            return False
        if task.status in (TaskStatus.DONE, TaskStatus.CANCELLED):
            return False
        task.status = TaskStatus.CANCELLED
        self._broadcast(force=True)
        await self._save_queue()
        logger.info("Задача %s отменена", task_id)
        return True

    async def pause_task(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if not task or task.status != TaskStatus.DOWNLOADING:
            return False
        task.status = TaskStatus.PAUSED
        self._broadcast(force=True)
        await self._save_queue()
        logger.info("Задача %s приостановлена", task_id)
        return True

    async def resume_task(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if not task or task.status != TaskStatus.PAUSED:
            return False
        task.status = TaskStatus.QUEUED
        self._queue.put_nowait(task_id)
        self._broadcast(force=True)
        await self._save_queue()
        logger.info("Задача %s возобновлена", task_id)
        return True

    async def retry_task(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if not task:
            return False
        if task.status not in (TaskStatus.BLOCKED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            return False
        task.status = TaskStatus.QUEUED
        task.retries = 0
        task.error = None
        task.progress = 0.0
        task.downloaded_to = None
        self._queue.put_nowait(task_id)
        self._broadcast(force=True)
        await self._save_queue()
        logger.info("Задача %s перезапущена", task_id)
        return True

    async def clear_queue(self) -> int:
        removed = 0
        for task_id, task in list(self._tasks.items()):
            if task.status in (
                TaskStatus.QUEUED,
                TaskStatus.DOWNLOADING,
                TaskStatus.PAUSED,
                TaskStatus.BLOCKED,
            ):
                task.status = TaskStatus.CANCELLED
                del self._tasks[task_id]
                removed += 1

        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        await self._save_queue()
        self._broadcast(force=True)
        if removed:
            logger.info("Очередь очищена: удалено %d задач", removed)
        return removed


    async def _worker_loop(self, worker_id: int):
        while not self._stop_event.is_set():
            while self._cooldown_remaining() > 0 and not self._stop_event.is_set():
                await asyncio.sleep(min(self._cooldown_remaining(), 5.0))
                self._broadcast()
            try:
                task_id = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            task = self._tasks.get(task_id)
            if not task or task.status in (TaskStatus.CANCELLED, TaskStatus.PAUSED):
                self._queue.task_done()
                continue

            try:
                if not await self._wait_my_turn(task):
                    continue
                await self._process_task(task, worker_id)
            except Exception as e:
                logger.exception("Воркер %d: ошибка задачи %s: %s", worker_id, task_id, e)
                await self._handle_failure(task, f"Неожиданная ошибка: {e}")
            finally:
                self._queue.task_done()
                await self._save_queue()

    async def _process_task(self, task: VideoTask, worker_id: int):
        task.status = TaskStatus.DOWNLOADING
        task.error = None
        self._broadcast(force=True)
        await self._save_queue()

        if not task.video_sources:
            parse_attempts = int(cfg_get("download.parse_attempts", 3))
            parse_backoff = int(cfg_get("download.retry_backoff_s", 5))
            sources = None
            for attempt in range(1, parse_attempts + 1):
                sources = await _resolve_sources(task.episode.url)
                if sources:
                    self._log_activity(
                        text=f"video URL получен ({len(sources)} качества)",
                        task_id=task.id,
                    )
                    break
                logger.warning(
                    "Парсинг %s: попытка %d/%d не дала результата",
                    task.episode.url, attempt, parse_attempts,
                )
                self._log_activity(
                    text=f"Не удалось получить video URL — попытка {attempt}/{parse_attempts}",
                    task_id=task.id, level="warn",
                )
                if attempt < parse_attempts:
                    await asyncio.sleep(parse_backoff)

            if not sources:
                return await self._handle_failure(task, "Не удалось получить video URL")
            task.video_sources = sources

            if not task.meta:
                meta = await asyncio.to_thread(parse_episode_meta, task.episode.url)
                if meta:
                    task.meta = meta

        fallback = cfg_get("download.quality_fallback", "lower")
        chosen = select_quality(task.video_sources, task.selected_quality, fallback)
        if not chosen:
            return await self._handle_failure(task, "Нет подходящего качества")

        task.actual_quality = chosen.quality

        download_path = Path(cfg_get("download.path", "data/downloads"))
        anime_dir = download_path / task.episode.slug
        anime_dir.mkdir(parents=True, exist_ok=True)

        filename = _sanitize_filename(task.episode.slug, task.episode.season, task.episode.episode_number)
        dest = anime_dir / filename

        min_size_mb = float(cfg_get("download.min_size_mb", 1.0))
        reuse = None
        if task.downloaded_to and Path(task.downloaded_to).exists():
            ok, _ = validate_downloaded_file(Path(task.downloaded_to), min_size_mb)
            if ok:
                reuse = Path(task.downloaded_to)

        if reuse is None:
            logger.info("[воркер %d] %s → %s\n    URL: %s", worker_id, chosen.quality, dest, chosen.url)
            self._log_activity(text=f"Скачивание {chosen.quality} → {dest.name}", task_id=task.id)

            update_manifest_file(
                anime_dir=anime_dir,
                slug=task.episode.slug,
                season=task.episode.season,
                episode_number=task.episode.episode_number,
                title=task.episode.title,
                url=task.episode.url,
                filename=filename,
                status=TaskStatus.DOWNLOADING.value,
                anime_title=task.episode.anime_title,
            )

            def cancel_check():
                t = self._tasks.get(task.id)
                return t is None or t.status in (TaskStatus.CANCELLED, TaskStatus.PAUSED) or self._stop_event.is_set()

            def on_progress(progress: float, speed_mbps: float):
                t = self._tasks.get(task.id)
                if t is None:
                    return
                t.progress = progress
                t.speed = round(speed_mbps, 2)
                self._broadcast()

            async def on_reparse() -> Optional[str]:
                sources = await _resolve_sources(task.episode.url)
                if sources:
                    fb = cfg_get("download.quality_fallback", "lower")
                    ch = select_quality(sources, task.selected_quality, fb)
                    if ch:
                        return ch.url
                return None

            def on_activity(text: str, level: str = "info"):
                self._log_activity(text, task.id, level)

            success = await download_video(
                url=chosen.url,
                dest=dest,
                cancel_check=cancel_check,
                progress_callback=on_progress,
                reparse_callback=on_reparse,
                activity_callback=on_activity,
                cookies=get_all_cookies(),
            )
        else:
            dest = reuse
            success = True
            task.downloaded_to = str(reuse)
            reuse.with_name(reuse.name + ".error").unlink(missing_ok=True)
            logger.info("[воркер %d] Файл уже скачан — переиспользую %s", worker_id, reuse)
            self._log_activity(text="Файл уже скачан, переиспользую", task_id=task.id)


        if success:
            min_size_mb = float(cfg_get("download.min_size_mb", 1.0))
            ok, reason = validate_downloaded_file(dest, min_size_mb)
            if not ok:
                dest.unlink(missing_ok=True)
                logger.error("  Валидация не пройдена (%s): %s", dest.name, reason)
                return await self._handle_failure(
                    task, f"Скачанный файл не валиден: {reason}", stub=True,
                )

            self._stub_streak = 0
            task.error = None
            task.downloaded_to = str(dest)

            if cfg_get("telegram.auto_send_enabled", False):
                task.status = TaskStatus.UPLOADING
                self._broadcast(force=True)
                await self._upload_to_telegram(task, dest)
            else:
                task.status = TaskStatus.DONE
                logger.info("[воркер %d] Задача %s завершена: %s", worker_id, task.id, dest)
                self._log_activity(text=f"Готово: {dest.name}", task_id=task.id)
                update_manifest_file(
                    anime_dir=anime_dir,
                    slug=task.episode.slug,
                    season=task.episode.season,
                    episode_number=task.episode.episode_number,
                    title=task.episode.title,
                    url=task.episode.url,
                    filename=filename,
                    status=TaskStatus.DONE.value,
                    anime_title=task.episode.anime_title,
                )

        else:
            logger.error("[воркер %d] Задача %s не удалась", worker_id, task.id)
            return await self._handle_failure(task, "Не удалось скачать файл")

        self._broadcast(force=True)

    _RUNNING = (
        TaskStatus.QUEUED,
        TaskStatus.DOWNLOADING,
        TaskStatus.UPLOADING,
        TaskStatus.BLOCKED,
    )

    def _find_blocker(self, task: VideoTask) -> Optional[VideoTask]:
        min_key = None
        min_task: Optional[VideoTask] = None
        for t in self._tasks.values():
            if t.episode.slug != task.episode.slug:
                continue
            if t.status not in self._RUNNING:
                continue
            key = (t.episode.season, t.episode.episode_number)
            if min_key is None or key < min_key:
                min_key = key
                min_task = t
        if min_task is None or min_task.id == task.id:
            return None
        return min_task

    def _cooldown_remaining(self) -> float:
        return max(0.0, self._cooldown_until - time.time())

    @property
    def cooldown_until(self) -> float:
        return self._cooldown_until

    async def _wait_my_turn(self, task: VideoTask) -> bool:
        while True:
            blocker = self._find_blocker(task)
            if blocker is None:
                return True

            logger.debug(
                "%s ждёт эпизод %d (задача %s/%s)",
                task.episode.title, blocker.episode.episode_number,
                blocker.status.value, blocker.id,
            )
            task.status = TaskStatus.BLOCKED
            self._broadcast()
            self._log_activity(
                text=f"Ждёт эпизод {blocker.episode.episode_number:04d}",
                task_id=task.id, level="warn",
            )
            await asyncio.sleep(2)
            self._queue.put_nowait(task.id)
            return False

    async def _handle_failure(self, task: VideoTask, error: str, stub: bool = False) -> None:
        if stub:
            self._stub_streak += 1
            trigger = int(cfg_get("download.stub_trigger", 3))
            cooldown_s = int(cfg_get("download.cooldown_s", 900))
            if self._stub_streak >= trigger:
                self._stub_streak = 0
                self._cooldown_until = time.time() + cooldown_s
                task.status = TaskStatus.QUEUED
                task.retries = 0
                task.error = error
                self._queue.put_nowait(task.id)
                self._broadcast(force=True)
                dur = f"{cooldown_s // 60} мин" if cooldown_s >= 60 else f"{cooldown_s} сек"
                logger.warning(
                    "Анти-бот флаг IP (%d заглушки подряд) — пауза %s, "
                    "очередь продолжится автоматически после.",
                    trigger, dur,
                )
                self._log_activity(
                    text=f"Анти-бот флаг: пауза очереди {dur}", task_id=task.id, level="warn",
                )
                return

        task.error = error
        self._log_activity(text=error, task_id=task.id, level="error")
        task.retries += 1
        wait_s = int(cfg_get("download.retry_backoff_s", 30))

        if task.downloaded_to:
            try:
                Path(task.downloaded_to).unlink(missing_ok=True)
            except Exception:
                pass
            task.downloaded_to = None

        task.status = TaskStatus.QUEUED
        self._queue.put_nowait(task.id)
        logger.warning(
            "%s — попытка %d, повтор через %ds: %s",
            task.episode.title, task.retries, wait_s, error,
        )
        await asyncio.sleep(wait_s)
        self._broadcast(force=True)

    async def _upload_to_telegram(self, task: VideoTask, file_path: Path):
        from telegram.client import get_client as tg_get_client
        client = tg_get_client()

        if client is None:
            from telegram.client import has_session
            if not has_session():
                await self._fail_send(task, "Telegram не авторизован — подключите клиент в настройках")
                return
            await self._defer_upload(task, "Telegram клиент ещё не запущен")
            return
        if not getattr(client, "is_connected", False):
            await self._defer_upload(task, "Telegram не подключён (умер VPN/прокси?)")
            return

        chat_id = cfg_get("telegram.target_dialog", "")
        if not chat_id:
            await self._fail_send(task, "Не указан чат для отправки (telegram.target_dialog)")
            return

        logger.info("Отправляю %s в %s...", file_path.name, chat_id)
        chat_id = await self._ensure_peer_cached(client, chat_id)
        if chat_id is None:
            await self._defer_upload(task, "Не удалось определить получателя для отправки")
            return

        title = task.episode.anime_title or task.episode.slug
        tag = re.sub(r'[^\w]', '_', title)
        tag = re.sub(r'_+', '_', tag).strip('_')
        ep_str = f"S{task.episode.season:02d}E{task.episode.episode_number:04d}"
        caption = f"#{tag}\n{ep_str}"

        thumb_path: Optional[Path] = None
        if task.meta and task.meta.poster_url:
            try:
                import httpx
                resp = await asyncio.to_thread(
                    lambda: httpx.get(task.meta.poster_url, timeout=15.0, follow_redirects=True)
                )
                if resp.status_code == 200:
                    thumb_dir = file_path.parent / ".thumbs"
                    thumb_dir.mkdir(parents=True, exist_ok=True)
                    thumb_path = thumb_dir / f"{file_path.stem}_thumb.jpg"
                    thumb_path.write_bytes(resp.content)
                    logger.debug("Постер сохранён: %s", thumb_path)
                else:
                    logger.debug("Постер не загрузился: HTTP %d", resp.status_code)
            except Exception as e:
                logger.debug("Не удалось загрузить постер: %s", e)

        defer = False
        defer_reason = "Telegram недоступен при отправке"
        defer_delay = 5.0
        try:
            msgs = await upload_file(
                client=client,
                file_path=file_path,
                chat_id=chat_id,
                thumb_path=thumb_path,
                caption=caption,
            )
            if not msgs:
                raise UploadSendError(f"Файл не отправлен: {file_path.name}")

            task.status = TaskStatus.DONE
            task.error = None
            logger.info("[воркер] %s отправлен в %s (%d сообщ.)", file_path.name, chat_id, len(msgs))
            self._log_activity(text=f"Отправлено в Telegram: {file_path.name}", task_id=task.id)
            update_manifest_file(
                anime_dir=file_path.parent,
                slug=task.episode.slug,
                season=task.episode.season,
                episode_number=task.episode.episode_number,
                title=task.episode.title,
                url=task.episode.url,
                filename=file_path.name,
                status=TaskStatus.DONE.value,
                anime_title=task.episode.anime_title,
            )
        except TelegramOfflineError as e:
            logger.warning("%s", e)
            defer = True
            defer_reason = str(e)
            defer_delay = float(getattr(e, "backoff_s", None) or 5.0)
        except UploadSendError as e:
            await self._fail_send(task, str(e))
        except Exception as e:
            logger.error("Неожиданная ошибка отправки %s: %s", file_path.name, e)
            await self._fail_send(task, f"Неожиданная ошибка: {e}")
        finally:
            if thumb_path and thumb_path.exists():
                try:
                    thumb_path.unlink()
                except Exception:
                    pass
            self._broadcast(force=True)

        if defer:
            await self._defer_upload(task, defer_reason, delay_s=defer_delay)

    async def _defer_upload(self, task: VideoTask, reason: str, delay_s: float = 5.0) -> None:
        task.deferrals += 1
        task.error = reason
        task.status = TaskStatus.QUEUED
        self._log_activity(
            text=f"Отправка отложена: {reason} (раз {task.deferrals})",
            task_id=task.id, level="warn",
        )
        logger.warning(
            "%s — %s. Отложено (раз %d), продолжу автоматически",
            task.episode.title, reason, task.deferrals,
        )
        self._broadcast(force=True)
        await asyncio.sleep(delay_s)
        self._queue.put_nowait(task.id)

    async def _fail_send(self, task: VideoTask, reason: str) -> None:
        task.deferrals += 1
        task.error = reason
        task.status = TaskStatus.QUEUED
        wait_s = int(cfg_get("download.retry_backoff_s", 30))
        logger.error(
            "%s — отправка не удалась: %s (повтор через %ds)",
            task.episode.title, reason, wait_s,
        )
        self._log_activity(
            text=f"Отправка не удалась, повтор через {wait_s}с: {reason}",
            task_id=task.id, level="error",
        )
        self._broadcast(force=True)
        await asyncio.sleep(wait_s)
        self._queue.put_nowait(task.id)

    @staticmethod
    async def _ensure_peer_cached(
        client: "Client",
        chat_id: str | int,
    ) -> str | int | None:
        from pyrogram import raw, utils as pyro_utils
        from pyrogram.errors import PeerIdInvalid

        resolved_id: str | int | None = None

        if isinstance(chat_id, str) and not chat_id.lstrip('@').lstrip('-').strip().isdigit():
            username = chat_id.lstrip('@').strip()
            try:
                r = await client.invoke(
                    raw.functions.contacts.ResolveUsername(username=username)
                )
                await client.fetch_peers(r.users)
                await client.fetch_peers(r.chats)
                if r.chats:
                    chat = r.chats[0]
                    if isinstance(chat, (raw.types.Channel, raw.types.ChannelForbidden)):
                        resolved_id = pyro_utils.get_channel_id(chat.id)
                        logger.debug("Username → channel: %s (%s)", username, resolved_id)
                    elif isinstance(chat, raw.types.Chat):
                        resolved_id = -chat.id
                if resolved_id is None and r.users:
                    resolved_id = r.users[0].id
                    logger.debug("Username → user: %s (%s)", username, resolved_id)
            except Exception as e:
                logger.debug("Username resolve failed: %s", e)

            if resolved_id is not None:
                cfg_set("telegram.target_dialog", str(resolved_id))
                return resolved_id
            return chat_id

        try:
            peer_id = int(str(chat_id).strip())
        except (ValueError, TypeError):
            return chat_id

        try:
            await client.resolve_peer(peer_id)
            logger.info("Peer уже в кэше: %s", peer_id)
            return peer_id
        except (PeerIdInvalid, ValueError):
            pass

        if peer_id < 0 and str(peer_id).startswith("-100"):
            raw_channel_id = pyro_utils.get_channel_id(peer_id)
            try:
                r = await client.invoke(
                    raw.functions.channels.GetChannels(
                        id=[raw.types.InputChannel(
                            channel_id=raw_channel_id,
                            access_hash=0,
                        )]
                    )
                )
                await client.fetch_peers(r.chats)
                await client.resolve_peer(peer_id)
                logger.info("ID → channel: %s (raw=%s)", peer_id, raw_channel_id)
                resolved_id = peer_id
            except Exception as e:
                logger.debug("ID not a channel: %s", e)

        if peer_id < 0 and resolved_id is None:
            try:
                r = await client.invoke(
                    raw.functions.messages.GetChats(id=[-peer_id])
                )
                if r.chats:
                    chat = r.chats[0]
                    migrated = getattr(chat, "migrated_to", None)
                    if migrated:
                        new_channel_id = pyro_utils.get_channel_id(migrated.channel_id)
                        logger.info(
                            "Чат %s мигрирован в супергруппу %s,"
                            "обновляю ID", peer_id, new_channel_id,
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
                        resolved_id = new_channel_id
                        cfg_set("telegram.target_dialog", str(new_channel_id))
                    else:
                        await client.fetch_peers(r.chats)
                        await client.resolve_peer(peer_id)
                        logger.info("ID → chat: %s", peer_id)
                        resolved_id = peer_id
            except Exception as e:
                logger.debug("ID not a chat: %s", e)

        if peer_id > 0 and resolved_id is None:
            try:
                r = await client.invoke(
                    raw.functions.users.GetUsers(
                        id=[raw.types.InputUser(user_id=peer_id, access_hash=0)]
                    )
                )
                await client.fetch_peers(r)
                await client.resolve_peer(peer_id)
                logger.info("ID → user: %s", peer_id)
                resolved_id = peer_id
            except Exception as e:
                logger.debug("ID not a user: %s", e)

        if resolved_id is not None:
            current_cfg = cfg_get("telegram.target_dialog", "")
            if str(resolved_id) != current_cfg:
                logger.info(
                    "Обновляю target_dialog в конфиге: %s → %s",
                    current_cfg, resolved_id,
                )
                cfg_set("telegram.target_dialog", str(resolved_id))
            return resolved_id

        logger.warning(
            "Не удалось распознать peer ID: %s. "
            "Попробуйте заново выбрать чат в настройках.",
            chat_id,
        )
        return None

    def subscribe_sse(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._sse_queues.append(q)
        return q

    def unsubscribe_sse(self, q: asyncio.Queue):
        if q in self._sse_queues:
            self._sse_queues.remove(q)

    def _broadcast(self, force: bool = False):
        now = time.monotonic()
        if not force and now - self._last_broadcast < 1.0:
            return
        self._last_broadcast = now

        data = self._serialize_state()
        dead = []
        for q in self._sse_queues:
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self.unsubscribe_sse(q)

    def _log_activity(self, text: str, task_id: str = "", level: str = "info"):
        self._activity.append({
            "ts": time.time(),
            "level": level,
            "task": task_id,
            "text": text,
        })
        if len(self._activity) > ACTIVITY_MAX:
            del self._activity[: len(self._activity) - ACTIVITY_MAX]

    def _serialize_state(self) -> dict:
        return {
            "tasks": [t.model_dump() for t in self._tasks.values()],
            "cooldown_until": self._cooldown_until,
            "activity": self._activity,
        }


    async def _save_queue(self):
        try:
            tasks_data = []
            for t in self._tasks.values():
                if t.status in (
                    TaskStatus.QUEUED,
                    TaskStatus.DOWNLOADING,
                    TaskStatus.PAUSED,
                    TaskStatus.BLOCKED,
                ):
                    tasks_data.append({
                        "id": t.id,
                        "episode": t.episode.model_dump(),
                        "video_sources": [s.model_dump() for s in t.video_sources],
                        "selected_quality": t.selected_quality,
                        "actual_quality": t.actual_quality,
                        "status": t.status.value,
                        "progress": t.progress,
                        "speed": t.speed,
                        "retries": t.retries,
                        "error": t.error,
                        "downloaded_to": t.downloaded_to,
                        "meta": t.meta.model_dump() if t.meta else None,
                    })
            QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
            QUEUE_FILE.write_text(
                json.dumps(
                    {"tasks": tasks_data, "cooldown_until": self._cooldown_until},
                    ensure_ascii=False, indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error("Ошибка сохранения очереди: %s", e)

    async def _restore_queue(self):
        if not QUEUE_FILE.exists():
            return
        try:
            data = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))

            saved_cd = data.get("cooldown_until", 0.0)
            if saved_cd and time.time() < saved_cd:
                self._cooldown_until = saved_cd
                logger.warning(
                    "Восстановлена активная пауза очереди "
                    "(до %s)", time.strftime("%H:%M:%S", time.localtime(saved_cd)),
                )
            seen: dict[tuple, dict] = {}
            for item in data.get("tasks", []):
                ep_item = item.get("episode") or {}
                key = (ep_item.get("slug"), ep_item.get("season"), ep_item.get("episode_number"))
                if not all(k is not None for k in key):
                    continue
                prev = seen.get(key)
                if prev is None or (not prev.get("downloaded_to") and item.get("downloaded_to")):
                    seen[key] = item

            restored = 0
            for item in seen.values():
                ep = Episode(**item["episode"])
                sources = [VideoSource(**s) for s in item.get("video_sources", [])]
                task = VideoTask(
                    id=item["id"],
                    episode=ep,
                    video_sources=sources,
                    selected_quality=item.get("selected_quality", "720p"),
                    actual_quality=item.get("actual_quality"),
                    status=TaskStatus(item.get("status", TaskStatus.QUEUED.value)),
                    progress=item.get("progress", 0.0),
                    retries=item.get("retries", 0),
                    error=item.get("error"),
                    downloaded_to=item.get("downloaded_to"),
                    meta=EpisodeMeta(**item["meta"]) if item.get("meta") else None,
                )
                self._tasks[task.id] = task
                if task.status in (TaskStatus.DOWNLOADING, TaskStatus.BLOCKED):
                    task.status = TaskStatus.QUEUED
                if task.status == TaskStatus.QUEUED:
                    self._queue.put_nowait(task.id)
                restored += 1

            if restored:
                logger.info("Восстановлено %d задач из очереди", restored)
        except Exception as e:
            logger.error("Ошибка восстановления очереди: %s", e)


_manager: Optional[TaskManager] = None


def get_manager() -> TaskManager:
    global _manager
    if _manager is None:
        _manager = TaskManager()
    return _manager


async def start_manager():
    m = get_manager()
    await m.start()


async def stop_manager():
    m = get_manager()
    await m.stop()
