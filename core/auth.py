import logging
from typing import Optional

import httpx

from core.session import (
    USER_AGENT,
    BASE_URL,
    get_client,
    has_auth_cookies,
    reset_client,
)
from config.settings import get as cfg_get, set_value as cfg_set
from config.secrets import encrypt, decrypt, has_key

logger = logging.getLogger(__name__)


def login(login_name: str, login_password: str) -> bool:
    data = {
        "login_name": login_name,
        "login_password": login_password,
        "login": "submit",
    }
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/x-www-form-urlencoded",
    }

    client = get_client()
    logger.info("Вход на сайт (пользователь: %s)", login_name)

    try:
        response = client.post(
            f"{BASE_URL}/",
            data=data,
            headers=headers,
        )
    except httpx.HTTPError as e:
        logger.error("Ошибка HTTP при входе: %s", e)
        return False

    if response.status_code != 200:
        logger.warning("Страница ответила %d", response.status_code)
        return False

    if has_auth_cookies():
        user_id = dict(client.cookies).get("dle_user_id", "?")
        logger.info("Успешный вход! dle_user_id = %s", user_id)
        return True

    logger.error("Вход не удался — куки авторизации не получены")
    return False


def is_authorized() -> bool:
    return has_auth_cookies()


def logout() -> None:
    reset_client()
    logger.info("Выполнен выход (сессия сброшена)")


def try_auto_login() -> bool:
    if is_authorized():
        logger.info("Уже авторизованы на сайте")
        return True

    saved_login = cfg_get("jutsu.login", "")
    saved_password_enc = cfg_get("jutsu.password", "")

    if not saved_login or not saved_password_enc:
        logger.info("Нет сохранённых данных для авто-входа")
        return False

    if not has_key():
        logger.warning("Нет ключа шифрования (data/.secret) — авто-вход невозможен")
        return False

    try:
        password = decrypt(saved_password_enc)
    except Exception as e:
        logger.error("Ошибка дешифровки пароля: %s", e)
        return False

    return login(saved_login, password)


def ensure_session() -> bool:
    if is_authorized():
        return True
    logger.info("Сессия истекла, пробуем автоматический вход...")
    return try_auto_login()


def save_credentials(login_name: str, login_password: str) -> None:
    encrypted = encrypt(login_password)
    cfg_set("jutsu.login", login_name)
    cfg_set("jutsu.password", encrypted)
    logger.info("Логин/пароль сохранены в конфиг (пароль зашифрован)")
