"""Read public stats.fm listening rankings for recommendation seeds."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote, urlsplit

from aiohttp import ClientError, ClientTimeout
from music_assistant_models.errors import InvalidDataError, ResourceTemporarilyUnavailable

from music_assistant.helpers.throttle_retry import ThrottlerManager

if TYPE_CHECKING:
    from music_assistant.providers.lastfm_recommendations import LastFMRecommendationsProvider

CONF_STATSFM_USERNAME = "statsfm_username"
CONF_STATSFM_PERIOD = "statsfm_period"
CACHE_CATEGORY_STATSFM = 4
CACHE_EXPIRATION_STATSFM = 6 * 60 * 60
STATSFM_LIMIT = 20
STATSFM_PERIODS = {"weeks": "Past 4 weeks", "months": "Past 6 months", "lifetime": "All time"}


def normalize_username(value: str) -> str:
    """Accept a stats.fm username or profile URL without permitting arbitrary API hosts."""
    value = value.strip()
    if value.startswith(("https://", "http://")):
        url = urlsplit(value)
        if url.hostname not in ("stats.fm", "www.stats.fm"):
            raise InvalidDataError("Enter a stats.fm username or profile URL")
        value = url.path.strip("/")
    if not value or any(char in value for char in "/?#\\"):
        raise InvalidDataError("Enter a single stats.fm username or profile URL")
    return value


class StatsFMAPIClient:
    """Fetch and cache bounded public rankings without credentials or page scraping."""

    BASE_URL = "https://api.stats.fm/api/v1/users"
    throttler = ThrottlerManager(rate_limit=1, period=1)

    def __init__(self, provider: LastFMRecommendationsProvider) -> None:
        """Initialize the client with the provider's shared HTTP session and cache."""
        self.provider = provider

    async def get_top_items(
        self, username: str, period: str, kind: Literal["artists", "tracks"]
    ) -> list[dict[str, Any]]:
        """Return ranked artists or tracks using the user's stats.fm sorting preference."""
        username = normalize_username(username)
        if period not in STATSFM_PERIODS:
            raise InvalidDataError("Unsupported stats.fm listening period")
        cache_key = f"statsfm_v1:{username}:{period}:{kind}"
        cache = self.provider.mass.cache
        cached = await cache.get(
            cache_key, provider=self.provider.instance_id, category=CACHE_CATEGORY_STATSFM
        )
        if isinstance(cached, list):
            return cached
        url = f"{self.BASE_URL}/{quote(username, safe='')}/top/{kind}"
        try:
            async with (
                self.throttler.acquire(),
                self.provider.mass.http_session.get(
                    url,
                    params={"range": period, "limit": STATSFM_LIMIT},
                    timeout=ClientTimeout(total=20),
                    headers={"User-Agent": "MusicAssistant/stats.fm"},
                ) as response,
            ):
                if response.status in (401, 403, 404):
                    raise InvalidDataError(
                        "stats.fm rankings are not public or the profile does not exist"
                    )
                if response.status == 429:
                    raise ResourceTemporarilyUnavailable("stats.fm rate limit reached")
                response.raise_for_status()
                data = await response.json()
        except (TimeoutError, ClientError) as err:
            raise ResourceTemporarilyUnavailable("Unable to read stats.fm rankings") from err
        except ValueError as err:
            raise InvalidDataError("Invalid stats.fm response") from err
        items = _parse_rankings(data, kind)
        await cache.set(
            cache_key,
            items,
            provider=self.provider.instance_id,
            category=CACHE_CATEGORY_STATSFM,
            expiration=CACHE_EXPIRATION_STATSFM,
        )
        return items


def _parse_rankings(data: Any, kind: Literal["artists", "tracks"]) -> list[dict[str, Any]]:
    """Validate rankings and retain only the catalog fields needed for recommendations."""
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise InvalidDataError("stats.fm response is missing its rankings")
    ranked: list[tuple[int, dict[str, Any]]] = []
    key = "artist" if kind == "artists" else "track"
    for index, row in enumerate(data["items"][:STATSFM_LIMIT]):
        if not isinstance(row, dict) or not isinstance(item := row.get(key), dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        normalized: dict[str, Any] = {"name": name.strip()}
        if kind == "tracks":
            artists = item.get("artists")
            if not isinstance(artists, list):
                continue
            names = list(
                dict.fromkeys(
                    artist["name"].strip()
                    for artist in artists
                    if isinstance(artist, dict)
                    and isinstance(artist.get("name"), str)
                    and artist["name"].strip()
                )
            )
            if not names:
                continue
            normalized["artists"] = names
        position = row.get("position")
        ranked.append(
            (position if isinstance(position, int) and position > 0 else index + 1, normalized)
        )
    ranked.sort(key=lambda entry: entry[0])
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for _, item in ranked:
        identity = (
            item["name"].casefold(),
            tuple(sorted(name.casefold() for name in item.get("artists", []))),
        )
        if identity not in seen:
            result.append(item)
            seen.add(identity)
    if data["items"] and not result:
        raise InvalidDataError("stats.fm rankings contain no usable catalog entries")
    return result
