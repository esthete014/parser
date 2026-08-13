import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def split_video(
    file_path: str | Path,
    max_size_mb: int = 1900,
) -> list[str]:
    src = Path(file_path)
    if not src.exists():
        raise FileNotFoundError(f"Файл не найден: {file_path}")

    file_size_mb = src.stat().st_size / (1024 * 1024)
    if file_size_mb <= max_size_mb:
        logger.info("Файл %s (%.1f МБ) меньше лимита %d МБ, нарезка не нужна",
                     src.name, file_size_mb, max_size_mb)
        return [str(src)]

    # Считаем количество частей
    num_parts = int(file_size_mb / max_size_mb) + 1
    logger.info("Нарезка %s (%.1f МБ) на %d частей по %d МБ",
                src.name, file_size_mb, num_parts, max_size_mb)

    duration = _get_duration(src)
    if not duration:
        logger.warning("Не удалось получить длительность, нарезка по размеру")
        return _split_by_size(src, num_parts)

    part_duration = duration / num_parts
    return _split_by_duration(src, part_duration, num_parts)


def _get_duration(file_path: Path) -> Optional[float]:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(file_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except (subprocess.SubprocessError, ValueError, FileNotFoundError) as e:
        logger.warning("ffprobe error: %s", e)
    return None


def _split_by_duration(
    src: Path, part_duration: float, num_parts: int
) -> list[str]:
    parts: list[str] = []
    stem = src.stem
    ext = src.suffix

    for i in range(num_parts):
        start = i * part_duration
        output = src.parent / f"{stem}.part{i + 1:02d}{ext}"

        logger.info("  Часть %d/%d: %s (start=%.1fс, duration=%.1fс)",
                    i + 1, num_parts, output.name, start, part_duration)

        cmd = [
            "ffmpeg", "-y",
            "-i", str(src),
            "-ss", str(start),
            "-t", str(part_duration),
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            str(output),
        ]

        try:
            subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=3600,
                check=True,
            )
            parts.append(str(output))
        except subprocess.CalledProcessError as e:
            logger.error("Ошибка нарезки части %d: %s\n%s", i + 1, e, e.stderr[:500])
            raise
        except FileNotFoundError:
            logger.error("ffmpeg не найден. Установите ffmpeg и добавьте в PATH")
            raise

    return parts


def _split_by_size(src: Path, num_parts: int) -> list[str]:
    parts: list[str] = []
    stem = src.stem
    ext = src.suffix

    total_size = src.stat().st_size
    part_size = total_size // num_parts

    for i in range(num_parts):
        output = src.parent / f"{stem}.part{i + 1:02d}{ext}"

        cmd = [
            "ffmpeg", "-y",
            "-i", str(src),
        ]

        if i > 0:
            cmd.extend(["-ss", str(i * part_size / _estimate_bitrate(src))])

        cmd.extend([
            "-fs", str(part_size),
            "-c", "copy",
            str(output),
        ])

        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=3600, check=True)
            parts.append(str(output))
        except Exception as e:
            logger.error("Ошибка нарезки части %d: %s", i + 1, e)
            import shutil
            shutil.copy2(src, output)
            parts.append(str(output))

    return parts


def _estimate_bitrate(file_path: Path) -> float:
    duration = _get_duration(file_path)
    if not duration or duration == 0:
        return 1_000_000
    return file_path.stat().st_size / duration
