import asyncio
import logging
import random
import time

from config.settings import get as cfg_get

logger = logging.getLogger(__name__)


def _get_range() -> tuple[int, int]:
    return cfg_get("cooldown.min_ms", 2000), cfg_get("cooldown.max_ms", 5000)


def sleep_blocking() -> None:
    min_ms, max_ms = _get_range()
    delay = random.randint(min_ms, max_ms)
    logger.debug("Cooldown %d мс", delay)
    time.sleep(delay / 1000)


async def sleep_async() -> None:
    min_ms, max_ms = _get_range()
    delay = random.randint(min_ms, max_ms)
    logger.debug("Cooldown %d мс", delay)
    await asyncio.sleep(delay / 1000)
