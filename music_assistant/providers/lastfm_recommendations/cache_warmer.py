"""Background filling of the Last.fm panels on the artist pages people are likely to open."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from music_assistant_models.enums import ExternalID, MediaType

from music_assistant.constants import DB_TABLE_ARTISTS
from music_assistant.controllers.cache import CacheCall, CacheWarmer

from .parsers import _music_providers_loading

if TYPE_CHECKING:
    from . import LastFMRecommendationsProvider

# the limits the artist pages ask Last.fm for, which are part of the cache keys
TOP_TRACKS_LIMIT = 25
SIMILAR_ARTISTS_LIMIT = 25


class LastFMCacheWarmer(CacheWarmer[tuple[str, str]]):
    """
    Keep an artist's Last.fm top tracks and similar artists cached for its page.

    Pages are (item_id, provider) pairs as the apps open them. Filling a panel resolves
    its Last.fm items through searches on the streaming providers, which go through those
    providers' throttles, so a run fills fewer pages than the Spotify warmer.
    """

    provider: LastFMRecommendationsProvider
    pages_per_run: ClassVar[int] = 100
    subject: ClassVar[str] = "artist panels"

    async def _paused(self) -> bool:
        """
        Return whether resolving Last.fm items could not complete now.

        A panel resolved while no streaming provider can be searched would be cached
        without the items that provider has.
        """
        if self._tasks_active("music_sync_") or await _music_providers_loading(self.mass):
            return True
        return not any(
            prov.is_streaming_provider and prov.available for prov in self.mass.music.providers
        )

    async def _targets(self) -> list[tuple[str, str]]:
        """Return the artist pages to cache: Discover's artists first, then the library's."""
        recommendations = self.mass.music.recommendations
        targets: dict[tuple[str, str], None] = {}
        for row in await recommendations.get_recommendations():
            for item in await recommendations.get_recommendation_items(row.provider, row.item_id):
                if item.media_type == MediaType.ARTIST:
                    targets[(str(item.item_id), item.provider)] = None
        library = await self.mass.music.database.get_rows_from_query(
            f"SELECT item_id FROM {DB_TABLE_ARTISTS} ORDER BY favorite DESC, sort_name",
            limit=0,
        )
        for db_row in library:
            targets[(str(db_row["item_id"]), "library")] = None
        return list(targets)

    async def _page_calls(self, target: tuple[str, str]) -> list[CacheCall]:
        """Return the Last.fm calls an artist page makes, with the artist it looks up."""
        item_id, provider = target
        artists = self.mass.music.artists
        if provider == "library":
            artist = await artists.get_library_item(item_id)
        else:
            # the page looks the artist up the same way: the library's copy when there is one
            artist = await artists.get(item_id, provider, allow_update_metadata=False)
        mbid = artist.get_external_id(ExternalID.MB_ARTIST)
        prov = self.provider
        return [
            (prov._get_artist_toptracks, (artist.name, mbid, TOP_TRACKS_LIMIT), {}),
            (prov._get_similar_artists, (artist.name, mbid, SIMILAR_ARTISTS_LIMIT), {}),
        ]
