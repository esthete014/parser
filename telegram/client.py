import asyncio
import logging
from pathlib import Path
from typing import Optional

from pyrogram import Client
from pyrogram.errors import (
    ApiIdInvalid,
    PhoneNumberInvalid,
    PhoneCodeInvalid,
    SessionPasswordNeeded,
)

from config.settings import get as cfg_get
from telegram.handlers import register_handlers

logger = logging.getLogger(__name__)

SESSION_DIR = Path("data/telegram_session")
SESSION_NAME = "jutsu_bot"
SESSION_FILE = SESSION_DIR / f"{SESSION_NAME}.session"

_client: Optional[Client] = None
_client_task: Optional[asyncio.Task] = None

_pending_auth: dict = {
    "client": None,
    "phone_code_hash": None,
    "phone_number": None,
}


def get_client() -> Optional[Client]:
    return _client


def is_connected() -> bool:
    return _client is not None and _client.is_connected


def _make_client(phone_number: str = "") -> Client:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)

    kwargs = dict(
        name=str(SESSION_DIR / SESSION_NAME),
        api_id=cfg_get("telegram.api_id", 0),
        api_hash=cfg_get("telegram.api_hash", ""),
        workdir=".",
        phone_number=phone_number,
        in_memory=False,
        sleep_threshold=cfg_get("telegram.sleep_threshold", 60),
    )

    proxy_enabled = cfg_get("telegram.proxy.enabled", False)
    if proxy_enabled:
        host = cfg_get("telegram.proxy.host", "")
        port = cfg_get("telegram.proxy.port", 9050)
        if host:
            proxy = {
                "scheme": cfg_get("telegram.proxy.scheme", "socks5"),
                "hostname": host,
                "port": int(port),
            }
            username = cfg_get("telegram.proxy.username", "")
            password = cfg_get("telegram.proxy.password", "")
            if username:
                proxy["username"] = username
            if password:
                proxy["password"] = password
            kwargs["proxy"] = proxy
            logger.info("Telegram через прокси: %s://%s:%s", proxy["scheme"], host, port)

    return Client(**kwargs)


def has_session() -> bool:
    return SESSION_FILE.exists()


async def start_client() -> bool:
    global _client, _client_task

    api_id = cfg_get("telegram.api_id", 0)
    api_hash = cfg_get("telegram.api_hash", "")

    if not api_id or not api_hash:
        logger.warning("Telegram не настроен: api_id/api_hash не заданы")
        return False

    if _client and _client.is_connected:
        return True

    if not has_session():
        logger.info("Нет сохранённой сессии Telegram — авторизация через UI")
        return False

    _client = _make_client()

    try:
        await asyncio.wait_for(_client.start(), timeout=15.0)
        logger.info("Telegram клиент подключён (есть сохранённая сессия)")
        register_handlers(_client)
        _client_task = asyncio.create_task(_keep_alive())
        return True
    except asyncio.TimeoutError:
        logger.warning("Таймаут подключения Telegram — сессия недействительна")
        try:
            SESSION_FILE.unlink(missing_ok=True)
        except PermissionError:
            pass
        if _client:
            try:
                await _client.disconnect()
            except Exception:
                pass
            _client = None
        return False
    except Exception as e:
        logger.warning("Не удалось запустить Telegram: %s", e)
        try:
            SESSION_FILE.unlink(missing_ok=True)
        except PermissionError:
            pass
        if _client:
            try:
                await _client.disconnect()
            except Exception:
                pass
            _client = None
        return False


async def send_code_and_connect(phone_number: str) -> dict:
    global _pending_auth, _client, _client_task

    api_id = cfg_get("telegram.api_id", 0)
    api_hash = cfg_get("telegram.api_hash", "")

    if not api_id or not api_hash:
        return {"ok": False, "error": "Telegram не настроен: укажите API ID и API Hash"}

    if _client and _client.is_connected:
        try:
            await _client.stop()
        except Exception:
            pass
        _client = None
        _client_task = None
        _pending_auth = {"client": None, "phone_code_hash": None, "phone_number": None}

    client = _make_client(phone_number)

    if has_session():
        try:
            await client.start()
            logger.info("Есть сохранённая сессия Telegram")
            register_handlers(client)
            _client = client
            _client_task = asyncio.create_task(_keep_alive())
            _pending_auth = {"client": None, "phone_code_hash": None, "phone_number": None}
            return {"ok": True, "already_authorized": True}
        except Exception:
            try:
                SESSION_FILE.unlink(missing_ok=True)
            except Exception:
                pass
            try:
                await client.disconnect()
            except Exception:
                pass
            client = _make_client(phone_number)

    logger.info("Подключаемся к Telegram (таймаут 15 с)...")
    try:
        await asyncio.wait_for(client.connect(), timeout=15.0)
    except asyncio.TimeoutError:
        logger.error("Таймаут подключения к Telegram (DC2)")
        try:
            await client.disconnect()
        except Exception:
            pass
        return {"ok": False, "error": "Таймаут подключения к Telegram. Возможно, MTProto заблокирован — нужен прокси."}
    except Exception as e:
        logger.error("Ошибка подключения к Telegram: %s", e)
        try:
            await client.disconnect()
        except Exception:
            pass
        return {"ok": False, "error": f"Не удалось подключиться: {e}"}

    # Отправляем код
    try:
        sent = await client.send_code(phone_number)
        _pending_auth = {
            "client": client,
            "phone_code_hash": sent.phone_code_hash,
            "phone_number": phone_number,
        }
        return {"ok": False, "need_code": True, "error": "Код отправлен в Telegram"}
    except PhoneNumberInvalid:
        await client.disconnect()
        return {"ok": False, "error": "Неверный номер телефона"}
    except ApiIdInvalid:
        await client.disconnect()
        return {"ok": False, "error": "Неверный API ID/API Hash"}
    except Exception as e:
        await client.disconnect()
        logger.error("Ошибка отправки кода: %s", e)
        return {"ok": False, "error": str(e)}


