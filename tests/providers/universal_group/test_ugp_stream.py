"""Tests for the pacing of the Universal Group Player stream."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any, Self
from unittest.mock import patch

import pytest
from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat

from music_assistant.controllers.streams.constants import output_pacing_args
from music_assistant.providers.universal_group.constants import (
    BACKLOG_SECONDS,
    RESYNC_THRESHOLD,
    START_CUSHION_GRACE,
    START_GATE_TIMEOUT,
    START_GATE_UNKNOWN_MEMBERS,
)
from music_assistant.providers.universal_group.ugp_stream import UGPStream


async def test_ugp_stream_is_paced_like_the_flow_stream() -> None:
    """
    The group stream is one continuous stream fanned out to all members.

    Its source is the raw PCM queue flow, which carries no pacing of its own, so this
    ffmpeg is the single pacing point and takes the flow route's realtime pace.
    """
    recorded: dict[str, Any] = {}
    pcm_format = AudioFormat(
        content_type=ContentType.PCM_F32LE,
        sample_rate=44100,
        bit_depth=32,
        channels=2,
    )

    async def fake_ffmpeg_stream(**kwargs: Any) -> AsyncGenerator[bytes]:
        recorded.update(kwargs)
        yield b"\x00" * 16

    async def audio_source() -> AsyncGenerator[bytes]:
        yield b"\x00" * 16

    stream = UGPStream(
        audio_source=audio_source(),
        audio_format=pcm_format,
        base_pcm_format=pcm_format,
        queue_id="q",
        session_id="s",
    )
    with patch(
        "music_assistant.providers.universal_group.ugp_stream.get_ffmpeg_stream",
        fake_ffmpeg_stream,
    ):
        chunks = [chunk async for chunk in stream.subscribe_raw()]

    assert chunks == [b"\x00" * 16]
    assert recorded["extra_input_args"] == output_pacing_args()


async def test_stream_waits_for_the_members_it_was_sent_to() -> None:
    """Nothing is handed out until every expected member has connected."""
    with _Harness() as harness:
        harness.stream.expect_members(["a", "b"])
        first = harness.connect("a")
        harness.feed(30)
        await _settle()

        assert not harness.stream.started
        assert first.chunks == []

        second = harness.connect("b")
        await _settle()

        assert first.indexes == list(range(30))
        assert second.indexes == list(range(30))


async def test_start_does_not_wait_for_a_member_that_will_not_connect() -> None:
    """Releasing a member that failed to start lets the others begin at once."""
    with _Harness() as harness:
        harness.stream.expect_members(["a", "b"])
        first = harness.connect("a")
        harness.feed(5)
        await _settle()
        assert not harness.stream.started

        harness.stream.release_member("b")
        await _settle()

        assert first.indexes == list(range(5))


async def test_start_stops_waiting_for_members_after_the_gate_timeout() -> None:
    """A member that never connects delays the start by the gate timeout at most."""
    with _Harness() as harness:
        harness.stream.expect_members(["a", "b"])
        first = harness.connect("a")
        harness.feed(5)
        await _settle()
        assert not harness.stream.started

        harness.clock.now += START_GATE_TIMEOUT
        harness.feed(1)
        await _settle()

        assert first.indexes == list(range(6))


async def test_start_holds_until_the_cushion_is_filled() -> None:
    """A source that delivers just in time is held until the cushion is there."""
    with _Harness(start_cushion=2) as harness:
        member = harness.connect("a")
        await _settle()
        harness.clock.now += START_GATE_UNKNOWN_MEMBERS
        harness.feed(19)
        await _settle()
        assert not harness.stream.started

        harness.feed(1)
        await _settle()

        assert member.indexes == list(range(20))


async def test_start_does_not_wait_forever_for_the_cushion() -> None:
    """A source too slow to fill the cushion starts with what it has."""
    with _Harness(start_cushion=2) as harness:
        member = harness.connect("a")
        harness.feed(5)
        await _settle()
        assert not harness.stream.started

        harness.clock.now += 2 + START_CUSHION_GRACE
        harness.feed(1)
        await _settle()

        assert member.indexes == list(range(6))


async def test_late_member_starts_where_the_others_are_playing() -> None:
    """A member that connects later gets the audio that is due now, not the live edge."""
    with _Harness() as harness:
        harness.stream.expect_members(["a"])
        first = harness.connect("a")
        # the source runs 10 seconds ahead of playback, as it does after its opening burst
        harness.feed(100)
        await _settle()
        assert first.indexes == list(range(100))

        harness.clock.now += 3
        late = harness.connect("b")
        await _settle()

        assert late.indexes == list(range(30, 100))


async def test_late_member_is_placed_within_a_chunk() -> None:
    """The placement is exact, not rounded to the stream's chunks."""
    with _Harness() as harness:
        harness.stream.expect_members(["a"])
        harness.connect("a")
        harness.feed(100)
        await _settle()

        harness.clock.now += 3.05
        late = harness.connect("b")
        await _settle()

        assert late.indexes[0] == 30
        assert len(late.chunks[0]) == _CHUNK // 2


