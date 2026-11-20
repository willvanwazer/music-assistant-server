"""
Implementation of a Stream for the Universal Group Player.

Stream handler for Universal Groups, managing audio distribution to group members.
Essentially, it multicasts an audio source to multiple client streams, allowing individual
filter_params for each client.

The members share no clock, so the stream keeps them together by position instead:
it runs on one timeline that follows the wall clock, and every member is handed the
audio that belongs to the present, whenever it connects.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncGenerator, Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from time import monotonic, time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from music_assistant_models.media_items import AudioFormat

    from music_assistant.helpers.dsp import ComplexFilter

from music_assistant.constants import MASS_LOGGER_NAME
from music_assistant.controllers.streams.constants import output_pacing_args
from music_assistant.helpers.ffmpeg import get_ffmpeg_stream

from .constants import (
    BACKLOG_SECONDS,
    DEFAULT_START_CUSHION,
    RESYNC_THRESHOLD,
    START_CUSHION_GRACE,
    START_GATE_TIMEOUT,
    START_GATE_UNKNOWN_MEMBERS,
)

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.providers.ugp_stream")


@dataclass(eq=False)
class MemberFeed:
    """The audio one connected member reads from the group stream."""

    player_id: str | None
    # how much later than the stream this member plays, in bytes of audio
    delay_bytes: int = 0
    # stream position of the feed's first byte; before the start of the stream for a
    # delayed member, which gets silence up to there
    start: int = 0
    # stream position the member has read up to
    position: int = 0
    # audio left out of the feed to bring a member that fell behind back to the present
    skipped: int = 0
    # wall-clock time the feed started
    joined_at: float | None = None
    _wakeup: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    async def wait(self) -> None:
        """Wait until the stream has something new for this feed."""
        self._wakeup.clear()
        await self._wakeup.wait()

    def wake(self) -> None:
        """Let the feed look at the stream again."""
        self._wakeup.set()


class UGPStream:
    """
    Implementation of a Stream for the Universal Group Player.

    Stream handler for Universal Groups, managing audio distribution to group members.
    Essentially, it multicasts an audio source to multiple client streams, allowing individual
    filter_params for each client.
    """

    def __init__(
        self,
        audio_source: AsyncGenerator[bytes],
        audio_format: AudioFormat,
        base_pcm_format: AudioFormat,
        queue_id: str | None,
        session_id: str | None,
        start_cushion: float = DEFAULT_START_CUSHION,
        member_delays: Mapping[str, float] | None = None,
        name: str = "",
        member_name: Callable[[str], str] | None = None,
    ) -> None:
        """
        Initialize UGP Stream.

        :param audio_source: The audio to distribute, in ``audio_format``.
        :param audio_format: The format of the audio source.
        :param base_pcm_format: The PCM format the members' streams are derived from.
        :param queue_id: The queue that is playing on the group.
        :param session_id: The playback session the stream belongs to.
        :param start_cushion: Seconds of audio to hold before the stream starts.
        :param member_delays: Seconds each member plays later than the stream, by player id.
        :param name: Name of the group, for logging.
        :param member_name: Returns the name of a member, for logging.
        """
        self.audio_source = audio_source
        self.input_format = audio_format
        self.base_pcm_format = base_pcm_format
        self.queue_id = queue_id
        self.session_id = session_id
        self.name = name
        self.subscribers: list[MemberFeed] = []
        # the feed each member last opened
        self.feeds: dict[str, MemberFeed] = {}
        self._member_name = member_name or str
        self._member_delays = dict(member_delays or {})
        self._frame_size = (base_pcm_format.bit_depth // 8) * base_pcm_format.channels
        self._bytes_per_second = self._frame_size * base_pcm_format.sample_rate
        self._chunk_size = self._frame_size * (base_pcm_format.sample_rate // 10)
        self._cushion_bytes = int(start_cushion * self._bytes_per_second)
        self._cushion_timeout = start_cushion + START_CUSHION_GRACE
        self._task: asyncio.Task[None] | None = None
        self._done: asyncio.Event = asyncio.Event()
        self._stopped = False
        self._eof = False
        # the retained audio, in chunks of one size (but for the very last one), so a
        # stream position maps straight onto a chunk
        self._backlog: deque[bytes] = deque()
        self._first_chunk = 0
        self._end = 0
        # the timeline: the stream position _origin is due at the moment _started_at
        self._started_at: float | None = None
        self._origin = 0
        self._opened_at: float | None = None
        self._first_audio_at: float | None = None
        self._expected: set[str] = set()
        self._seen: set[str] = set()
        self._timers: list[asyncio.TimerHandle] = []

    @property
    def done(self) -> bool:
        """Return if this stream is already done."""
        if self._stopped:
            return True
        if not self._eof or self._started_at is None:
            return False
        # live for as long as its audio is still due to play
        return self._due_position(0) >= self._end

    @property
    def started(self) -> bool:
        """Return whether the stream has started handing out audio."""
        return self._started_at is not None

    def expect_members(self, player_ids: Iterable[str]) -> None:
        """
        Hold the start of the stream until these members have connected.

        :param player_ids: The members the stream was sent to.
        """
        self._expected.update(player_ids)

    def release_member(self, player_id: str) -> None:
        """
        Stop holding the start of the stream for a member.

        :param player_id: The member that will not connect.
        """
        self._expected.discard(player_id)
        self._try_start()

    def seconds_due(self, feed: MemberFeed) -> float:
        """
        Return how many seconds of a feed a member playing in time has played by now.

        :param feed: The feed of the member.
        """
        if self._started_at is None:
            return 0.0
        due = min(self._due_position(feed.delay_bytes), self._end) - feed.start
        return max(due, 0) / self._bytes_per_second

    def seconds_skipped(self, feed: MemberFeed) -> float:
        """
        Return how many seconds of audio were left out of a feed.

        :param feed: The feed of the member.
        """
        return feed.skipped / self._bytes_per_second

    async def stop(self) -> None:
        """Stop/cancel the stream."""
        if self._stopped:
            return
        self._stopped = True
        for timer in self._timers:
            timer.cancel()
        self._timers.clear()
        if self._task and not self._task.done():
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        self._done.set()
        self._wake_feeds()

    async def subscribe_raw(self, player_id: str | None = None) -> AsyncGenerator[bytes]:
        """
        Subscribe to the raw/unaltered audio stream.

        The returned stream has the format `self.base_pcm_format`.

        :param player_id: The member the audio is for.
        """
        # start the runner as soon as the (first) client connects
        if not self._task:
            self._open()
        delay = self._member_delays.get(player_id, 0.0) if player_id else 0.0
        delay_bytes = int(delay * self._bytes_per_second)
        feed = MemberFeed(player_id, delay_bytes - delay_bytes % self._frame_size)
        self.subscribers.append(feed)
        if player_id:
            self._seen.add(player_id)
        self._try_start()
        # a member that is there for the start gets the stream from its very beginning
        from_start = self._started_at is None
        try:
            while self._started_at is None and not self._stopped:
                await feed.wait()
            if self._stopped:
                return
            silence = self._place(feed, from_start)
            while silence > 0 and not self._stopped:
                padding = min(silence, self._chunk_size)
                silence -= padding
                yield bytes(padding)
            while not self._stopped:
                self._resync(feed)
                if feed.position < self._end:
                    index, offset = divmod(feed.position, self._chunk_size)
                    chunk = self._backlog[index - self._first_chunk]
                    if offset:
                        chunk = chunk[offset:]
                    feed.position += len(chunk)
                    yield chunk
                    continue
                if self._eof:
                    break
                await feed.wait()
        finally:
            self.subscribers.remove(feed)

    async def get_stream(
        self,
        output_format: AudioFormat,
        filter_params: Sequence[str | ComplexFilter] | None = None,
        player_id: str | None = None,
    ) -> AsyncGenerator[bytes]:
        """
        Subscribe to the client specific audio stream.

        :param output_format: The format to deliver the audio in.
        :param filter_params: The ffmpeg filters to apply for this client.
        :param player_id: The member the audio is for.
        """
        async for chunk in get_ffmpeg_stream(
            audio_input=self.subscribe_raw(player_id),
            input_format=self.base_pcm_format,
            output_format=output_format,
            filter_params=filter_params,
        ):
            yield chunk

    def _open(self) -> None:
        """Start reading the audio source and the wait for the members."""
        self._opened_at = monotonic()
        self._task = asyncio.create_task(self._runner())
        loop = asyncio.get_running_loop()
        for gate in (START_GATE_UNKNOWN_MEMBERS, START_GATE_TIMEOUT):
            self._timers.append(loop.call_later(gate, self._try_start))

    def _try_start(self) -> None:
        """Start handing out audio once the members and the audio are there."""
        if self._started_at is not None or self._opened_at is None or self._stopped:
            return
        now = monotonic()
        waited = now - self._opened_at
        if self._expected:
            members_ready = self._expected <= self._seen or waited >= START_GATE_TIMEOUT
        else:
            members_ready = waited >= START_GATE_UNKNOWN_MEMBERS
        if not members_ready:
            return
        if not self._eof:
            if self._first_audio_at is None:
                return
            cushion_ready = (
                self._end >= self._cushion_bytes
                or now - self._first_audio_at >= self._cushion_timeout
            )
            if not cushion_ready:
                return
        self._started_at = now
        self._origin = self._first_chunk * self._chunk_size
        LOGGER.info(
            "%s: group stream started %.2fs after the first member connected, "
            "holding %.1fs of audio",
            self.name,
            waited,
            (self._end - self._origin) / self._bytes_per_second,
        )
        self._wake_feeds()

    def _due_position(self, delay_bytes: int, at_start: bool = False) -> int:
        """
        Return the stream position that is due to play right now.

        :param delay_bytes: How much later than the stream the member plays.
        :param at_start: Return what was due the moment the stream started instead.
        """
        assert self._started_at is not None
        elapsed = 0.0 if at_start else monotonic() - self._started_at
        position = self._origin + round(elapsed * self._bytes_per_second) - delay_bytes
        return position - position % self._frame_size

    def _place(self, feed: MemberFeed, from_start: bool) -> int:
        """
        Point a feed at the audio that is due and return the silence to send before it.

        :param feed: The feed that starts.
        :param from_start: Whether the member was connected when the stream started.
        """
        due = self._due_position(feed.delay_bytes, at_start=from_start)
        backlog_start = self._first_chunk * self._chunk_size
        feed.position = min(max(due, backlog_start), self._end)
        feed.start = min(due, feed.position)
        feed.joined_at = time()
        if feed.player_id:
            self.feeds[feed.player_id] = feed
            assert self._started_at is not None
            LOGGER.info(
                "%s: %s joined the group stream %.2fs after it started, at %.2fs%s",
                self.name,
                self._member_name(feed.player_id),
                monotonic() - self._started_at,
                (feed.start - self._origin) / self._bytes_per_second,
                f" (delayed {feed.delay_bytes / self._bytes_per_second * 1000:.0f}ms)"
                if feed.delay_bytes
                else "",
            )
        return feed.position - feed.start

    def _resync(self, feed: MemberFeed) -> None:
        """
        Move a feed that fell too far behind up to the audio that is due now.

        :param feed: The feed to check.
        """
        backlog_start = self._first_chunk * self._chunk_size
        due = min(self._due_position(feed.delay_bytes), self._end)
        behind = due - feed.position
        if behind <= RESYNC_THRESHOLD * self._bytes_per_second and feed.position >= backlog_start:
            return
        position = max(due, backlog_start, feed.position)
        if position == feed.position:
            return
        LOGGER.warning(
            "%s: %s fell %.1fs behind the group stream, skipping ahead",
            self.name,
            self._member_name(feed.player_id) if feed.player_id else "a member",
            behind / self._bytes_per_second,
        )
        feed.skipped += position - feed.position
        feed.position = position

    def _append(self, chunk: bytes) -> None:
        """
        Add audio to the stream.

        :param chunk: The audio, a full chunk unless it is the last of the stream.
        """
        if self._started_at is not None:
            # a source that falls behind playback delays everyone alike: the members ran
            # dry and pick up with this chunk, so the timeline never runs ahead of the
            # audio that exists
            ahead = self._due_position(0) - self._end
            if ahead > 0:
                self._started_at += ahead / self._bytes_per_second
        self._backlog.append(chunk)
        self._end += len(chunk)
        if self._first_audio_at is None:
            self._first_audio_at = monotonic()
            self._timers.append(
                asyncio.get_running_loop().call_later(self._cushion_timeout, self._try_start)
            )
        if self._started_at is None:
            self._try_start()
            return
        max_chunks = int(BACKLOG_SECONDS * self._bytes_per_second / self._chunk_size)
        while len(self._backlog) > max_chunks:
            self._backlog.popleft()
            self._first_chunk += 1
        self._wake_feeds()

    def _wake_feeds(self) -> None:
        """Let every connected member look at the stream again."""
        for feed in self.subscribers:
            feed.wake()

    async def _runner(self) -> None:
        """Run the stream for the given audio source."""
        pending = bytearray()
        try:
            async for chunk in get_ffmpeg_stream(
                audio_input=self.audio_source,
                input_format=self.input_format,
                output_format=self.base_pcm_format,
                chunk_size=self._chunk_size,
                # we don't allow the player to buffer too much ahead so we use readrate limiting
                extra_input_args=output_pacing_args(),
            ):
                if not pending and len(chunk) == self._chunk_size:
                    self._append(chunk)
                    continue
                pending += chunk
                while len(pending) >= self._chunk_size:
                    self._append(bytes(pending[: self._chunk_size]))
                    del pending[: self._chunk_size]
        except asyncio.CancelledError:
            LOGGER.debug("UGP stream runner cancelled")
            raise
        except Exception as err:
            LOGGER.error("UGP stream runner error: %s", err, exc_info=err)
        finally:
            if pending:
                self._append(bytes(pending))
            self._eof = True
            self._try_start()
            self._wake_feeds()
            self._done.set()