async def complete_auth(code: str) -> dict:
    global _pending_auth, _client, _client_task

    if not _pending_auth["client"]:
        return {"ok": False, "error": "Сначала отправьте номер телефона"}

    client = _pending_auth["client"]
    phone = _pending_auth["phone_number"]
    phone_code_hash = _pending_auth["phone_code_hash"]

    try:
        await client.sign_in(phone, phone_code_hash, code)
    except SessionPasswordNeeded:
        return {"ok": False, "need_password": True, "error": "Требуется пароль 2FA"}
    except PhoneCodeInvalid:
        return {"ok": False, "error": "Неверный код подтверждения"}
    except Exception as e:
        await client.disconnect()
        _pending_auth = {"client": None, "phone_code_hash": None, "phone_number": None}
        return {"ok": False, "error": str(e)}

    try:
        await client.start()
    except Exception as e:
        logger.warning("Ошибка доинициализации после auth: %s", e)
    logger.info("Telegram авторизован")
    register_handlers(client)
    _client = client
    _client_task = asyncio.create_task(_keep_alive())
    _pending_auth = {"client": None, "phone_code_hash": None, "phone_number": None}
    return {"ok": True}


async def complete_auth_password(password: str) -> dict:
    global _pending_auth, _client, _client_task

    if not _pending_auth["client"]:
        return {"ok": False, "error": "Сначала введите код"}

    client = _pending_auth["client"]

    try:
        await client.check_password(password)
    except Exception as e:
        return {"ok": False, "error": str(e)}

    try:
        await client.start()
    except Exception as e:
        logger.warning("Ошибка доинициализации после 2FA: %s", e)
    logger.info("Telegram: 2FA пройдена")
    register_handlers(client)
    _client = client
    _client_task = asyncio.create_task(_keep_alive())
    _pending_auth = {"client": None, "phone_code_hash": None, "phone_number": None}
    return {"ok": True}


async def stop_client():
    global _client, _client_task, _pending_auth

    if _client_task:
        _client_task.cancel()
        try:
            await _client_task
        except asyncio.CancelledError:
            pass
        _client_task = None

    if _client:
        try:
            await _client.stop()
        except Exception as e:
            logger.warning("Ошибка при остановке Telegram: %s", e)
        _client = None
        logger.info("Telegram клиент остановлен")

    if _pending_auth["client"]:
        try:
            await _pending_auth["client"].disconnect()
        except Exception:
            pass
        _pending_auth = {"client": None, "phone_code_hash": None, "phone_number": None}


async def logout_client():
    global _client, _client_task, _pending_auth

    if _client and _client.is_connected:
        try:
            await _client.log_out()
            logger.info("Telegram: сессия инвалидирована на сервере")
        except Exception as e:
            logger.warning("Ошибка logout на сервере: %s", e)

    await stop_client()

    try:
        SESSION_FILE.unlink(missing_ok=True)
        logger.info("Файл сессии удалён: %s", SESSION_FILE)
    except Exception as e:
        logger.warning("Ошибка удаления сессии: %s", e)


async def _keep_alive():
    backoff = 10.0
    was_connected = _client is not None and _client.is_connected
    try:
        while True:
            await asyncio.sleep(10)
            if _client is None:
                was_connected = False
                continue
            try:
                ok = _client.is_connected
            except Exception:
                ok = False
            if ok:
                backoff = 10.0
                was_connected = True
                continue
            if was_connected:
                logger.warning("Telegram отключился — пытаюсь восстановить соединение")
                was_connected = False
            if not has_session():
                was_connected = False
                await asyncio.sleep(10)
                continue
            try:
                await asyncio.wait_for(_client.start(), timeout=15.0)
                if _client.is_connected:
                    logger.info("Telegram восстановлен")
                    backoff = 10.0
                    was_connected = True
            except Exception as e:
                logger.warning(
                    "Не удалось восстановить соединение: %s — повтор через %ds",
                    e, int(backoff),
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120.0)
    except asyncio.CancelledError:
        pass
