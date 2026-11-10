"""Unit tests for the Spotify provider's recently played recommendation row."""

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import EventType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import Album, Artist, Playlist, Track

from music_assistant.providers.spotify.parsers import recently_played_sources
from music_assistant.providers.spotify.provider import (
    RECENTLY_PLAYED_ROW_ID,
    SpotifyProvider,
)
from tests.common import use_real_create_task

LIKED = "liked_songs-spotify--test"


def _album(album_id: str) -> dict[str, Any]:
    return {
        "id": album_id,
        "name": f"Album {album_id}",
        "album_type": "album",
        "external_urls": {"spotify": f"https://open.spotify.com/album/{album_id}"},
        "artists": [],
        "images": [],
    }


def _track(track_id: str, album_id: str = "al1") -> dict[str, Any]:
    return {
        "id": track_id,
        "name": f"Track {track_id}",
        "duration_ms": 1000,
        "explicit": False,
        "external_urls": {"spotify": f"https://open.spotify.com/track/{track_id}"},
        "artists": [],
        "album": _album(album_id),
    }


def _play(track_id: str, context: tuple[str, str] | None, album_id: str = "al1") -> dict[str, Any]:
    ctx = {"type": context[0], "uri": f"spotify:{context[0]}:{context[1]}"} if context else None
    return {"track": _track(track_id, album_id), "context": ctx}


def test_sources_group_plays_by_context() -> None:
    """Plays from one album or playlist collapse into one entry, newest first."""
    plays = [
        _play("t1", ("playlist", "p1")),
        _play("t2", ("playlist", "p1")),
        _play("t3", ("album", "al2"), "al2"),
        _play("t4", None),
        _play("t4", None),
        {
            "track": _track("t5"),
            "context": {"type": "collection", "uri": "spotify:user:x:collection"},
        },
        {"track": None, "context": None},
        _play("t6", ("artist", "ar1")),
    ]
    assert [(kind, item_id) for kind, item_id, _ in recently_played_sources(plays, LIKED, 10)] == [
        ("playlist", "p1"),
        ("album", "al2"),
        ("track", "t4"),
        ("playlist", LIKED),
        ("artist", "ar1"),
    ]


def test_sources_respect_limit() -> None:
    """No more than the limit is returned."""
    plays = [_play(f"t{i}", None) for i in range(10)]
    assert len(recently_played_sources(plays, LIKED, 3)) == 3


@pytest.fixture
def provider() -> SpotifyProvider:
    """Return a SpotifyProvider with mocked mass, bypassing __init__."""
    prov = object.__new__(SpotifyProvider)
    prov.config = MagicMock(instance_id="spotify--test")
    prov.manifest = MagicMock(domain="spotify")
    prov.logger = MagicMock()
    mass = MagicMock()
    mass.cache.get = AsyncMock(return_value=None)
    mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    mass.cache.set = AsyncMock()
    use_real_create_task(mass)
    prov.mass = mass
    return prov


@pytest.mark.asyncio
async def test_rows_list_recently_played(provider: SpotifyProvider) -> None:
    """The provider offers one recently played row."""
    rows = await provider.get_recommendations()
    assert [row.item_id for row in rows] == [RECENTLY_PLAYED_ROW_ID]
    assert rows[0].items == []


@pytest.mark.asyncio
async def test_recently_played_items_resolve_sources(
    provider: SpotifyProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contexts resolve to albums, playlists and artists; missing ones are skipped."""
    get_data = AsyncMock(
        return_value={
            "items": [
                _play("t1", ("playlist", "p1")),
                _play("t2", ("album", "al1")),
                _play("t3", ("playlist", "gone")),
                _play("t4", ("artist", "ar1")),
                _play("t5", None),
            ]
        }
    )
    monkeypatch.setattr(provider, "_get_data", get_data)

    async def get_playlist(item_id: str) -> Playlist:
        if item_id == "gone":
            raise MediaNotFoundError(item_id)
        return MagicMock(spec=Playlist, item_id=item_id)

    monkeypatch.setattr(provider, "get_playlist", get_playlist)
    monkeypatch.setattr(
        provider, "get_artist", AsyncMock(return_value=MagicMock(spec=Artist, item_id="ar1"))
    )

    items = await provider.get_recommendation_items(RECENTLY_PLAYED_ROW_ID)

    get_data.assert_awaited_once_with("me/player/recently-played", limit=50)
    cast("MagicMock", provider.mass.signal_event).assert_called_with(
        EventType.PROVIDER_EVENT,
        object_id="spotify--test",
        data={"event": "recommendations_updated", "item_id": RECENTLY_PLAYED_ROW_ID},
    )
    assert [item.item_id for item in items] == ["p1", "al1", "ar1", "t5"]
    assert isinstance(items[1], Album)
    assert isinstance(items[3], Track)


@pytest.mark.asyncio
async def test_recently_played_plays_are_cached(
    provider: SpotifyProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cached listing is served without calling Spotify."""
    cached = [_play("t1", None)]
    monkeypatch.setattr(
        provider.mass.cache,
        "get_with_freshness",
        AsyncMock(return_value=(cached, True, True)),
    )
    get_data = AsyncMock()
    monkeypatch.setattr(provider, "_get_data", get_data)

    items = await provider.get_recommendation_items(RECENTLY_PLAYED_ROW_ID)

    get_data.assert_not_awaited()
    assert [item.item_id for item in items] == ["t1"]


@pytest.mark.asyncio
async def test_unknown_row_is_empty(provider: SpotifyProvider) -> None:
    """Rows this provider does not know return nothing."""
    assert list(await provider.get_recommendation_items("other")) == []
