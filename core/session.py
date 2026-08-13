import logging
from typing import Optional

import httpx

from config.settings import get as cfg_get

logger = logging.getLogger(__name__)

_client: Optional[httpx.Client] = None

BASE_URL = "https://jut.su"

AUTH_COOKIES = {"dle_user_id", "PHPSESSID", "dle_password"}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)


def proxy_url() -> Optional[str]:
    if not cfg_get("telegram.proxy.enabled", False):
        return None
    scheme = cfg_get("telegram.proxy.scheme", "socks5")
    host = cfg_get("telegram.proxy.host", "")
    port = cfg_get("telegram.proxy.port", 0)
    username = cfg_get("telegram.proxy.username", "")
    password = cfg_get("telegram.proxy.password", "")
    auth = f"{username}:{password}@" if username else ""
    url = f"{scheme}://{auth}{host}:{port}"
    logger.info("HTTP-слой через прокси: %s", url)
    return url


def get_client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(
            http2=True,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
                "Accept-Language": "ru-RU,ru;q=0.9",
                "Referer": BASE_URL,
            },
            cookies={},
            timeout=30.0,
            follow_redirects=True,
            proxy=proxy_url(),
        )
        logger.debug("Создан новый httpx.Client (HTTP/2)")
    return _client


def close_client() -> None:
    global _client
    if _client is not None:
        _client.close()
        _client = None
        logger.debug("httpx.Client закрыт")


def reset_client() -> None:
    close_client()
    get_client()
    logger.debug("Сессия сброшена")


def get_cookie(name: str) -> Optional[str]:
    client = get_client()
    return dict(client.cookies).get(name)


def get_all_cookies() -> dict[str, str]:
    return dict(get_client().cookies)


def has_auth_cookies() -> bool:
    cookies = dict(get_client().cookies)
    return AUTH_COOKIES.issubset(cookies.keys())
