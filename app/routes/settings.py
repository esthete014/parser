import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from config.settings import get as cfg_get, load as cfg_load, save as cfg_save

logger = logging.getLogger(__name__)

router = APIRouter(tags=["settings"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return templates.TemplateResponse(
        "settings.html",
        {"request": request, "title": "Настройки — jutsu_parse"},
    )


@router.get("/api/settings")
async def api_settings_get():
    cfg = cfg_load()
    if "jutsu" in cfg and "password" in cfg["jutsu"]:
        cfg["jutsu"]["password"] = ""

    if "telegram" in cfg and cfg["telegram"].get("api_hash"):
        cfg["telegram"]["api_hash"] = cfg["telegram"]["api_hash"][:8] + "..."

    return JSONResponse(content=cfg)


@router.post("/api/settings")
async def api_settings_save(data: dict):
    current = cfg_load()

    for section, values in data.items():
        if section not in current:
            current[section] = {}
        if isinstance(values, dict):
            for k, v in values.items():
                if isinstance(current[section].get(k), int):
                    try:
                        v = int(v)
                    except (ValueError, TypeError):
                        pass
                elif isinstance(current[section].get(k), float):
                    try:
                        v = float(v)
                    except (ValueError, TypeError):
                        pass
                current[section][k] = v
        else:
            current[section] = values

    cfg_save(current)
    logger.info("Настройки сохранены")
    return {"ok": True}
