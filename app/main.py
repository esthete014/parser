import asyncio
import logging
import logging.handlers
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional
from threading import Thread
import os
import time
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.routes import auth, catalog, downloads, settings, telegram

logger = logging.getLogger(__name__)

LOG_DIR = Path("data/logs")


def setup_file_logging():
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    log_file = LOG_DIR / f"app_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log"

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=2, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(formatter)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for h in root.handlers[:]:
        root.removeHandler(h)
    root.addHandler(ch)
    root.addHandler(fh)

    logging.getLogger("pyrogram.session").setLevel(logging.WARNING)
    logging.getLogger("pyrogram.connection").setLevel(logging.CRITICAL)
    logging.getLogger("pyrogram.methods").setLevel(logging.CRITICAL)
    logging.getLogger("hpack").setLevel(logging.WARNING)
    logging.getLogger("h2").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    logger.info("Логгирование в файл: %s", log_file)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_file_logging()
    logger.info("Запускается...")

    from core.auth import try_auto_login
    if try_auto_login():
        logger.info("Авторизация выполнена")
    else:
        logger.info("Авторизация не доступна")

    from core.task_manager import start_manager
    await start_manager()

    async def _try_start_tg():
        try:
            from telegram.client import start_client
            if await start_client():
                logger.info("Telegram userbot запущен")
            else:
                logger.info("Telegram: авторизация через UI при подключении")
        except Exception as e:
            logger.warning("Telegram: ошибка подключения: %s", e)

    _tg_task = asyncio.create_task(_try_start_tg())

    yield

    logger.info("завершает работу...")

    from core.task_manager import stop_manager
    await stop_manager()
    from telegram.client import stop_client as tg_stop
    await tg_stop()
    from core.session import close_client
    close_client()
    from core.url_fetcher import close_browser
    await close_browser()


app = FastAPI(
    title="downloader",
    description="Парсер видео",
    version="0.0.2",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(auth.router)
app.include_router(catalog.router)
app.include_router(downloads.router)
app.include_router(settings.router)
app.include_router(telegram.router)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/api/shutdown")
async def api_shutdown():

    def _force_exit():
        time.sleep(0.5)
        logger.info("Выключение...")
        os._exit(0)

    Thread(target=_force_exit, daemon=True).start()
    return {"ok": True, "message": "Сервер завершает работу"}
