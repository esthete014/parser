from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, HttpUrl


class TaskStatus(str, Enum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    UPLOADING = "uploading"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"
    PAUSED = "paused"
    CANCELLED = "cancelled"


class Episode(BaseModel):
    title: str
    url: str
    slug: str
    season: int
    episode_number: int
    anime_title: Optional[str] = None


class VideoSource(BaseModel):
    url: str
    quality: str
    format: str


class EpisodeMeta(BaseModel):
    duration_sec: Optional[int] = None
    duration_str: Optional[str] = None
    poster_url: Optional[str] = None
    upload_date: Optional[str] = None
    thumbnail_url: Optional[str] = None


class EpisodeFull(BaseModel):
    episode: Episode
    sources: list[VideoSource] = []
    meta: Optional[EpisodeMeta] = None


class AnimePage(BaseModel):
    slug: str
    anime_url: str
    title: Optional[str] = None
    seasons: list[Season] = []


class Season(BaseModel):
    season: int
    episodes: list[Episode] = []


class VideoTask(BaseModel):
    id: str
    episode: Episode
    video_sources: list[VideoSource] = []
    selected_quality: str = "720p"
    actual_quality: Optional[str] = None
    status: TaskStatus = TaskStatus.QUEUED
    progress: float = 0.0
    speed: float = 0.0
    retries: int = 0
    deferrals: int = 0
    error: Optional[str] = None
    downloaded_to: Optional[str] = None
    meta: Optional[EpisodeMeta] = None


class QualityFallback(str, Enum):
    LOWER = "lower"
    HIGHER = "higher"
