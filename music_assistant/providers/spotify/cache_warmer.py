"""Background filling of the Spotify pages people are likely to open."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import BrowseFolder, ItemMapping, MediaItemType

from music_assistant.constants import DB_TABLE_PROVIDER_MAPPINGS
from music_assistant.controllers.cache import CacheCall, CacheWarmer

if TYPE_CHECKING:
    from .provider import SpotifyProvider

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


class SpotifyCacheWarmer(CacheWarmer[tuple[MediaType, str]]):
    """Keep the cache behind Spotify's Discover and library pages filled."""

    provider: SpotifyProvider

    async def _paused(self) -> bool:
        """Return whether a Spotify library sync is running, which takes priority."""
        return self._tasks_active(f"music_sync_{self.provider.instance_id}_")

    async def _page_calls(self, target: tuple[MediaType, str]) -> list[CacheCall]:
        """Return the cached calls that a page of this item makes, as its callers make them."""
        media_type, item_id = target
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
