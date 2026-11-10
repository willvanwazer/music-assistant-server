"""Write Music Assistant listening progress back to Overcast on the Mac."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from music_assistant_models.errors import LoginFailed, MediaNotFoundError

from .client import OvercastCtlError
from .constants import (
    FULLY_PLAYED_FRACTION,
    FULLY_PLAYED_REMAINING_S,
    SYNC_DELAY_S,
    SYNC_MAX_ATTEMPTS,
    SYNC_RETRY_S,
    SYNC_STALE_TICK_S,
    WRITE_TIMEOUT_S,
)

if TYPE_CHECKING:
    import logging

    from .client import OvercastCtlClient

# overcastctl codes worth retrying later: Overcast is playing on the Mac (or its state
# cannot be read), another write is running, or the app was reopened mid-write
_RETRY_CODES = {"playing", "running", "busy", "conflict"}


@dataclass
class _Session:
    started: int  # epoch seconds of the first report, the not_after guard
    position: int = 0
    played: bool = False
    task: asyncio.Task[None] | None = None


class ProgressSync:
    """
    Collect on_played reports and write each listening session to Overcast once.

    Every overcastctl write closes and reopens the Overcast app, so nothing is
    written while an episode plays: the position is written shortly after it stops
    or pauses (or, if no stop is ever reported, a while after the last tick).
    A session is skipped when Overcast changed the episode after it started,
    e.g. it was played on a phone meanwhile.
    """

    def __init__(self, client: OvercastCtlClient, logger: logging.Logger) -> None:
        """
        Initialize the syncer.

        :param client: The overcastctl client.
        :param logger: Provider logger.
        """
        self._client = client
        self._logger = logger
        self._sessions: dict[str, _Session] = {}

    def report(
        self, episode_id: str, position: int, fully_played: bool, duration: int, is_playing: bool
    ) -> None:
        """
        Record one on_played report and (re)schedule the write.

        :param episode_id: Overcast episode id.
        :param position: Position in seconds.
        :param fully_played: MA's fully played flag (also set on skip/stop).
        :param duration: Episode duration in seconds, 0 if unknown.
        :param is_playing: Whether the episode is still playing.
        """
        session = self._sessions.get(episode_id)
        if session is None:
            session = _Session(started=int(time.time()) - 5)
            self._sessions[episode_id] = session
        session.position = max(position, 0)
        session.played = _really_finished(fully_played, position, duration)
        if session.task is not None:
            session.task.cancel()
        delay = SYNC_STALE_TICK_S if is_playing else SYNC_DELAY_S
        session.task = asyncio.create_task(self._write_later(episode_id, session, delay))

    async def close(self) -> None:
        """Cancel pending writes (provider unload)."""
        for session in self._sessions.values():
            if session.task is not None:
                session.task.cancel()
        self._sessions.clear()

    async def _write_later(self, episode_id: str, session: _Session, delay: float) -> None:
        await asyncio.sleep(delay)
        for attempt in range(SYNC_MAX_ATTEMPTS):
            try:
                result = await self._client.call(
                    "set_progress",
                    timeout=WRITE_TIMEOUT_S,
                    episode_id=episode_id,
                    position_seconds=None if session.played else session.position,
                    played=session.played,
                    not_after=session.started,
                )
            except OvercastCtlError as err:
                if err.code not in _RETRY_CODES:
                    self._logger.warning("Could not save Overcast progress: %s", err)
                    break
                self._logger.debug("Overcast progress write deferred (%s)", err.code)
            except (MediaNotFoundError, LoginFailed) as err:
                self._logger.warning("Could not save Overcast progress: %s", err)
                break
            except Exception as err:
                # unreachable Mac (asleep or offline): keep trying for a while
                self._logger.debug("Overcast progress write failed, will retry: %s", err)
            else:
                if result.get("skipped"):
                    self._logger.info(
                        "Overcast progress for %s not saved: changed in Overcast since", episode_id
                    )
                else:
                    self._logger.debug(
                        "Saved Overcast progress for %s: %s",
                        episode_id,
                        "played" if session.played else f"{session.position}s",
                    )
                break
            if attempt < SYNC_MAX_ATTEMPTS - 1:
                await asyncio.sleep(SYNC_RETRY_S)
        if self._sessions.get(episode_id) is session:
            del self._sessions[episode_id]


def _really_finished(fully_played: bool, position: int, duration: int) -> bool:
    """MA also reports fully_played on skip/stop, so confirm it against the position."""
    if not fully_played or duration <= 0:
        return False
    return position >= duration * FULLY_PLAYED_FRACTION or (
        duration - position <= FULLY_PLAYED_REMAINING_S
    )
