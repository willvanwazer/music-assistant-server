"""Regression coverage for persistent Last.fm API and resolved listing caches."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import InvalidToken
from music_assistant_models.media_items import Artist

from music_assistant.mass import MusicAssistant
from music_assistant.providers.lastfm_recommendations import LastFMRecommendationsProvider
from music_assistant.providers.lastfm_recommendations.api_client import LastFMAPIClient
from music_assistant.providers.lastfm_recommendations.constants import CACHE_CATEGORY_API


@pytest.fixture
async def provider(mass_minimal: MusicAssistant) -> LastFMRecommendationsProvider:
    """Use the real SQLite cache and production task deduplication."""
    await mass_minimal.cache._setup_database()
    mass_minimal._http_session = MagicMock()
    prov = object.__new__(LastFMRecommendationsProvider)
    prov.config = MagicMock(instance_id="lastfm--cache-test")
    prov.config.get_value.return_value = "test-key"
    prov.manifest = MagicMock(domain="lastfm_recommendations")
    prov.logger = MagicMock()
    prov.mass = mass_minimal
    prov.api = LastFMAPIClient(prov)
    prov.recommendations_manager = MagicMock()
    return prov


def mock_http(client: LastFMAPIClient, data: dict[str, Any]) -> MagicMock:
    """Return an HTTP response stub without making a network request."""
    response = MagicMock()
    response.json = AsyncMock(return_value=data)
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)
    client.http_session = MagicMock()
    client.http_session.get.return_value = response
    return response


async def test_raw_response_is_shared_and_survives_restart(
    provider: LastFMRecommendationsProvider,
) -> None:
    """Concurrent callers and a recreated client reuse the same persisted response."""
    response = mock_http(provider.api, {"similarartists": {"artist": [{"name": "B"}]}})
    first, second = await asyncio.gather(
        provider.api.get_similar_artists("A"), provider.api.get_similar_artists("A")
    )
    assert first == second == [{"name": "B"}]
    response.json.assert_awaited_once()
    await provider.mass.cache.close()
    await provider.mass.cache._setup_database()
    restarted = LastFMAPIClient(provider)
    unused = mock_http(restarted, {})
    assert await restarted.get_similar_artists("A") == [{"name": "B"}]
    unused.json.assert_not_awaited()


async def test_stale_api_data_is_immediate_and_refreshes(
    provider: LastFMRecommendationsProvider,
) -> None:
    """An expired raw entry is served while one refresh updates SQLite."""
    key = "_get_data.artist.getSimilar.artistA.autocorrect1.limit10"
    await provider.mass.cache.set(
        key,
        {"similarartists": {"artist": [{"name": "old"}]}},
        provider=provider.instance_id,
        category=CACHE_CATEGORY_API,
        expiration=-1,
        allow_expired_cache=True,
    )
    started = asyncio.Event()
    finish = asyncio.Event()
    response = mock_http(provider.api, {})

    async def fetch() -> dict[str, Any]:
        started.set()
        await finish.wait()
        return {"similarartists": {"artist": [{"name": "new"}]}}

    response.json.side_effect = fetch
    assert await provider.api.get_similar_artists("A") == [{"name": "old"}]
    await asyncio.wait_for(started.wait(), 1)
    assert await provider.api.get_similar_artists("A") == [{"name": "old"}]
    finish.set()
    for _ in range(50):
        if await provider.api.get_similar_artists("A") == [{"name": "new"}]:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("Background refresh did not update the cache")
    response.json.assert_awaited_once()


async def test_api_errors_are_not_cached(provider: LastFMRecommendationsProvider) -> None:
    """A corrected credential can retry immediately after Last.fm rejects a call."""
    response = mock_http(provider.api, {"error": 10, "message": "invalid key"})
    with pytest.raises(InvalidToken):
        await provider.api.get_similar_artists("A")
    response.json.return_value = {"similarartists": {"artist": [{"name": "B"}]}}
    assert await provider.api.get_similar_artists("A") == [{"name": "B"}]
    assert response.json.await_count == 2


async def test_similar_artist_resolution_is_persisted(
    provider: LastFMRecommendationsProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeat artist-page visits do not rerun cross-provider resolution."""
    artist = Artist(item_id="1", provider="library", name="A", provider_mappings=set())
    similar = Artist(item_id="2", provider="spotify", name="B", provider_mappings=set())
    fetch = AsyncMock(return_value=[{"name": "B"}])
    resolve = AsyncMock(return_value=similar)
    monkeypatch.setattr(provider.api, "get_similar_artists", fetch)
    monkeypatch.setattr(provider.recommendations_manager, "get_or_resolve_artist", resolve)
    first, second = await asyncio.gather(
        provider.get_similar_artists(artist), provider.get_similar_artists(artist)
    )
    assert first[0].name == second[0].name == "B"
    fetch.assert_awaited_once()
    resolve.assert_awaited_once()
    first[0].name = "Changed"
    assert (await provider.get_similar_artists(artist))[0].name == "B"
