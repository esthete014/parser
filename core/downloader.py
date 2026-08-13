import asyncio
import inspect
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from core.session import USER_AGENT, proxy_url

logger = logging.getLogger(__name__)

CHUNK_SIZE = 8 * 1024 * 1024


async def _run_callback(callback: Callable[..., Any]) -> Any:
    result = callback()
    if inspect.isawaitable(result):
        result = await result
    return result


async def download_video(
    url: str,
    dest: Path,
    cancel_check: Callable[[], bool],
    progress_callback: Optional[Callable[[float, float], None]] = None,
    reparse_callback: Optional[Callable[[], Optional[str]]] = None,
    activity_callback: Optional[Callable[[str, str, str], None]] = None,
    cookies: Optional[dict[str, str]] = None,
    max_retries: int = 3,
) -> bool:
    marker = dest.with_name(dest.name + ".downloading")
    cookies = cookies or {}
    current_url = url
    last_error = ""
    last_status = 0
    _last_logged_pct = -1

    def _act(icon: str, text: str, level: str = "info"):
        if activity_callback:
            activity_callback(icon, text, level)

    for attempt in range(1, max_retries + 1):
        start_time = time.monotonic()
        marker.touch(exist_ok=True)

        headers = {
            "User-Agent": USER_AGENT,
            "Referer": "https://jut.su/",
            "Accept": "*/*",
        }

        # Режим докачки
        existing_size = 0
        if dest.exists() and dest.stat().st_size > 0:
            existing_size = dest.stat().st_size
            headers["Range"] = f"bytes={existing_size}-"
            logger.info("Докачка с %d байт (попытка %d/%d)", existing_size, attempt, max_retries)

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(600.0, connect=30.0),
                cookies=cookies,
                proxy=proxy_url(),
            ) as client:
                async with client.stream("GET", current_url, headers=headers) as resp:
                    if resp.status_code not in (200, 206):
                        if resp.status_code in (403, 404) and reparse_callback:
                            logger.warning("  Ссылка протухла (%d), перепарсинг...", resp.status_code)
                            _act(
                                text=f"Ссылка протухла (HTTP {resp.status_code}), беру свежую...",
                                level="warn",
                            )
                            new_url = await _run_callback(reparse_callback)
                            if new_url and new_url != current_url:
                                current_url = new_url
                                if dest.exists():
                                    dest.unlink()
                                _act(text="Свежий video URL получен")
                                continue

                        logger.error("HTTP %d (попытка %d/%d)", resp.status_code, attempt, max_retries)
                        last_status = resp.status_code
                        last_error = f"HTTP {resp.status_code}"
                        await asyncio.sleep(2)
                        continue

                    total = int(resp.headers.get("content-length", 0))
                    downloaded = existing_size
                    mode = "ab" if existing_size > 0 else "wb"

                    with open(dest, mode) as f:
                        async for chunk in resp.aiter_bytes():
                            if cancel_check():
                                logger.info("  Загрузка отменена")
                                return False

                            f.write(chunk)
                            downloaded += len(chunk)

                            if total > 0:
                                progress = min(100.0, downloaded / total * 100)
                            else:
                                progress = 0.0

                            elapsed = time.monotonic() - start_time
                            speed_mbps = (downloaded / (1024 * 1024)) / elapsed if elapsed > 0 else 0.0

                            if progress_callback:
                                progress_callback(progress, speed_mbps)

                            pct_10 = int(progress / 10) * 10
                            if pct_10 > _last_logged_pct:
                                logger.info("  Прогресс: %d%% (%.1f МБ/с)", pct_10, speed_mbps)
                                _last_logged_pct = pct_10

            # Успешно
            if marker.exists():
                marker.unlink()
            dest.with_name(dest.name + ".error").unlink(missing_ok=True)
            return True

        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError, OSError) as e:
            last_error = str(e)
            last_status = 0
            logger.warning("  Ошибка (попытка %d/%d): %s", attempt, max_retries, e)
            if reparse_callback:
                new_url = await _run_callback(reparse_callback)
                if new_url and new_url != current_url:
                    logger.info("Перепарсинг дал новый URL")
                    _act(text=f"Ошибка соединения, беру свежий URL... ({type(e).__name__})", level="warn")
                    current_url = new_url
                    if dest.exists():
                        dest.unlink()
                    continue
            await asyncio.sleep(3)

    if marker.exists():
        marker.unlink()
    error_file = dest.with_name(dest.name + ".error")
    error_file.write_text(
        json.dumps({
            "url": current_url,
            "time": datetime.now().isoformat(),
            "http_status": last_status,
            "error": last_error or f"Не удалось скачать после {max_retries} попыток",
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return False


_NON_VIDEO_SIGNATURES = (
    b"\x89PNG\r\n",
    b"\xff\xd8\xff",
    b"<!DOCTYPE",
    b"<!doctype",
    b"<html",
)


def validate_downloaded_file(path: Path, min_size_mb: float = 1.0) -> tuple[bool, str]:
    if not path.exists() or path.stat().st_size == 0:
        return False, "Файл пустой или не существует"

    size = path.stat().st_size
    min_bytes = int(min_size_mb * 1024 * 1024)
    if size < min_bytes:
        return False, f"Файл слишком мал: {size} байт (минимум {min_bytes})"

    with open(path, "rb") as f:
        head = f.read(32)

    for sig in _NON_VIDEO_SIGNATURES:
        if head.startswith(sig):
            return False, "Ответ — не видео (заглушка/ошибка: %r)" % sig

    if head[4:8] != b"ftyp":
        return False, "Файл не похож на MP4 (нет ftyp-мэджика)"

    return True, ""


def cleanup_stale_markers(download_path: Path) -> int:
    cleaned = 0
    for marker in download_path.rglob("*.downloading"):
        video_file = marker.with_suffix("")
        if video_file.exists():
            video_file.unlink()
            logger.info("Удалён частичный файл: %s", video_file)
        marker.unlink()
        cleaned += 1
    for err in download_path.rglob("*.error"):
        err.unlink(missing_ok=True)
        logger.info("Удалён .error прошлой неудачной качки: %s", err)
        cleaned += 1
    if cleaned:
        logger.info("Очищено %d остаточных файлов (частичные/.error)", cleaned)
    return cleaned


def update_manifest(
    anime_dir: Path,
    slug: str,
    season: int,
    episode_number: int,
    title: str,
    url: str,
    filename: str,
    status: str,
    anime_title: Optional[str] = None,
):
    manifest_path = anime_dir / "manifest.json"
    manifest = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    manifest.setdefault("slug", slug)
    manifest.setdefault("url", f"https://jut.su/{slug}/")
    if anime_title:
        manifest["anime_title"] = anime_title

    seasons_data = manifest.setdefault("seasons", [])
    season_found = None
    for s in seasons_data:
        if s.get("season") == season:
            season_found = s
            break
    if not season_found:
        season_found = {"season": season, "episodes": []}
        seasons_data.append(season_found)

    ep_data: dict[str, Any] = {
        "number": episode_number,
        "title": title,
        "url": url,
        "filename": filename,
        "status": status,
    }
    if status == "done":
        ep_data["downloaded_at"] = datetime.now().isoformat()

    for i, ep in enumerate(season_found["episodes"]):
        if ep.get("number") == episode_number:
            season_found["episodes"][i] = ep_data
            break
    else:
        season_found["episodes"].append(ep_data)

    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def cleanup_stale_manifests(download_path: Path) -> int:
    cleaned = 0
    for manifest_path in download_path.rglob("manifest.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        changed = False
        for season in manifest.get("seasons", []):
            for ep in season.get("episodes", []):
                if ep.get("status") == "downloading":
                    filename = ep.get("filename")
                    if filename:
                        video_file = manifest_path.parent / filename
                        if video_file.exists():
                            video_file.unlink()
                            logger.info("Удалён повреждённый файл: %s", video_file)
                    ep["status"] = "queued"
                    changed = True
                    cleaned += 1

        if changed:
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    if cleaned:
        logger.info("Очищено %d прерванных записей в manifest (→ queued)", cleaned)
    return cleaned
