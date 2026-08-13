import logging
import re
from typing import Optional

from bs4 import BeautifulSoup

from core.auth import ensure_session
from core.session import BASE_URL, get_client, has_auth_cookies
from utils.cooldown import sleep_blocking
from utils.models import AnimePage, Episode, EpisodeMeta, Season, VideoSource

logger = logging.getLogger(__name__)


def parse_anime_page(url: str) -> Optional[AnimePage]:
    slug = _extract_slug(url)
    if not slug:
        logger.error("Не удалось извлечь slug из URL: %s", url)
        return None

    # Проверка и восстановление сессии
    if not has_auth_cookies() and not ensure_session():
        logger.warning("Нет авторизации на jut.su — парсинг может не сработать")

    sleep_blocking()

    client = get_client()
    headers = _base_headers(url)

    try:
        resp = client.get(url, headers=headers)
    except Exception as e:
        logger.error("Ошибка при загрузке страницы %s: %s", url, e)
        return None

    if resp.status_code != 200:
        logger.error("Страница %s ответила %d", url, resp.status_code)
        return None

    soup = BeautifulSoup(resp.content, "lxml", from_encoding="windows-1251")

    anime_title = _parse_title(soup)
    seasons = _parse_seasons(soup, slug, anime_title)

    if not seasons:
        logger.warning("На странице %s не найдено сезонов", url)
        return None

    return AnimePage(
        slug=slug,
        anime_url=url,
        title=anime_title,
        seasons=seasons,
    )


def validate_anime_url(url: str) -> Optional[str]:
    if not url.startswith(("https://jut.su/", "http://jut.su/")):
        return "URL должен начинаться с https://jut.su/"
    path = url.replace("https://jut.su", "").replace("http://jut.su", "")
    if not path or path == "/":
        return "Это главная страница. Укажите ссылку на аниме или эпизод."
    if not re.match(r"^/[^/]+", path):
        return "Некорректный URL. Введите ссылку на страницу аниме или эпизода."
    return None


def _extract_slug(url: str) -> Optional[str]:
    """Извлекает slug из URL аниме."""
    m = re.search(r"jut\.su/([^/]+)", url)
    return m.group(1) if m else None


def _base_headers(referer: str = "") -> dict:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/150.0.0.0 Safari/537.36"
        ),
        "Referer": referer or BASE_URL,
    }


def _parse_title(soup: BeautifulSoup) -> Optional[str]:
    h1 = soup.find("h1")
    if h1:
        return h1.get_text(strip=True)
    title_tag = soup.title
    if title_tag:
        text = title_tag.get_text(strip=True)
        text = re.sub(r"\s*смотреть онлайн.*$", "", text, flags=re.IGNORECASE)
        return text.strip()
    return None


def _parse_seasons(
    soup: BeautifulSoup, slug: str, anime_title: Optional[str] = None
) -> list[Season]:
    seasons = []
    last_season_num = None

    season_headers = soup.find_all("h2", class_="the-anime-season")
    if not season_headers:
        return _fallback_parse_episodes(soup, slug, anime_title)

    for header in season_headers:
        season_text = header.get_text(strip=True)
        season_num = _extract_season_number(season_text)

        if season_num is None:
            part_num = _extract_part_number(season_text)
            if part_num is not None and last_season_num is not None:
                season_num = last_season_num
            else:
                continue

        episodes = []
        current = header.find_next_sibling()

        while current:
            if current.name == "h2" and "the-anime-season" in current.get("class", []):
                break
            if current.name == "a" and "short-btn" in current.get("class", []) \
                    and "video" in current.get("class", []):
                ep = _parse_episode_link(current, slug, season_num, anime_title)
                if ep:
                    episodes.append(ep)

            current = current.find_next_sibling()

        if episodes:
            existing = next((s for s in seasons if s.season == season_num), None)
            if existing:
                existing.episodes.extend(episodes)
            else:
                seasons.append(Season(season=season_num, episodes=episodes))
            last_season_num = season_num

    return seasons


