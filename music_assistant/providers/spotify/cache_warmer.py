"""Background filling of the Spotify pages people are likely to open."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from music_assistant_models.auth import Scope
from music_assistant_models.enums import MediaType, TaskStatus
from music_assistant_models.errors import ResourceTemporarilyUnavailable, RetriesExhausted
from music_assistant_models.media_items import BrowseFolder, ItemMapping, MediaItemType

from music_assistant.constants import DB_TABLE_PROVIDER_MAPPINGS
from music_assistant.controllers.cache import needs_cache_fill

if TYPE_CHECKING:
    from music_assistant_models.background_task import BackgroundTask

    from .provider import SpotifyProvider

type CacheCall = tuple[Callable[..., Awaitable[Any]], tuple[Any, ...], dict[str, Any]]

INTERVAL = 6 * 3600
FIRST_RUN_DELAY = 5 * 60
# at most this many pages are filled per run, so a cold cache fills over several runs
PAGES_PER_RUN = 200
# pause between pages, leaving the shared Spotify throttle free for people browsing
PAGE_PAUSE = 4.0
# consecutive failed pages after which the run stops until the next interval
MAX_CONSECUTIVE_FAILURES = 5
# the limit the track pages ask similar tracks for, which is part of the cache key
SIMILAR_TRACKS_LIMIT = 25
LIBRARY_ORDER = (
    MediaType.ARTIST,
    MediaType.ALBUM,
    MediaType.PLAYLIST,
    MediaType.PODCAST,
    MediaType.AUDIOBOOK,
    MediaType.TRACK,
)


class CacheWarmer:
    """Fill the cache behind Spotify pages before anyone opens them."""

    def __init__(self, provider: SpotifyProvider) -> None:
        """Initialize the warmer attached to a loaded Spotify provider."""
        self.provider = provider
        self.mass = provider.mass
        self.task_id = f"spotify_cache_warmer_{provider.instance_id}"
        self.timer_id = f"{self.task_id}_timer"
        self.lock = asyncio.Lock()
        self.unregister = self.mass.register_api_command(
            f"spotify/{provider.instance_id}/warm_cache",
            self.trigger,
            required_scope=Scope.LIBRARY_MANAGE,
        )

    def start(self) -> None:
        """Schedule the first run after provider startup."""
        self.mass.call_later(FIRST_RUN_DELAY, self.queue, task_id=self.timer_id)

    async def stop(self) -> None:
        """Stop the timer and drain a running fill before provider unload."""
        self.mass.cancel_timer(self.timer_id)
        self.unregister()
        await self.mass.tasks.unregister_scheduled_task_and_wait(
            self.task_id, clear_persisted_state=False
        )

    async def trigger(self) -> BackgroundTask:
        """Queue a run without overlapping an existing one."""
        return self.queue()

    def queue(self) -> BackgroundTask:
        """Queue work in the visible task manager and schedule the next run."""
        self.mass.call_later(INTERVAL, self.queue, task_id=self.timer_id)
        return self.mass.tasks.run_background_task(
            task_id=self.task_id,
            name=f"Keep {self.provider.name} pages cached (every 6 hours)",
            handler=self.run,
            metadata={"provider_instance": self.provider.instance_id},
        )

    async def run(self) -> dict[str, int]:
        """
        Fill the cache entries of Discover and library pages that a visit would fetch.

        Pages whose entries are already cached are skipped without calling Spotify.
        """
        async with self.lock:
            if not self.provider.available:
                raise RuntimeError("Spotify is unavailable; the cache was not filled")
            counts = {"pages": 0, "filled": 0, "failed": 0, "deferred": 0}
            failures = 0
            for media_type, item_id in await self._targets():
                calls = [
                    (method, args, kwargs)
                    for method, args, kwargs in self._page_calls(media_type, item_id)
                    if await needs_cache_fill(method, *args, **kwargs)
                ]
                counts["pages"] += 1
                if not calls:
                    continue
                if counts["filled"] + counts["failed"] >= PAGES_PER_RUN or self._syncing():
                    counts["deferred"] += 1
                    continue
                try:
                    for method, args, kwargs in calls:
                        await method(*args, **kwargs)
                except ResourceTemporarilyUnavailable, RetriesExhausted:
                    # Spotify asked us to back off: leave the rest for the next run
                    self.provider.logger.info("Spotify cache fill paused by a rate limit")
                    counts["failed"] += 1
                    break
                except Exception as err:
                    self.provider.logger.debug(
                        "Could not cache %s %s: %s", media_type.value, item_id, err
                    )
                    counts["failed"] += 1
                    failures += 1
                    if failures >= MAX_CONSECUTIVE_FAILURES:
                        break
                else:
                    counts["filled"] += 1
                    failures = 0
                await asyncio.sleep(PAGE_PAUSE)
            self.provider.logger.info("Spotify cache fill complete: %s", counts)
            return counts

    def _page_calls(self, media_type: MediaType, item_id: str) -> list[CacheCall]:
        """Return the cached calls that a page of this item makes, as its callers make them."""
        prov = self.provider
        match media_type:
            case MediaType.ARTIST:
                return [
                    (prov.get_artist, (item_id,), {}),
                    (prov.get_artist_albums, (item_id,), {}),
                    (prov.get_artist_toptracks, (item_id,), {}),
                ]
            case MediaType.ALBUM:
                # get_album reads the cached album data, which get_album_tracks reuses
                return [
                    (prov._get_album_data, (item_id,), {}),
                    (prov.get_album_tracks, (item_id,), {}),
                ]
            case MediaType.PLAYLIST:
                return [
                    (prov.get_playlist, (item_id,), {}),
                    (prov.get_playlist_tracks, (item_id,), {"page": 0}),
                ]
            case MediaType.PODCAST:
                return [
                    (prov.get_podcast, (item_id,), {}),
                    (prov._get_podcast_episodes_data, (item_id,), {}),
                ]
            case MediaType.AUDIOBOOK if prov.audiobooks_supported:
                return [(prov.get_audiobook, (item_id,), {})]
            case MediaType.TRACK:
                return [
                    (prov.get_track, (item_id,), {}),
                    (prov.get_similar_tracks, (item_id,), {"limit": SIMILAR_TRACKS_LIMIT}),
                ]
        return []

    async def _targets(self) -> list[tuple[MediaType, str]]:
        """Return the Spotify items to cache: Discover's first, then the library's."""
        targets = dict.fromkeys(await self._discover_targets())
        targets.update(dict.fromkeys(await self._library_targets()))
        return list(targets)

    async def _discover_targets(self) -> list[tuple[MediaType, str]]:
        """Return the Spotify items on the Discover rows, in row order."""
        recommendations = self.mass.music.recommendations
        targets: list[tuple[MediaType, str]] = []
        for row in await recommendations.get_recommendations():
            for item in await recommendations.get_recommendation_items(row.provider, row.item_id):
                if not isinstance(item, BrowseFolder) and (item_id := self._spotify_id(item)):
                    targets.append((item.media_type, item_id))
        return targets

    async def _library_targets(self) -> list[tuple[MediaType, str]]:
        """Return the library's Spotify items, ordered by the kind of page they open."""
        rows = await self.mass.music.database.get_rows_from_query(
            f"SELECT media_type, provider_item_id FROM {DB_TABLE_PROVIDER_MAPPINGS} "
            "WHERE provider_instance = :provider_instance AND available = 1",
            {"provider_instance": self.provider.instance_id},
            limit=0,
        )
        order = {media_type.value: index for index, media_type in enumerate(LIBRARY_ORDER)}
        return [
            (MediaType(row["media_type"]), row["provider_item_id"])
            for row in sorted(rows, key=lambda row: order.get(row["media_type"], len(order)))
            if row["media_type"] in order
        ]

    def _spotify_id(self, item: MediaItemType | ItemMapping) -> str | None:
        """Return the item's id on this Spotify instance, if it has one."""
        if item.provider == self.provider.instance_id:
            return item.item_id
        for mapping in getattr(item, "provider_mappings", ()):
            if mapping.provider_instance == self.provider.instance_id:
                return str(mapping.item_id)
        return None

    def _syncing(self) -> bool:
        """Return whether a Spotify library sync is running, which takes priority."""
        active = (TaskStatus.RUNNING, TaskStatus.PENDING)
        prefix = f"music_sync_{self.provider.instance_id}_"
        return any(
            task.id.startswith(prefix) and task.status in active
            for task in self.mass.tasks.list_tasks()
        )
