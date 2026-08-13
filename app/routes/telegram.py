import asyncio
import logging
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from config.settings import get as cfg_get, set_value as cfg_set
from telegram.client import (
    complete_auth,
    complete_auth_password,
    get_client,
    is_connected,
    logout_client,
    send_code_and_connect,
    start_client,
    stop_client,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["telegram"])


class TelegramConnectRequest(BaseModel):
    phone_number: str


class AuthCodeRequest(BaseModel):
    code: str


class AuthPasswordRequest(BaseModel):
    password: str


@router.get("/api/telegram/status")
async def api_telegram_status():
    client = get_client()
    return {
        "connected": is_connected(),
        "has_api_config": bool(cfg_get("telegram.api_id", 0) and cfg_get("telegram.api_hash", "")),
        "target_dialog": cfg_get("telegram.target_dialog", ""),
    }


@router.post("/api/telegram/connect")
async def api_telegram_connect(data: TelegramConnectRequest):
    result = await send_code_and_connect(data.phone_number)
    return result


@router.post("/api/telegram/auth_code")
async def api_telegram_auth_code(data: AuthCodeRequest):
    result = await complete_auth(data.code)
    return result


@router.post("/api/telegram/auth_password")
async def api_telegram_auth_password(data: AuthPasswordRequest):
    result = await complete_auth_password(data.password)
    return result


@router.post("/api/telegram/disconnect")
async def api_telegram_disconnect():
    await stop_client()
    return {"ok": True}


@router.post("/api/telegram/logout")
async def api_telegram_logout():
    await logout_client()
    return {"ok": True}


@router.post("/api/telegram/config")
async def api_telegram_config(data: dict):
    api_id = data.get("api_id")
    api_hash = data.get("api_hash")
    if api_id:
        cfg_set("telegram.api_id", int(api_id))
    if api_hash:
        cfg_set("telegram.api_hash", api_hash)
    return {"ok": True}


@router.post("/api/telegram/set_dialog")
async def api_telegram_set_dialog(data: dict):
    dialog = data.get("dialog", "")
    if dialog:
        cfg_set("telegram.target_dialog", dialog)
    return {"ok": True}


@router.get("/api/telegram/dialogs")
async def api_telegram_dialogs():
    client = get_client()
    if not client or not is_connected():
        return {"ok": False, "error": "Telegram не подключён", "dialogs": []}

    try:
        dialogs_list = []
        async for dialog in client.get_dialogs(limit=100):
            chat = dialog.chat
            title = chat.title or getattr(chat, "first_name", "") or "?"
            identifier = chat.username or (
                f"+{chat.phone_number}" if getattr(chat, "phone_number", None) else str(chat.id)
            )
            dialogs_list.append({
                "id": chat.id,
                "title": title,
                "type": str(chat.type).split(".")[-1] if chat.type else "unknown",
                "identifier": identifier,
            })

        dialogs_list.sort(key=lambda d: d["title"].lower())
        return {"ok": True, "dialogs": dialogs_list}
    except Exception as e:
        logger.error("Ошибка получения диалогов: %s", e)
        return {"ok": False, "error": str(e), "dialogs": []}
