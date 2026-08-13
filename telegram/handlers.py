import logging
import re
from typing import Optional

from pyrogram import Client, filters
from pyrogram.handlers import MessageHandler
from pyrogram.types import Message

from core.parser import parse_anime_page, parse_episode_page
from core.task_manager import get_manager
from config.settings import set_value as cfg_set, get as cfg_get
from utils.models import Episode, TaskStatus

logger = logging.getLogger(__name__)

HELP_TEXT = """
**Parser** — загрузка аниме

Команды:
`/download <url>` — скачать отдельную серию
`/download <url> --all` — скачать все серии аниме
`/status` — текущие загрузки
`/list` — завершённые загрузки
`/cancel <id>` — отменить загрузку
`/set_dialog` — назначить этот чат для отправки видео
`/help` — эта справка
"""


def register_handlers(client: Client):
    handlers = [
        ("download", cmd_download, filters.command("download")),
        ("status", cmd_status, filters.command("status")),
        ("list", cmd_list, filters.command("list")),
        ("cancel", cmd_cancel, filters.command("cancel")),
        ("set_dialog", cmd_set_dialog, filters.command("set_dialog")),
        ("help", cmd_help, filters.command("help")),
        ("start", cmd_help, filters.command("start")),
    ]

    for i, (name, handler, filter_) in enumerate(handlers):
        client.add_handler(MessageHandler(handler, filters=filter_), group=i)
        logger.debug("Handler /%s registered (group=%d)", name, i)


async def cmd_download(client: Client, message: Message):
    args = message.text.split(maxsplit=2)
    if len(args) < 2:
        await message.reply("Укажите URL. Пример:\n`/download https://jut.su/...`")
        return

    url = args[1]
    download_all = len(args) > 2 and "--all" in args[2]

    if not url.startswith("http"):
        url = "https://jut.su" + ("" if url.startswith("/") else "/") + url

    await message.reply(f"Парсинг: {url}")

    if download_all:
        await _download_all(client, message, url)
    else:
        await _download_single(client, message, url)


async def _download_single(client: Client, message: Message, url: str):
    slug = _extract_slug(url)
    season = _extract_season(url)
    episode_num = _extract_episode(url)

    if not all([slug, season, episode_num]):
        m = re.search(r"jut\.su/([^/]+)/season-(\d+)/episode-(\d+)", url)
        if m:
            slug = m.group(1)
            season = int(m.group(2))
            episode_num = int(m.group(3))

    if not slug:
        await message.reply("Не удалось определить аниме из URL")
        return

    ep = Episode(
        title=f"S{season:02d}E{episode_num:04d}",
        url=url,
        slug=slug,
        season=season or 1,
        episode_number=episode_num or 1,
    )

    mgr = get_manager()
    task = mgr.add_task(episode=ep)
    await message.reply(f"Задача добавлена: `{task.id}`\n{ep.slug} S{ep.season:02d}E{ep.episode_number:04d}")


async def _download_all(client: Client, message: Message, url: str):
    page = parse_anime_page(url)
    if not page:
        await message.reply("Не удалось распарсить страницу аниме")
        return

    mgr = get_manager()
    count = 0
    for season in page.seasons:
        for ep in season.episodes:
            mgr.add_task(episode=ep)
            count += 1

    await message.reply(f"Добавлено {count} задач на скачивание: {page.title or page.slug}")


async def cmd_status(client: Client, message: Message):
    mgr = get_manager()
    tasks = mgr.get_tasks()

    active = [t for t in tasks if t.status in (TaskStatus.QUEUED, TaskStatus.DOWNLOADING)]
    done = [t for t in tasks if t.status == TaskStatus.DONE]
    failed = [t for t in tasks if t.status == TaskStatus.FAILED]

    text = "**Статус загрузок**\n\n"
    text += f"В очереди: {len([t for t in active if t.status == TaskStatus.QUEUED])}\n"
    text += f"Скачивается: {len([t for t in active if t.status == TaskStatus.DOWNLOADING])}\n"
    text += f"Завершено: {len(done)}\n"
    text += f"Ошибок: {len(failed)}\n\n"

    if active:
        for t in active[:5]:
            ep = t.episode
            status = "в очереди" if t.status == TaskStatus.QUEUED else "загружается"
            quality = t.actual_quality or t.selected_quality
            text += f"{status} `{t.id}` {ep.slug} S{ep.season:02d}E{ep.episode_number:04d} [{quality}] {t.progress:.0f}%\n"

    await message.reply(text)


async def cmd_list(client: Client, message: Message):
    mgr = get_manager()
    tasks = [t for t in mgr.get_tasks() if t.status == TaskStatus.DONE]

    if not tasks:
        await message.reply("Нет завершённых загрузок")
        return

    text = f"**Завершено:** {len(tasks)}\n\n"
    for t in tasks[-10:]:
        ep = t.episode
        text += f"{ep.slug} S{ep.season:02d}E{ep.episode_number:04d}\n"

    await message.reply(text)


async def cmd_cancel(client: Client, message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply("Укажите ID задачи. Пример:\n`/cancel abc123def456`")
        return

    task_id = args[1].strip()
    ok = await get_manager().cancel_task(task_id)
    if ok:
        await message.reply(f"Задача `{task_id}` отменена")
    else:
        await message.reply(f"Задача `{task_id}` не найдена или уже завершена")


async def cmd_set_dialog(client: Client, message: Message):
    chat_id = message.chat.id
    chat_title = message.chat.title or message.chat.username or str(chat_id)
    cfg_set("telegram.target_dialog", str(chat_id))
    await message.reply(f"Диалог для отправки установлен: **{chat_title}**\nID: `{chat_id}`")


async def cmd_help(client: Client, message: Message):
    """Показывает справку."""
    await message.reply(HELP_TEXT)



def _extract_slug(url: str) -> Optional[str]:
    m = re.search(r"jut\.su/([^/]+)", url)
    return m.group(1) if m else None


def _extract_season(url: str) -> Optional[int]:
    m = re.search(r"season-(\d+)", url)
    return int(m.group(1)) if m else None


def _extract_episode(url: str) -> Optional[int]:
    m = re.search(r"episode-(\d+)", url)
    return int(m.group(1)) if m else None