def _extract_season_number(text: str) -> Optional[int]:
    m = re.search(r"(\d+)\s*сезон", text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


def _extract_part_number(text: str) -> Optional[int]:
    m = re.search(r"(\d+)\s*часть", text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


def _parse_episode_link(
    tag, slug: str, season_num: int, anime_title: Optional[str] = None
) -> Optional[Episode]:
    href = tag.get("href", "")
    text = tag.get_text(strip=True)

    ep_num = None
    m = re.search(r"(\d+)\s*серия", text, re.IGNORECASE)
    if m:
        ep_num = int(m.group(1))

    if ep_num is None:
        m = re.search(r"/episode-(\d+)\.html", href)
        if m:
            ep_num = int(m.group(1))

    if ep_num is None:
        return None

    full_url = href if href.startswith("http") else f"{BASE_URL}{href}"

    return Episode(
        title=text or f"{ep_num} серия",
        url=full_url,
        slug=slug,
        season=season_num,
        episode_number=ep_num,
        anime_title=anime_title,
    )


def _fallback_parse_episodes(
    soup: BeautifulSoup, slug: str, anime_title: Optional[str] = None
) -> list[Season]:
    eps_by_season: dict[int, dict[int, Episode]] = {}

    season_re = re.compile(r"/season-(\d+)/episode-(\d+)\.html")
    plain_re = re.compile(r"/episode-(\d+)\.html")

    for link in soup.find_all("a", href=True):
        href = link["href"]

        path = href.split("jut.su", 1)[-1]
        if not path.startswith(f"/{slug}/"):
            continue

        m = season_re.search(href)
        if m:
            season_num, ep_num = int(m.group(1)), int(m.group(2))
        else:
            m = plain_re.search(href)
            if not m:
                continue
            season_num, ep_num = 1, int(m.group(1))

        text = link.get_text(strip=True) or f"{ep_num} серия"
        full_url = href if href.startswith("http") else f"{BASE_URL}{href}"

        ep = Episode(
            title=text,
            url=full_url,
            slug=slug,
            season=season_num,
            episode_number=ep_num,
            anime_title=anime_title,
        )
        eps_by_season.setdefault(season_num, {}).setdefault(ep_num, ep)

    return [
        Season(season=s, episodes=[eps_by_season[s][n] for n in sorted(eps_by_season[s])])
        for s in sorted(eps_by_season)
    ]


def parse_episode_page(url: str) -> Optional[list[VideoSource]]:
    if not has_auth_cookies() and not ensure_session():
        logger.warning("Нет авторизации на jut.su — парсинг эпизода может не сработать")

    sleep_blocking()

    client = get_client()
    headers = _base_headers(url)

    try:
        resp = client.get(url, headers=headers)
    except Exception as e:
        logger.error("Ошибка при загрузке эпизода %s: %s", url, e)
        return None

    if resp.status_code != 200:
        logger.error("Страница эпизода %s ответила %d", url, resp.status_code)
        return None

    soup = BeautifulSoup(resp.content, "lxml", from_encoding="windows-1251")

    video = soup.find("video", id="my-player")
    if not video:
        logger.error("Тег <video id='my-player'> не найден на %s", url)
        return None

    sources = []
    for s in video.find_all("source"):
        src = s.get("src", "").strip()
        label = s.get("label", s.get("res", "?"))
        fmt = s.get("type", "mp4")
        if src and "pixel.png" not in src:
            sources.append(VideoSource(url=src, quality=label, format=fmt))

    if not sources:
        has_stub = bool(video.find_all("source", src=lambda v: v and "pixel.png" in v))
        if has_stub:
            logger.warning(
                "На %s найдены только заглушки pixel.png — сервер не выдал подписанный URL "
                "(нужны авторизация и HTTP/2)",
                url,
            )
        else:
            logger.warning("На странице %s нет <source> тегов", url)
        return None

    return sources


def parse_episode_meta(url: str) -> Optional[EpisodeMeta]:
    # Проверка и восстановление сессии
    if not has_auth_cookies() and not ensure_session():
        logger.warning("Нет авторизации на jut.su — парсинг метаданных может не сработать")

    sleep_blocking()

    client = get_client()
    headers = _base_headers(url)

    try:
        resp = client.get(url, headers=headers)
    except Exception as e:
        logger.error("Ошибка при загрузке %s: %s", url, e)
        return None

    if resp.status_code != 200:
        return None

    soup = BeautifulSoup(resp.content, "lxml", from_encoding="windows-1251")

    meta = EpisodeMeta()

    dur_tag = soup.find("meta", itemprop="duration")
    if dur_tag and dur_tag.get("content"):
        meta.duration_str = dur_tag["content"]
        meta.duration_sec = _parse_iso8601_duration(dur_tag["content"])

    video = soup.find("video", id="my-player")
    if video and video.get("poster"):
        meta.poster_url = video["poster"]

    upload_tag = soup.find("meta", itemprop="uploadDate")
    if upload_tag and upload_tag.get("content"):
        meta.upload_date = upload_tag["content"]

    thumb_tag = soup.find("link", itemprop="thumbnailUrl")
    if thumb_tag and thumb_tag.get("href"):
        meta.thumbnail_url = thumb_tag["href"]

    return meta


def _parse_iso8601_duration(dur: str) -> Optional[int]:
    m = re.match(r"^P?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$", dur)
    if not m:
        return None
    hours = int(m.group(1)) if m.group(1) else 0
    mins = int(m.group(2)) if m.group(2) else 0
    secs = int(m.group(3)) if m.group(3) else 0
    return hours * 3600 + mins * 60 + secs



def select_quality(
    sources: list[VideoSource],
    preferred: str = "720p",
    fallback: str = "lower",
) -> Optional[VideoSource]:
    if not sources:
        return None

    def _quality_num(q: str) -> int:
        nums = re.findall(r"\d+", q)
        return int(nums[0]) if nums else 0

    sorted_sources = sorted(sources, key=lambda s: _quality_num(s.quality))
    pref_num = _quality_num(preferred)

    for s in sorted_sources:
        if _quality_num(s.quality) == pref_num:
            return s

    if fallback == "lower":
        lower = [s for s in sorted_sources if _quality_num(s.quality) < pref_num]
        if lower:
            return lower[-1]
        return min(sorted_sources, key=lambda s: abs(_quality_num(s.quality) - pref_num))
    else:
        higher = [s for s in sorted_sources if _quality_num(s.quality) > pref_num]
        if higher:
            return higher[0]
        return min(sorted_sources, key=lambda s: abs(_quality_num(s.quality) - pref_num))
