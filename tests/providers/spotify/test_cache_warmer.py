"""Tests for the Spotify cache warmer."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError, RetriesExhausted
from music_assistant_models.media_items import (
    Album,
    BrowseFolder,
    ItemMapping,
    ProviderMapping,
    RecommendationFolder,
    UniqueList,
)

from music_assistant.controllers.cache import use_cache
from music_assistant.mass import MusicAssistant
from music_assistant.providers.spotify import cache_warmer
from music_assistant.providers.spotify.cache_warmer import CacheWarmer
from music_assistant.providers.spotify.provider import SpotifyProvider

INSTANCE = "spotify--warmer-test"


class _Pages:
    """Stand-in for the provider's cached page methods, counting service fetches."""

    domain = "spotify"
    instance_id = INSTANCE

    def __init__(self, mass: MusicAssistant) -> None:
        self.mass = mass
        self.attempts: list[str] = []
        self.fetched: list[str] = []
        self.error: Exception | None = None

    @use_cache(3600)
    async def page(self, item_id: str) -> str:
        """Fetch one page."""
        self.attempts.append(item_id)
        if self.error is not None:
            raise self.error
        self.fetched.append(item_id)
        return item_id


@pytest.fixture
async def provider(mass_minimal: MusicAssistant) -> SpotifyProvider:
    """Return an available provider using the real SQLite cache."""
    await mass_minimal.cache._setup_database()
    prov = object.__new__(SpotifyProvider)
    prov.config = MagicMock(instance_id=INSTANCE)
    prov.manifest = MagicMock(domain="spotify")
    prov.logger = MagicMock()
    prov.mass = mass_minimal
    prov.available = True
    prov._audiobooks_supported = True
    return prov


@pytest.fixture
def pages(mass_minimal: MusicAssistant) -> _Pages:
    """Return the stand-in page methods."""
    return _Pages(mass_minimal)


@pytest.fixture
def warmer(
    provider: SpotifyProvider, pages: _Pages, monkeypatch: pytest.MonkeyPatch
) -> CacheWarmer:
    """Return a warmer whose pages each make one stand-in cached call."""
    monkeypatch.setattr(cache_warmer, "PAGE_PAUSE", 0)
    warmer = CacheWarmer(provider)
    monkeypatch.setattr(warmer, "_syncing", lambda: False)
    monkeypatch.setattr(
        warmer, "_page_calls", lambda _media_type, item_id: [(pages.page, (item_id,), {})]
    )
    return warmer


def _targets(warmer: CacheWarmer, monkeypatch: pytest.MonkeyPatch, *ids: str) -> None:
    monkeypatch.setattr(
        warmer, "_targets", AsyncMock(return_value=[(MediaType.ARTIST, i) for i in ids])
    )


async def test_run_fills_only_pages_not_cached(
    warmer: CacheWarmer, pages: _Pages, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page already in the cache is skipped without calling Spotify."""
    await pages.page("cached")
    pages.fetched.clear()
    _targets(warmer, monkeypatch, "cached", "new")

    counts = await warmer.run()

    assert pages.fetched == ["new"]
    assert counts == {"pages": 2, "filled": 1, "failed": 0, "deferred": 0}
    # the next run finds everything cached
    assert (await warmer.run())["filled"] == 0
    assert pages.fetched == ["new"]


async def test_run_stops_filling_at_the_page_budget(
    warmer: CacheWarmer, pages: _Pages, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pages beyond the per-run budget wait for the next run."""
    monkeypatch.setattr(cache_warmer, "PAGES_PER_RUN", 2)
    _targets(warmer, monkeypatch, "a", "b", "c")

    counts = await warmer.run()

    assert pages.fetched == ["a", "b"]
    assert counts["deferred"] == 1


async def test_rate_limit_ends_the_run(
    warmer: CacheWarmer, pages: _Pages, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When Spotify asks to back off, no further page is requested."""
    pages.error = RetriesExhausted("Not retrying, asked to wait 600 seconds")
    _targets(warmer, monkeypatch, "a", "b")

    counts = await warmer.run()

    assert pages.attempts == ["a"]
    assert counts["failed"] == 1


async def test_repeated_failures_end_the_run(
    warmer: CacheWarmer, pages: _Pages, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that keeps failing stops rather than walking the whole list."""
    pages.error = MediaNotFoundError("gone")
    _targets(warmer, monkeypatch, *(str(i) for i in range(10)))

    counts = await warmer.run()

    assert counts["failed"] == cache_warmer.MAX_CONSECUTIVE_FAILURES
    assert len(pages.attempts) == cache_warmer.MAX_CONSECUTIVE_FAILURES


async def test_unavailable_provider_is_not_filled(
    warmer: CacheWarmer, provider: SpotifyProvider
) -> None:
    """Nothing is fetched while Spotify is unavailable."""
    provider.available = False
    with pytest.raises(RuntimeError):
        await warmer.run()


async def test_targets_put_discover_first_and_skip_other_providers(
    provider: SpotifyProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discover's Spotify items come first, the library's follow, each listed once."""
    rows = [RecommendationFolder(item_id="row", provider="lastfm", name="Row")]
    library_album = Album(
        provider="library",
        item_id="12",
        name="A",
        provider_mappings={
            ProviderMapping(item_id="al1", provider_domain="spotify", provider_instance=INSTANCE)
        },
    )
    items = UniqueList(
        [
            ItemMapping(media_type=MediaType.PLAYLIST, item_id="pl1", provider=INSTANCE, name="P"),
            ItemMapping(media_type=MediaType.TRACK, item_id="t9", provider="tidal--x", name="T"),
            BrowseFolder(item_id="folder", provider=INSTANCE, path="x", name="F"),
            library_album,
        ]
    )
    recommendations = SimpleNamespace(
        get_recommendations=AsyncMock(return_value=rows),
        get_recommendation_items=AsyncMock(return_value=items),
    )
    database = SimpleNamespace(
        get_rows_from_query=AsyncMock(
            return_value=[
                {"media_type": "track", "provider_item_id": "t1"},
                {"media_type": "radio", "provider_item_id": "r1"},
                {"media_type": "artist", "provider_item_id": "ar1"},
                {"media_type": "playlist", "provider_item_id": "pl1"},
            ]
        )
    )
    music = SimpleNamespace(recommendations=recommendations, database=database)
    monkeypatch.setattr(provider.mass, "music", music, raising=False)
    warmer = CacheWarmer(provider)

    targets: list[tuple[MediaType, Any]] = await warmer._targets()

    assert targets == [
        (MediaType.PLAYLIST, "pl1"),
        (MediaType.ALBUM, "al1"),
        (MediaType.ARTIST, "ar1"),
        (MediaType.TRACK, "t1"),
    ]


@pytest.mark.parametrize(
    "media_type",
    [
        MediaType.ARTIST,
        MediaType.ALBUM,
        MediaType.PLAYLIST,
        MediaType.PODCAST,
        MediaType.AUDIOBOOK,
        MediaType.TRACK,
    ],
)
async def test_every_page_call_is_a_cached_method(
    provider: SpotifyProvider, media_type: MediaType
) -> None:
    """Each page is warmed through cached methods only, so a fill is a cache check first."""
    calls = CacheWarmer(provider)._page_calls(media_type, "id1")

    assert calls
    for method, _args, _kwargs in calls:
        assert hasattr(method.__func__, "_use_cache_options"), method.__name__  # type: ignore[attr-defined]
