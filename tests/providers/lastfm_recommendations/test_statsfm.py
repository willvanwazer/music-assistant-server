"""Tests for public stats.fm rankings and their use as discovery seeds."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import ClientConnectionError
from music_assistant_models.errors import InvalidDataError, ResourceTemporarilyUnavailable
from music_assistant_models.media_items import Artist, Track

from music_assistant.providers.lastfm_recommendations.recommendations import (
    LastFMRecommendationManager,
    _SeedTrack,
)
from music_assistant.providers.lastfm_recommendations.statsfm import (
    CACHE_CATEGORY_STATSFM,
    StatsFMAPIClient,
    _parse_rankings,
    normalize_username,
)


class FakeCache:
    """Store responses by full provider/category/key identity."""

    def __init__(self) -> None:
        """Initialize an empty cache."""
        self.items: dict[tuple[str, int, str], Any] = {}

    async def get(self, key: str, *, provider: str, category: int) -> Any:
        """Retrieve a response."""
        return self.items.get((provider, category, key))

    async def set(
        self, key: str, data: Any, *, provider: str, category: int, **kwargs: Any
    ) -> None:
        """Save a response."""
        self.items[provider, category, key] = data


@pytest.fixture
def provider() -> Mock:
    """Return a provider with real cache behavior and no network access."""
    provider = Mock()
    provider.instance_id = "lastfm_recommendations--test"
    provider.mass.cache = FakeCache()
    response = AsyncMock()
    response.status = 200
    response.raise_for_status = Mock()
    response.json.return_value = {"items": []}
    provider.mass.http_session.get.return_value.__aenter__ = AsyncMock(return_value=response)
    provider.mass.http_session.get.return_value.__aexit__ = AsyncMock(return_value=False)
    settings = {
        "enable_personalized": True,
        "statsfm_username": "listener",
        "statsfm_period": "months",
    }
    provider.config.get_value.side_effect = lambda key, default=None: settings.get(key, default)
    return provider


@pytest.mark.parametrize(
    "value", ["listener", "https://stats.fm/listener", "https://www.stats.fm/listener/"]
)
def test_profile_username(value: str) -> None:
    """Profile URLs resolve to usernames, without arbitrary request destinations."""
    assert normalize_username(value) == "listener"


@pytest.mark.parametrize(
    "value", ["https://example.com/listener", "../listener", "a/b", "", "listener?x=1"]
)
def test_invalid_profile(value: str) -> None:
    """Reject paths and foreign hosts before calling the API."""
    with pytest.raises(InvalidDataError):
        normalize_username(value)


def test_artist_ranking_preserves_positions_and_deduplicates() -> None:
    """The actual stats.fm rank wins over payload order or apparent stream counts."""
    rows = [
        {"position": 2, "streams": 90, "artist": {"name": "Second"}},
        {"position": 1, "streams": 20, "artist": {"name": "First"}},
        {"position": 3, "artist": {"name": "FIRST"}},
        {"position": 4, "artist": {"name": ""}},
    ]
    assert _parse_rankings({"items": rows}, "artists") == [{"name": "First"}, {"name": "Second"}]


def test_track_ranking_preserves_collaborators_and_omits_preview_urls() -> None:
    """Keep artist credits for matching, without retaining unrelated playback data."""
    track = {
        "name": "Duet",
        "artists": [{"name": "Guest"}, {"name": "Lead"}],
        "appleMusicPreview": "https://example.com/audio",
    }
    assert _parse_rankings({"items": [{"position": 1, "track": track}]}, "tracks") == [
        {"name": "Duet", "artists": ["Guest", "Lead"]}
    ]


@pytest.mark.parametrize(
    "data", [{}, {"items": None}, {"items": [{"track": {"name": "Missing credits"}}]}]
)
def test_malformed_ranking_is_not_an_empty_history(data: Any) -> None:
    """Invalid payloads cannot replace a previously working recommendation generation."""
    with pytest.raises(InvalidDataError):
        _parse_rankings(data, "tracks")


async def test_cached_rankings_are_scoped_to_profile_and_period(provider: Mock) -> None:
    """Cache empty and populated successes, but never reuse another profile or time period."""
    client = StatsFMAPIClient(provider)
    response = await provider.mass.http_session.get.return_value.__aenter__()
    response.json.return_value = {"items": [{"position": 1, "artist": {"name": "First"}}]}
    assert await client.get_top_items("listener", "months", "artists") == [{"name": "First"}]
    assert await client.get_top_items("https://stats.fm/listener", "months", "artists") == [
        {"name": "First"}
    ]
    assert provider.mass.http_session.get.call_count == 1
    kwargs = provider.mass.http_session.get.call_args.kwargs
    assert kwargs["params"] == {"range": "months", "limit": 20}
    response.json.return_value = {"items": []}
    assert await client.get_top_items("listener", "weeks", "artists") == []
    assert await client.get_top_items("listener", "weeks", "artists") == []
    assert await client.get_top_items("another", "months", "artists") == []
    assert provider.mass.http_session.get.call_count == 3


@pytest.mark.parametrize("status", [401, 403, 404, 429])
async def test_access_and_rate_limit_errors_are_not_cached(provider: Mock, status: int) -> None:
    """Private profiles and throttling remain errors instead of successful empty rankings."""
    response = await provider.mass.http_session.get.return_value.__aenter__()
    response.status = status
    with pytest.raises((InvalidDataError, ResourceTemporarilyUnavailable)):
        await StatsFMAPIClient(provider).get_top_items("listener", "months", "artists")
    assert provider.mass.cache.items == {}


async def test_connection_error_is_reported(provider: Mock) -> None:
    """Network failures use the provider's recoverable-error path."""
    provider.mass.http_session.get.side_effect = ClientConnectionError()
    with pytest.raises(ResourceTemporarilyUnavailable):
        await StatsFMAPIClient(provider).get_top_items("listener", "months", "artists")