async def test_delayed_member_gets_silence_first() -> None:
    """A member with a playback delay plays the same audio that much later."""
    with _Harness(member_delays={"b": 0.5}) as harness:
        harness.stream.expect_members(["a", "b"])
        first = harness.connect("a")
        delayed = harness.connect("b")
        harness.feed(10)
        await _settle()

        assert first.indexes == list(range(10))
        silence = b"".join(delayed.chunks[:5])
        assert silence == bytes(5 * _CHUNK)
        assert delayed.indexes[5:] == list(range(10))


async def test_delayed_member_that_connects_late_keeps_its_delay() -> None:
    """The delay of a late member is taken off the position it is placed at."""
    with _Harness(member_delays={"b": 0.5}) as harness:
        harness.stream.expect_members(["a"])
        harness.connect("a")
        harness.feed(100)
        await _settle()

        harness.clock.now += 3
        delayed = harness.connect("b")
        await _settle()

        assert delayed.indexes == list(range(25, 100))


async def test_member_that_stops_reading_does_not_hold_back_the_others() -> None:
    """A stalled member no longer starves the rest of the group."""
    with _Harness() as harness:
        harness.stream.expect_members(["a", "b"])
        stalled = harness.stream.subscribe_raw("a")
        stalled_first = asyncio.ensure_future(anext(stalled))
        reader = harness.connect("b")
        harness.feed(100)
        await _settle()

        assert _index(await stalled_first) == 0
        assert reader.indexes == list(range(100))
        await stalled.aclose()


async def test_member_that_fell_far_behind_skips_ahead() -> None:
    """A member that could not have played in time is moved up to the present."""
    with _Harness() as harness:
        harness.stream.expect_members(["a"])
        feed = harness.stream.subscribe_raw("a")
        pending = asyncio.ensure_future(anext(feed))
        harness.feed(100)
        await _settle()
        assert _index(await pending) == 0

        harness.clock.now += RESYNC_THRESHOLD + 2
        chunk = await anext(feed)

        assert _index(chunk) == 50
        member_feed = harness.stream.feeds["a"]
        # chunks 1 up to 49 were left out
        assert harness.stream.seconds_skipped(member_feed) == pytest.approx(4.9)
        # what it should have played by now is still counted from where it started
        assert harness.stream.seconds_due(member_feed) == pytest.approx(5)
        await feed.aclose()


async def test_member_that_is_a_little_behind_is_left_alone() -> None:
    """A short stall does not cost a member any audio."""
    with _Harness() as harness:
        harness.stream.expect_members(["a"])
        feed = harness.stream.subscribe_raw("a")
        pending = asyncio.ensure_future(anext(feed))
        harness.feed(100)
        await _settle()
        await pending

        harness.clock.now += RESYNC_THRESHOLD - 1
        chunk = await anext(feed)

        assert _index(chunk) == 1
        assert harness.stream.feeds["a"].skipped == 0
        await feed.aclose()


async def test_source_that_falls_behind_delays_everyone_alike() -> None:
    """When the source stalls, the timeline waits for it instead of skipping audio."""
    with _Harness() as harness:
        harness.stream.expect_members(["a"])
        member = harness.connect("a")
        harness.feed(10)
        await _settle()

        # 5 seconds pass with only 1 second of audio to play
        harness.clock.now += 5
        harness.feed(10)
        await _settle()

        assert member.indexes == list(range(20))
        assert harness.stream.feeds["a"].skipped == 0
        late = harness.connect("b")
        await _settle()
        # the member that joins now is placed on the audio that just became due
        assert late.indexes == list(range(10, 20))


async def test_stream_keeps_a_bounded_backlog() -> None:
    """The audio kept for placing members does not grow with the session."""
    with _Harness() as harness:
        harness.stream.expect_members(["a"])
        member = harness.connect("a")
        harness.feed(400)
        await _settle()

        assert len(member.chunks) == 400
        assert len(harness.stream._backlog) == int(BACKLOG_SECONDS * 10)


