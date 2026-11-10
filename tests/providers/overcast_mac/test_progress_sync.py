"""Tests for writing Music Assistant progress back to Overcast."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_assistant.providers.overcast_mac import progress_sync
from music_assistant.providers.overcast_mac.client import OvercastCtlError
from music_assistant.providers.overcast_mac.progress_sync import ProgressSync


@pytest.fixture(autouse=True)
def fast_timers() -> object:
    """Run the write delays instantly."""
    with (
        patch.object(progress_sync, "SYNC_DELAY_S", 0),
        patch.object(progress_sync, "SYNC_STALE_TICK_S", 0.05),
        patch.object(progress_sync, "SYNC_RETRY_S", 0),
    ):
        yield


@pytest.fixture
def client() -> MagicMock:
    """Return a fake overcastctl client."""
    client = MagicMock()
    client.call = AsyncMock(return_value={"changed": True})
    return client


async def _settle(sync: ProgressSync) -> None:
    for session in list(sync._sessions.values()):
        if session.task is not None:
            await session.task


async def test_stop_writes_position_once(client: MagicMock) -> None:
    """Ticks while playing are coalesced into one write after the stop."""
    sync = ProgressSync(client, logging.getLogger("test"))
    sync.report("501", 30, False, 900, is_playing=True)
    sync.report("501", 60, False, 900, is_playing=True)
    sync.report("501", 75, False, 900, is_playing=False)
    await _settle(sync)
    client.call.assert_awaited_once()
    kwargs = client.call.await_args.kwargs
    assert client.call.await_args.args == ("set_progress",)
    assert (kwargs["episode_id"], kwargs["position_seconds"], kwargs["played"]) == (
        "501",
        75,
        False,
    )
    assert isinstance(kwargs["not_after"], int)
    assert not sync._sessions


async def test_nothing_written_while_playing_until_stale(client: MagicMock) -> None:
    """A playing tick only schedules the long fallback write."""
    with patch.object(progress_sync, "SYNC_STALE_TICK_S", 3600):
        sync = ProgressSync(client, logging.getLogger("test"))
        sync.report("501", 30, False, 900, is_playing=True)
        client.call.assert_not_awaited()
        await sync.close()


async def test_finishing_marks_played_but_skip_does_not(client: MagicMock) -> None:
    """MA's fully_played on a skip mid-episode must not mark it played."""
    sync = ProgressSync(client, logging.getLogger("test"))
    sync.report("501", 880, True, 900, is_playing=False)
    sync.report("502", 100, True, 900, is_playing=False)
    await _settle(sync)
    calls = {c.kwargs["episode_id"]: c.kwargs for c in client.call.await_args_list}
    assert calls["501"]["played"] is True
    assert calls["501"]["position_seconds"] is None
    assert calls["502"]["played"] is False
    assert calls["502"]["position_seconds"] == 100


async def test_retries_while_overcast_plays_on_the_mac(client: MagicMock) -> None:
    """A refusal to interrupt Mac playback is retried later."""
    client.call.side_effect = [OvercastCtlError("playing", "playing"), {"changed": True}]
    sync = ProgressSync(client, logging.getLogger("test"))
    sync.report("501", 75, False, 900, is_playing=False)
    await _settle(sync)
    assert client.call.await_count == 2


async def test_permanent_errors_are_not_retried(client: MagicMock) -> None:
    """An unsupported Overcast version is logged once, not retried."""
    client.call.side_effect = OvercastCtlError("version", "version")
    sync = ProgressSync(client, logging.getLogger("test"))
    sync.report("501", 75, False, 900, is_playing=False)
    await _settle(sync)
    assert client.call.await_count == 1


async def test_newer_overcast_state_is_left_alone(client: MagicMock) -> None:
    """Overcastctl's skip result ends the session without retrying."""
    client.call.return_value = {"changed": False, "skipped": "newer_in_overcast"}
    sync = ProgressSync(client, logging.getLogger("test"))
    sync.report("501", 75, False, 900, is_playing=False)
    await _settle(sync)
    assert client.call.await_count == 1
    assert not sync._sessions
