"""Regression tests for Spotify catalog read-through caching."""

import asyncio
from collections.abc import AsyncGenerator
from copy import deepcopy
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from music_assistant.mass import MusicAssistant
from music_assistant.providers.spotify.provider import SpotifyProvider


@pytest.fixture
async def provider(mass_minimal: MusicAssistant) -> SpotifyProvider:
    """Return a provider using the real SQLite cache and task deduplication."""
    await mass_minimal.cache._setup_database()
    prov = object.__new__(SpotifyProvider)
    prov.config = MagicMock(instance_id="spotify--catalog-test")
    prov.manifest = MagicMock(domain="spotify")
    prov.logger = MagicMock()
    prov.mass = mass_minimal
    return prov


@pytest.fixture
def album_data() -> dict[str, Any]:
    """Return an album with a complete embedded track listing."""
    return {
        "id": "album1",
        "name": "Album",
        "album_type": "album",
        "artists": [],
        "external_urls": {"spotify": "https://open.spotify.com/album/album1"},
        "images": [{"url": "https://i.scdn.co/image/album1", "height": 300, "width": 300}],
        "tracks": {
            "total": 1,
            "offset": 0,
            "next": None,
            "items": [
                {
                    "id": "track1",
                    "name": "Track",
                    "duration_ms": 120000,
                    "external_urls": {"spotify": "https://open.spotify.com/track/track1"},
                    "is_local": False,
                    "is_playable": True,
                    "explicit": False,
                    "disc_number": 1,
                    "track_number": 1,
                    "artists": [],
                }
            ],
        },
    }


async def test_album_and_tracks_share_one_spotify_request(
    provider: SpotifyProvider, album_data: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent detail and track reads reuse Spotify's embedded listing."""
    fetch = AsyncMock(return_value=album_data)
    monkeypatch.setattr(provider, "_get_data", fetch)
    album, tracks = await asyncio.gather(
        provider.get_album("album1"), provider.get_album_tracks("album1")
    )
    assert album.name == "Album"
    assert [track.item_id for track in tracks] == ["track1"]
    assert tracks[0].album is not None
    assert tracks[0].album.item_id == "album1"
    assert tracks[0].image == album.image
    fetch.assert_awaited_once_with("albums/album1")
    # A later read also comes from the persistent cache, with independent objects.
    tracks[0].name = "changed by caller"
    assert (await provider.get_album_tracks("album1"))[0].name == "Track"
    assert fetch.await_count == 1


@pytest.mark.parametrize("kind", ["partial", "missing_total", "offset", "next", "missing_tracks"])
async def test_incomplete_embedded_tracks_fall_back_to_full_pagination(
    provider: SpotifyProvider,
    album_data: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """A partial, absent or ambiguous embedded page must never truncate an album."""
    if kind == "partial":
        album_data["tracks"]["total"] = 2
    elif kind == "missing_total":
        del album_data["tracks"]["total"]
    elif kind == "offset":
        album_data["tracks"]["offset"] = 1
    elif kind == "next":
        album_data["tracks"]["next"] = "next-page"
    else:
        del album_data["tracks"]
    monkeypatch.setattr(provider, "_get_data", AsyncMock(return_value=album_data))
    pages = MagicMock()

    async def all_items(endpoint: str) -> AsyncGenerator[dict[str, Any]]:
        pages(endpoint)
        yield {
            "id": "track2",
            "name": "Second",
            "duration_ms": 1000,
            "external_urls": {"spotify": "https://open.spotify.com/track/track2"},
            "is_local": False,
            "is_playable": True,
            "explicit": False,
        }

    monkeypatch.setattr(provider, "_get_all_items", all_items)
    assert [track.item_id for track in await provider.get_album_tracks("album1")] == ["track2"]
    pages.assert_called_once_with("albums/album1/tracks")


async def test_expired_album_is_served_while_refreshing(
    provider: SpotifyProvider, album_data: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Expired metadata remains readable while one background Spotify fetch runs."""
    await provider.mass.cache.set(
        "_get_album_data.album1",
        album_data,
        provider=provider.instance_id,
        expiration=-10,
        allow_expired_cache=True,
    )
    gate = asyncio.Event()
    started = asyncio.Event()
    fresh = deepcopy(album_data)
    fresh["name"] = "Updated"

    async def fetch(_endpoint: str) -> dict[str, Any]:
        started.set()
        await gate.wait()
        return fresh

    request = AsyncMock(side_effect=fetch)
    monkeypatch.setattr(provider, "_get_data", request)
    results = await asyncio.wait_for(
        asyncio.gather(provider.get_album("album1"), provider.get_album("album1")), timeout=1
    )
    assert [album.name for album in results] == ["Album", "Album"]
    await asyncio.wait_for(started.wait(), timeout=1)
    assert request.await_count == 1
    gate.set()
    task = provider.mass.create_task(
        asyncio.sleep(0), task_id=f"cache_refresh.{provider.instance_id}._get_album_data.album1"
    )
    await task
    assert (await provider.get_album("album1")).name == "Updated"


async def test_explicit_refresh_bypasses_album_cache(
    provider: SpotifyProvider, album_data: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The normal force-refresh context still requests current Spotify metadata."""
    await provider.mass.cache.set(
        "_get_album_data.album1", album_data, provider=provider.instance_id
    )
    fresh = deepcopy(album_data)
    fresh["name"] = "Updated"
    request = AsyncMock(return_value=fresh)
    monkeypatch.setattr(provider, "_get_data", request)
    async with provider.mass.cache.handle_refresh(True):
        assert (await provider.get_album("album1")).name == "Updated"
    request.assert_awaited_once_with("albums/album1")