async def test_stream_is_done_once_its_audio_has_played_out() -> None:
    """The stream stays live after the source ended, for the audio still due to play."""
    with _Harness() as harness:
        harness.stream.expect_members(["a"])
        member = harness.connect("a")
        harness.feed(100)
        harness.end()
        await _settle()

        assert member.indexes == list(range(100))
        assert member.task.done()
        assert not harness.stream.done

        harness.clock.now += 4
        late = harness.connect("b")
        await _settle()
        assert late.indexes == list(range(40, 100))

        harness.clock.now += 6
        assert harness.stream.done


async def test_stop_ends_every_member_feed() -> None:
    """Stopping the stream releases members that are waiting for audio."""
    with _Harness() as harness:
        harness.stream.expect_members(["a"])
        member = harness.connect("a")
        harness.feed(3)
        await _settle()

        await harness.stream.stop()
        await _settle()

        assert member.task.done()
        assert harness.stream.done
        assert harness.stream.subscribers == []


_TEST_FORMAT = AudioFormat(
    content_type=ContentType.PCM_S16LE,
    sample_rate=1000,
    bit_depth=16,
    channels=1,
)
# a tenth of a second of audio in the test format, the size the stream chunks at
_CHUNK = 200


class _Clock:
    """A clock the test moves by hand."""

    def __init__(self) -> None:
        """Initialize the clock."""
        self.now = 1000.0

    def __call__(self) -> float:
        """Return the current time."""
        return self.now


class _Member:
    """A member that reads its feed as fast as it can."""

    def __init__(self, stream: UGPStream, player_id: str) -> None:
        """Start reading the feed of the given member."""
        self.chunks: list[bytes] = []
        self.task = asyncio.ensure_future(self._read(stream, player_id))

    @property
    def indexes(self) -> list[int]:
        """Return the index of the source chunk each received chunk came from."""
        return [_index(chunk) for chunk in self.chunks]

    async def _read(self, stream: UGPStream, player_id: str) -> None:
        async for chunk in stream.subscribe_raw(player_id):
            self.chunks.append(chunk)


class _Harness:
    """A group stream on a hand-moved clock, fed by the test instead of ffmpeg."""

    def __init__(
        self, start_cushion: float = 0, member_delays: dict[str, float] | None = None
    ) -> None:
        """Create the stream."""
        self.clock = _Clock()
        self._source: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._fed = 0
        self._members: list[_Member] = []
        self._patches: list[Any] = [
            patch(
                "music_assistant.providers.universal_group.ugp_stream.get_ffmpeg_stream",
                self._ffmpeg_stream,
            ),
            patch("music_assistant.providers.universal_group.ugp_stream.monotonic", self.clock),
        ]

        async def audio_source() -> AsyncGenerator[bytes]:
            yield b""

        self.stream = UGPStream(
            audio_source=audio_source(),
            audio_format=_TEST_FORMAT,
            base_pcm_format=_TEST_FORMAT,
            queue_id="q",
            session_id="s",
            start_cushion=start_cushion,
            member_delays=member_delays,
        )

    def __enter__(self) -> Self:
        """Take the place of ffmpeg and the clock."""
        for item in self._patches:
            item.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        """Stop the members and restore ffmpeg and the clock."""
        for member in self._members:
            member.task.cancel()
        for timer in self.stream._timers:
            timer.cancel()
        if self.stream._task:
            self.stream._task.cancel()
        for item in self._patches:
            item.stop()

    def connect(self, player_id: str) -> _Member:
        """
        Connect a member that reads everything it is given.

        :param player_id: The member that connects.
        """
        member = _Member(self.stream, player_id)
        self._members.append(member)
        return member

    def feed(self, chunks: int) -> None:
        """
        Let the source deliver more audio.

        Every chunk is filled with its index plus one, which tells it apart from silence.

        :param chunks: The number of chunks, each a tenth of a second.
        """
        for _ in range(chunks):
            self._fed += 1
            self._source.put_nowait(bytes([(self._fed - 1) % 255 + 1]) * _CHUNK)

    def end(self) -> None:
        """End the source."""
        self._source.put_nowait(None)

    async def _ffmpeg_stream(self, **_kwargs: Any) -> AsyncGenerator[bytes]:
        while (chunk := await self._source.get()) is not None:
            yield chunk


def _index(chunk: bytes) -> int:
    """
    Return the index of the source chunk a received chunk came from.

    :param chunk: A chunk as fed by the harness, or part of one.
    """
    return chunk[0] - 1


async def _settle() -> None:
    """Let the stream and the members run until they wait for something new."""
    for _ in range(50):
        await asyncio.sleep(0)
