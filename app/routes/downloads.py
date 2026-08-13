import asyncio
import json
import logging
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from core.auth import is_authorized
from core.task_manager import get_manager
from utils.models import Episode

logger = logging.getLogger(__name__)

router = APIRouter(tags=["downloads"])
templates = Jinja2Templates(directory="app/templates")


class AddDownloadRequest(BaseModel):
    url: str
    slug: Optional[str] = None
    season: Optional[int] = None
    episode_number: Optional[int] = None
    anime_title: Optional[str] = None


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    mgr = get_manager()
    tasks = mgr.get_tasks()
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "title": "Queue",
            "tasks_json": json.dumps(
                [t.model_dump() for t in tasks], ensure_ascii=False
            ),
            "cooldown_until": mgr.cooldown_until,
        },
    )


@router.get("/api/downloads")
async def api_downloads():
    mgr = get_manager()
    tasks = mgr.get_tasks()
    return {
        "tasks": [t.model_dump() for t in tasks],
        "cooldown_until": mgr.cooldown_until,
    }


@router.post("/api/downloads")
async def api_downloads_add(data: AddDownloadRequest):
    if not is_authorized():
        return JSONResponse(
            content={"ok": False, "error": "Не авторизован на сайте"},
            status_code=401,
        )

    ep = Episode(
        title=f"{data.slug or 'anime'} S{data.season or 0:02d}E{data.episode_number or 0:04d}",
        url=data.url,
        slug=data.slug or "",
        season=data.season or 1,
        episode_number=data.episode_number or 1,
        anime_title=data.anime_title,
    )
    task = get_manager().add_task(episode=ep)
    return {"ok": True, "task_id": task.id}


@router.post("/api/downloads/{task_id}/cancel")
async def api_downloads_cancel(task_id: str):
    ok = await get_manager().cancel_task(task_id)
    return {"ok": ok}


@router.post("/api/downloads/{task_id}/pause")
async def api_downloads_pause(task_id: str):
    ok = await get_manager().pause_task(task_id)
    return {"ok": ok}


@router.post("/api/downloads/{task_id}/resume")
async def api_downloads_resume(task_id: str):
    ok = await get_manager().resume_task(task_id)
    return {"ok": ok}


@router.post("/api/downloads/{task_id}/retry")
async def api_downloads_retry(task_id: str):
    ok = await get_manager().retry_task(task_id)
    return {"ok": ok}


@router.post("/api/downloads/clear")
async def api_downloads_clear():
    removed = await get_manager().clear_queue()
    return {"ok": True, "removed": removed}


@router.post("/api/downloads/batch")
async def api_downloads_batch(data: list[AddDownloadRequest]):
    if not is_authorized():
        return JSONResponse(
            content={"ok": False, "error": "Не авторизован на сайте"},
            status_code=401,
        )

    mgr = get_manager()
    added = []
    for item in data:
        ep = Episode(
            title=f"{item.slug or 'anime'} S{item.season or 0:02d}E{item.episode_number or 0:04d}",
            url=item.url,
            slug=item.slug or "",
            season=item.season or 1,
            episode_number=item.episode_number or 1,
        )
        task = mgr.add_task(episode=ep)
        added.append(task.id)

    return {"ok": True, "task_ids": added}


@router.get("/api/downloads/stream")
async def api_downloads_stream(request: Request):

    async def event_generator():
        mgr = get_manager()
        q = mgr.subscribe_sse()
        try:
            data = mgr._serialize_state()
            yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

            while True:
                try:
                    data = await asyncio.wait_for(q.get(), timeout=30.0)
                    if data is None:
                        break
                    yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            mgr.unsubscribe_sse(q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
