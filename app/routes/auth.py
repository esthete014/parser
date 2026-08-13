
import logging
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from core.auth import login as my_login
from core.auth import logout as my_logout
from core.auth import is_authorized, save_credentials, try_auto_login

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])
templates = Jinja2Templates(directory="app/templates")


class LoginRequest(BaseModel):
    login: str
    password: str
    remember: Optional[bool] = False


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "title": "start up login"},
    )


@router.post("/api/auth/login")
async def api_login(data: LoginRequest):
    try:
        success = my_login(data.login, data.password)
    except Exception as e:
        logger.exception("Ошибка при входе")
        return {"ok": False, "error": f"Ошибка: {e}"}

    if success:
        if data.remember:
            save_credentials(data.login, data.password)
        return {"ok": True}
    else:
        return {"ok": False, "error": "Неверный логин или пароль"}


@router.post("/api/auth/logout")
async def api_logout():
    my_logout()
    return {"ok": True}


@router.get("/api/auth/status")
async def api_auth_status():
    return {"authorized": is_authorized()}
