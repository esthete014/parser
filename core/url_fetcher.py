import asyncio
import logging
import time
from typing import Optional

from utils.models import VideoSource

from core.session import proxy_url

logger = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)

_playwright = None
_browser = None
_browser_lock = asyncio.Lock()

_SRC_JS = (
    "els => els.map(e => ({"
    "src: e.getAttribute('src') || '',"
    "label: e.getAttribute('label') || e.getAttribute('res') || '?'"
    "}))"
)


def _real_srcs(items):
    for it in items:
        s = it.get("src") or ""
        if s and "pixel.png" not in s and "yandexwebcache" in s:
            return True
    return False


async def _poll_real_sources(page, url: str, timeout_s: float):
    items = []
    poll_s = 0.5
    deadline = time.time() + timeout_s
    while True:
        try:
            items = await page.eval_on_selector_all("#my-player source", _SRC_JS)
        except Exception:
            items = []
        if _real_srcs(items):
            break
        if time.time() >= deadline:
            break
        await page.wait_for_timeout(int(poll_s * 1000))
    return items


async def _get_browser():
    global _browser, _playwright
    async with _browser_lock:
        if _browser is not None:
            return _browser
        from playwright.async_api import async_playwright
        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(headless=True)
        logger.info("Headless Chromium запущен")
        return _browser


async def fetch_episode_sources(url: str, timeout_s: float = 45.0) -> Optional[list[VideoSource]]:
    from core.session import get_all_cookies

    cookies = get_all_cookies()
    pw_cookies = [
        {"name": k, "value": v, "domain": ".jut.su", "path": "/"}
        for k, v in cookies.items()
    ]

    try:
        browser = await _get_browser()
    except Exception as e:
        logger.error("Не удалось запустить Chromium для %s: %s", url, e)
        return None

    context = None
    try:
        _ctx_kwargs = {
            "user_agent": UA,
            "viewport": {"width": 1366, "height": 768},
            "locale": "ru-RU",
            "timezone_id": "Europe/Moscow",
        }
        _proxy_server = proxy_url()
        if _proxy_server:
            _ctx_kwargs["proxy"] = {"server": _proxy_server}
        context = await browser.new_context(**_ctx_kwargs)
        await context.add_cookies(pw_cookies)
        page = await context.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            logger.warning("Ошибка загрузки %s в браузере: %s", url, e)
            return None

        items = await _poll_real_sources(page, url, timeout_s)

        if not _real_srcs(items):
            logger.warning(
                "Не дождались замены пустышки на %s за %ss — подписанный URL не появился "
                "(katon/fire.php или doton/earth.php не отработал, или сайт троттлит)",
                url, timeout_s,
            )
            return None
    finally:
        if context is not None:
            await context.close()

    sources = []
    for it in items:
        src = (it.get("src") or "").strip()
        if src and "pixel.png" not in src and "yandexwebcache" in src:
            sources.append(VideoSource(url=src, quality=it.get("label") or "?", format="mp4"))

    if not sources:
        logger.warning("Браузер не вернул подписанных URL для %s", url)
        return None

    logger.info("video URL получен через браузер: %d качеств (%s)", len(sources), url)
    for s in sources:
        logger.info("    [%s] %s", s.quality, s.url)
    return sources


async def close_browser():
    global _browser, _playwright
    async with _browser_lock:
        if _browser is not None:
            try:
                await _browser.close()
            except Exception:
                pass
            _browser = None
        if _playwright is not None:
            try:
                await _playwright.stop()
            except Exception:
                pass
            _playwright = None
        logger.info("Headless Chromium закрыт")