async def test_statsfm_rankings_drive_top_artists_and_existing_discovery_rows(
    provider: Mock,
) -> None:
    """Ranked seeds bypass local play recency and retain owned artists in the top-artists row."""
    manager = LastFMRecommendationManager(provider)
    manager.statsfm.get_top_items = AsyncMock(
        side_effect=[
            [{"name": f"Artist {n}"} for n in range(20)],
            [{"name": f"Track {n}", "artists": [f"Artist {n}"]} for n in range(20)],
        ]
    )
    artists = [
        Artist(item_id=str(n), provider="library", name=f"Artist {n}", provider_mappings=set())
        for n in range(20)
    ]
    manager.get_or_resolve_artist = AsyncMock(side_effect=artists)
    manager._get_recent_play_seeds = AsyncMock(side_effect=AssertionError("Must use stats.fm"))
    manager._get_similar_artists_from_seeds = AsyncMock(return_value=[artists[0]])
    manager._get_similar_tracks_from_seeds = AsyncMock(
        return_value=[
            Track(item_id="other", provider="spotify", name="Other", provider_mappings=set())
        ]
    )
    rows = [row async for row in manager._get_personalized_recommendations()]
    assert [row.item_id for row in rows] == [
        f"{provider.instance_id}_{suffix}"
        for suffix in ("statsfm_top_artists", "similar_artists", "similar_tracks")
    ]
    assert list(rows[0].items) == artists
    assert rows[0].translation_key == "statsfm_top_artists_months"
    assert rows[1].translation_params == ["5"]
    assert rows[2].translation_params == ["20"]
    manager._get_similar_artists_from_seeds.assert_awaited_once_with(
        [f"Artist {n}" for n in range(5)]
    )
    manager._get_similar_tracks_from_seeds.assert_awaited_once_with(
        [_SeedTrack(f"Artist {n}", f"Track {n}") for n in range(20)]
    )
    manager._get_recent_play_seeds.assert_not_awaited()


async def test_statsfm_failure_does_not_fall_back_to_recent_plays(provider: Mock) -> None:
    """Never mislabel recent-play approximations as stats.fm top charts."""
    manager = LastFMRecommendationManager(provider)
    manager.statsfm.get_top_items = AsyncMock(side_effect=ResourceTemporarilyUnavailable("offline"))
    manager._get_recent_play_seeds = AsyncMock()
    with pytest.raises(ResourceTemporarilyUnavailable):
        _ = [row async for row in manager._get_personalized_recommendations()]
    manager._get_recent_play_seeds.assert_not_awaited()


async def test_collaboration_tries_alternate_artist_only_when_needed(provider: Mock) -> None:
    """Stats.fm credits may not put the artist Last.fm indexes first."""
    manager = LastFMRecommendationManager(provider)
    result = [{"name": "Similar", "artist": {"name": "Other"}}]
    manager.api.get_similar_tracks = AsyncMock(side_effect=[[], result])
    assert (
        await manager._get_similar_tracks_for_seed(_SeedTrack("Guest", "Duet", ("Lead",))) == result
    )
    assert [
        call.kwargs["artist_name"] for call in manager.api.get_similar_tracks.await_args_list
    ] == ["Guest", "Lead"]


async def test_manual_cache_refresh_keeps_visible_rows_until_replacement(provider: Mock) -> None:
    """Explicit refresh clears stats.fm data while the old generation remains usable."""
    provider.mass.cache = Mock(clear=AsyncMock())
    rows = [Mock()]
    provider._recommendation_folders = rows
    await LastFMRecommendationManager(provider).clear_cache()
    assert provider._recommendation_folders == rows
    provider.mass.cache.clear.assert_any_await(
        category_filter=CACHE_CATEGORY_STATSFM, provider_filter=provider.instance_id
    )
