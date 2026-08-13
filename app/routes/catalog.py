"""
Роуты каталога: страница парсинга + API для получения списка эпизодов.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from core.auth import is_authorized
from core.parser import parse_anime_page, parse_episode_page, parse_episode_meta, validate_anime_url

logger = logging.getLogger(__name__)

router = APIRouter(tags=["catalog"])
templates = Jinja2Templates(directory="app/templates")


class ParseRequest(BaseModel):
    url: str


@router.get("/parse", response_class=HTMLResponse)
async def parse_page(request: Request):
    return templates.TemplateResponse(
        "catalog.html",
        {"request": request, "title": "parse"},
    )


@router.get("/anime/{slug}", response_class=HTMLResponse)
async def anime_detail(slug: str, request: Request):
    anime_url = f"https://jut.su/{slug}/"
    import json

    page = parse_anime_page(anime_url)
    seasons_json = "[]"
    title = slug
    if page:
        title = page.title or slug
        seasons_json = json.dumps(
            [
                {
                    "season": s.season,
                    "episodes": [
                        {
                            "number": e.episode_number,
                            "title": e.title,
                            "url": e.url,
                        }
                        for e in s.episodes
                    ],
                }
                for s in page.seasons
            ]
        )

    return templates.TemplateResponse(
        "anime_detail.html",
        {
            "request": request,
            "title": f"{title} — jutsu_parse",
            "slug": slug,
            "anime_title": title,
            "seasons_json": seasons_json,
        },
    )


@router.post("/api/parse")
async def api_parse(data: ParseRequest):
    if not is_authorized():
        return JSONResponse(
            content={"ok": False, "error": "Не авторизован на jut.su"},
        )

    error = validate_anime_url(data.url)
    if error:
        return JSONResponse(content={"ok": False, "error": error})

    page = parse_anime_page(data.url)
    if not page:
        return JSONResponse(
            content={"ok": False, "error": "Не удалось распарсить страницу"},
        )

    return JSONResponse(content={"ok": True, "slug": page.slug, "title": page.title})


@router.post("/api/parse/episode")
async def api_parse_episode(data: ParseRequest):
    if not is_authorized():
        return JSONResponse(
            content={"ok": False, "error": "Не авторизован на jut.su"},
        )

    sources = parse_episode_page(data.url)
    if not sources:
        return JSONResponse(
            content={"ok": False, "error": "Не удалось получить video URL"},
        )

    meta = parse_episode_meta(data.url)

    return {
        "ok": True,
        "sources": [s.model_dump() for s in sources],
        "meta": meta.model_dump() if meta else None,
    }
