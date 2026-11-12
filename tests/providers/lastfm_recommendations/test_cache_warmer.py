"""Tests for the Last.fm artist panel cache warmer."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ExternalID, MediaType
from music_assistant_models.media_items import (
    Artist,
    ItemMapping,
    ProviderMapping,
    RecommendationFolder,
    Track,
    UniqueList,
)

from music_assistant.controllers.cache import CacheStatus, cache_status
from music_assistant.mass import MusicAssistant
from music_assistant.providers.lastfm_recommendations import LastFMRecommendationsProvider
from music_assistant.providers.lastfm_recommendations.cache_warmer import LastFMCacheWarmer

INSTANCE = "lastfm--warmer-test"
MBID = "f4fdbb4c-e4b7-47a0-b83b-d91bbfcfa387"


@pytest.fixture
async def provider(mass_minimal: MusicAssistant) -> LastFMRecommendationsProvider:
    """Return a provider using the real SQLite cache, with Last.fm's API stubbed."""
    await mass_minimal.cache._setup_database()
    prov = object.__new__(LastFMRecommendationsProvider)
    prov.config = MagicMock(instance_id=INSTANCE)
    prov.manifest = MagicMock(domain="lastfm_recommendations")
    prov.logger = MagicMock()
    prov.mass = mass_minimal
    prov.api = MagicMock(
        get_artist_top_tracks=AsyncMock(return_value=[]),
        get_similar_artists=AsyncMock(return_value=[]),
    )
    prov.recommendations_manager = MagicMock()
    return prov


def _artist(name: str, mbid: str | None = None) -> Artist:
    artist = Artist(
        item_id="7",
        provider="library",
        name=name,
        provider_mappings={
            ProviderMapping(
                item_id="sp1", provider_domain="spotify", provider_instance="spotify--x"
            )
        },
    )
    if mbid:
        artist.add_external_id(ExternalID.MB_ARTIST, mbid)
    return artist


async def test_page_calls_fill_the_entries_the_artist_page_reads(
    provider: LastFMRecommendationsProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The warmer's calls hit the keys the page's top tracks and similar artists read."""
    artist = _artist("Ariana Grande", MBID)
    artists = SimpleNamespace(get_library_item=AsyncMock(return_value=artist))
    monkeypatch.setattr(provider.mass, "music", SimpleNamespace(artists=artists), raising=False)
    calls = await LastFMCacheWarmer(provider)._page_calls(("7", "library"))

    # the page's requests: top tracks at the default limit, similar artists at 25
    await provider.get_artist_toptracks(artist)
    await provider.get_similar_artists(artist, limit=25)

    assert len(calls) == 2
    for method, args, kwargs in calls:
        assert await cache_status(method, *args, **kwargs) is CacheStatus.FRESH


async def test_page_calls_look_up_provider_artists_like_the_page(
    provider: LastFMRecommendationsProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Discover artist is looked up through the library first, without a metadata update."""
    artists = SimpleNamespace(get=AsyncMock(return_value=_artist("Billie Holiday")))
    monkeypatch.setattr(provider.mass, "music", SimpleNamespace(artists=artists), raising=False)

    calls = await LastFMCacheWarmer(provider)._page_calls(("sp1", "spotify--x"))

    artists.get.assert_awaited_once_with("sp1", "spotify--x", allow_update_metadata=False)
    assert [args for _method, args, _kwargs in calls] == [
        ("Billie Holiday", None, 25),
        ("Billie Holiday", None, 25),
    ]


async def test_targets_put_discover_artists_first(
    provider: LastFMRecommendationsProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discover's artists come first, then the library's, each listed once."""
    rows = [RecommendationFolder(item_id="row", provider="spotify--x", name="Row")]
    items = UniqueList(
        [
            ItemMapping(
                media_type=MediaType.ARTIST, item_id="sp1", provider="spotify--x", name="A"
            ),
            Track(item_id="t1", provider="spotify--x", name="T", provider_mappings=set()),
            ItemMapping(media_type=MediaType.ARTIST, item_id="3", provider="library", name="B"),
        ]
    )
    recommendations = SimpleNamespace(
        get_recommendations=AsyncMock(return_value=rows),
        get_recommendation_items=AsyncMock(return_value=items),
    )
    database = SimpleNamespace(
        get_rows_from_query=AsyncMock(return_value=[{"item_id": 3}, {"item_id": 9}])
    )
    music = SimpleNamespace(recommendations=recommendations, database=database)
    monkeypatch.setattr(provider.mass, "music", music, raising=False)

    targets = await LastFMCacheWarmer(provider)._targets()

    assert targets == [("sp1", "spotify--x"), ("3", "library"), ("9", "library")]


async def test_paused_without_a_searchable_streaming_provider(
    provider: LastFMRecommendationsProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Panels are not resolved while no streaming provider can be searched."""
    warmer = LastFMCacheWarmer(provider)
    monkeypatch.setattr(warmer, "_tasks_active", lambda _prefix: False)
    monkeypatch.setattr(
        "music_assistant.providers.lastfm_recommendations.cache_warmer._music_providers_loading",
        AsyncMock(return_value=False),
    )
    spotify = SimpleNamespace(is_streaming_provider=True, available=False)
    monkeypatch.setattr(provider.mass, "music", SimpleNamespace(providers=[spotify]), raising=False)

    assert await warmer._paused()
    spotify.available = True
    assert not await warmer._paused()
