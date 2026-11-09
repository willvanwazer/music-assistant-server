"""Tests for the Overcast (Mac) provider backed by overcastctl."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.providers.overcast_mac import OvercastMacProvider

PODCASTS = [
    {
        "id": "10",
        "title": "Private Show",
        "author": "Relay",
        "private": True,
        "episodes": 2,
        "artwork_url": "https://r2.overcast-cdn.com/art/10?v1",
    }
]


def _episode(**overrides: Any) -> dict[str, Any]:
    return {
        "id": "501",
        "podcast_id": "10",
        "podcast": "Private Show",
        "title": "501: Title",
        "display_title": "Title",
        "published": "2026-11-06T10:00:46+00:00",
        "duration_seconds": 900.05,
        "progress_seconds": 91,
        "played": False,
        "downloaded": True,
        "private": True,
        "last_played": None,
        **overrides,
    }


@pytest.fixture
def client() -> MagicMock:
    """Return a fake overcastctl client."""
    client = MagicMock()
    client.paged = AsyncMock(return_value=PODCASTS)
    client.call = AsyncMock(return_value=_episode())
    client.audio_info = AsyncMock(return_value=("audio/mpeg", 9_000_000))
    return client


@pytest.fixture
def provider(client: MagicMock) -> OvercastMacProvider:
    """Return a provider wired to the fake client."""
    manifest = MagicMock()
    manifest.domain = "overcast_mac"
    config = MagicMock()
    config.instance_id = "overcast_mac"
    config.get_value.return_value = None
    prov = OvercastMacProvider(AsyncMock(), manifest, config)
    prov._client = client
    prov._podcasts = {p["id"]: p for p in PODCASTS}
    prov._podcasts_fetched = 1e18
    return prov


async def test_up_next_reads_unordered_queue(
    provider: OvercastMacProvider, client: MagicMock
) -> None:
    """Up Next must not fail when Overcast has not placed new episodes yet."""
    client.paged.return_value = [_episode(), _episode(id="502", progress_seconds=0)]
    items = await provider.browse("overcast_mac://up_next")
    client.paged.assert_awaited_with("get_queue", allow_unordered=True)
    assert [i.item_id for i in items] == ["10:501", "10:502"]
    first = items[0]
    assert isinstance(first.podcast, object)
    assert first.resume_position_ms == 91_000
    assert first.metadata.images[0].path.startswith("https://r2.overcast-cdn.com/")


async def test_root_lists_folders_then_podcasts(provider: OvercastMacProvider) -> None:
    """The root offers the folders the Discover shelves browse, then subscriptions."""
    items = await provider.browse("overcast_mac://")
    assert [i.item_id for i in items] == ["up_next", "in_progress", "starred", "10"]


async def test_downloaded_episode_streams_from_mac(provider: OvercastMacProvider) -> None:
    """Downloaded audio is a custom stream so the token never reaches ffmpeg."""
    details = await provider.get_stream_details("10:501", MediaType.PODCAST_EPISODE)
    assert details.stream_type == StreamType.CUSTOM
    assert details.audio_format.content_type == ContentType.MP3
    assert details.can_seek
    assert details.data == {"episode_id": "501", "size": 9_000_000}


async def test_seek_uses_byte_offset(provider: OvercastMacProvider, client: MagicMock) -> None:
    """Seeking requests a proportional byte offset."""
    seen: list[int] = []

    async def fake_stream(_episode_id: str, offset: int = 0):  # type: ignore[no-untyped-def]
        seen.append(offset)
        yield b"x"

    client.stream_audio = fake_stream
    details = await provider.get_stream_details("10:501", MediaType.PODCAST_EPISODE)
    assert [c async for c in provider.get_audio_stream(details, seek_position=450)] == [b"x"]
    assert seen == [4_500_000]


async def test_undownloaded_private_episode_streams_through_mac(
    provider: OvercastMacProvider, client: MagicMock
) -> None:
    """Private feed episodes play via the Mac's relay; no feed URL reaches MA."""
    client.call.return_value = _episode(downloaded=False)
    client.audio_info.return_value = ("audio/mpeg", None)
    details = await provider.get_stream_details("10:501", MediaType.PODCAST_EPISODE)
    assert details.stream_type == StreamType.CUSTOM
    assert details.path is None
    # without a size the byte offset cannot be estimated, so MA seeks in ffmpeg
    assert not details.can_seek


async def test_unplayable_episode_raises(provider: OvercastMacProvider, client: MagicMock) -> None:
    """An episode with neither a download nor a feed URL is reported missing."""
    client.audio_info.side_effect = MediaNotFoundError("no audio")
    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details("10:501", MediaType.PODCAST_EPISODE)


async def test_podcast_lists_full_local_catalog(
    provider: OvercastMacProvider, client: MagicMock
) -> None:
    """Podcast pages include played episodes, all playable."""
    client.paged.return_value = [_episode(downloaded=False, played=True)]
    episodes = [e async for e in provider.get_podcast_episodes("10")]
    client.paged.assert_awaited_with("list_episodes", podcast="10", state="all")
    assert episodes[0].fully_played
    assert all(m.available for m in episodes[0].provider_mappings)


async def test_resume_position(provider: OvercastMacProvider, client: MagicMock) -> None:
    """Resume comes from Overcast; untouched episodes defer to MA's playlog."""
    assert await provider.get_resume_position("10:501", MediaType.PODCAST_EPISODE) == (
        False,
        91_000,
        None,
    )
    client.call.return_value = _episode(progress_seconds=0)
    with pytest.raises(NotImplementedError):
        await provider.get_resume_position("10:501", MediaType.PODCAST_EPISODE)
