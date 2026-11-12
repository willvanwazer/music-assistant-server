"""Background filling and refreshing of the cache behind a provider's pages."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Hashable
from typing import TYPE_CHECKING, Any, ClassVar

from music_assistant_models.auth import Scope
from music_assistant_models.enums import TaskStatus
from music_assistant_models.errors import ResourceTemporarilyUnavailable, RetriesExhausted

from .helpers import CacheStatus, cache_status, refresh_cache

if TYPE_CHECKING:
    from music_assistant_models.background_task import BackgroundTask

    from music_assistant.models.provider import Provider

type CacheCall = tuple[Callable[..., Awaitable[Any]], tuple[Any, ...], dict[str, Any]]


class CacheWarmer[TargetT: Hashable](ABC):
    """
    Keep the cache behind a provider's pages filled before anyone opens them.

    A run fills the entries a visit would wait on first, then refreshes the expired
    entries that pages serve stale, so the next visit shows current data at once. Fresh
    entries cost nothing but a cache read. Subclasses list the pages and the cached calls
    each page makes.
    """

    interval: ClassVar[int] = 6 * 3600
    first_run_delay: ClassVar[int] = 5 * 60
    # at most this many pages are fetched per run, so a cold cache fills over several runs
    pages_per_run: ClassVar[int] = 200
    # pause between pages, leaving the provider's shared throttle free for people browsing
    page_pause: ClassVar[float] = 4.0
    # consecutive failed pages after which the run stops until the next interval
    max_consecutive_failures: ClassVar[int] = 5
    # what the background task calls the pages it keeps cached
    subject: ClassVar[str] = "pages"

    def __init__(self, provider: Provider) -> None:
        """
        Initialize the warmer attached to a loaded provider.

        :param provider: The provider whose cached calls are filled.
        """
        self.provider = provider
        self.mass = provider.mass
        self.task_id = f"{provider.domain}_cache_warmer_{provider.instance_id}"
        self.timer_id = f"{self.task_id}_timer"
        self.lock = asyncio.Lock()
        self.unregister = self.mass.register_api_command(
            f"{provider.domain}/{provider.instance_id}/warm_cache",
            self.trigger,
            required_scope=Scope.LIBRARY_MANAGE,
        )

    def start(self) -> None:
        """Schedule the first run after provider startup."""
        self.mass.call_later(self.first_run_delay, self.queue, task_id=self.timer_id)

    async def stop(self) -> None:
        """Stop the timer and drain a running fill before provider unload."""
        self.mass.cancel_timer(self.timer_id)
        self.unregister()
        await self.mass.tasks.unregister_scheduled_task_and_wait(
            self.task_id, clear_persisted_state=False
        )

    async def trigger(self) -> BackgroundTask:
        """Queue a run without overlapping an existing one."""
        return self.queue()

    def queue(self) -> BackgroundTask:
        """Queue work in the visible task manager and schedule the next run."""
        self.mass.call_later(self.interval, self.queue, task_id=self.timer_id)
        return self.mass.tasks.run_background_task(
            task_id=self.task_id,
            name=f"Keep {self.provider.name} {self.subject} cached (every 6 hours)",
            handler=self.run,
            metadata={"provider_instance": self.provider.instance_id},
        )

    async def run(self) -> dict[str, int]:
        """Fill the cache entries that visits would wait on, then refresh expired ones."""
        async with self.lock:
            if not self.provider.available:
                raise RuntimeError(f"{self.provider.name} is unavailable; the cache was not filled")
            counts = {"pages": 0, "filled": 0, "refreshed": 0, "failed": 0, "deferred": 0}
            missing, stale = await self._pages_to_fetch(counts)
            failures = 0
            for outcome, target, calls in [
                *(("filled", *page) for page in missing),
                *(("refreshed", *page) for page in stale),
            ]:
                if (
                    counts["filled"] + counts["refreshed"] + counts["failed"] >= self.pages_per_run
                    or await self._paused()
                ):
                    counts["deferred"] += 1
                    continue
                try:
                    for (method, args, kwargs), status in calls:
                        if status is CacheStatus.MISSING:
                            await method(*args, **kwargs)
                        else:
                            await refresh_cache(method, *args, **kwargs)
                except ResourceTemporarilyUnavailable, RetriesExhausted:
                    # the service asked us to back off: leave the rest for the next run
                    self.provider.logger.info("Cache fill paused by a rate limit")
                    counts["failed"] += 1
                    break
                except Exception as err:
                    self.provider.logger.debug("Could not cache %s: %s", target, err)
                    counts["failed"] += 1
                    failures += 1
                    if failures >= self.max_consecutive_failures:
                        break
                else:
                    counts[outcome] += 1
                    failures = 0
                await asyncio.sleep(self.page_pause)
            self.provider.logger.info("Cache fill complete: %s", counts)
            return counts

    @abstractmethod
    async def _targets(self) -> list[TargetT]:
        """Return the pages to keep cached, most likely to be opened first."""

    @abstractmethod
    async def _page_calls(self, target: TargetT) -> list[CacheCall]:
        """
        Return the cached calls that opening this page makes, as its callers make them.

        :param target: A page returned by _targets.
        """

    async def _paused(self) -> bool:
        """Return whether fetching should wait for the next run, e.g. during a sync."""
        return False

    def _tasks_active(self, prefix: str) -> bool:
        """
        Return whether a background task whose id starts with prefix is pending or running.

        :param prefix: Task id prefix, e.g. music_sync_ for any provider sync.
        """
        active = (TaskStatus.RUNNING, TaskStatus.PENDING)
        return any(
            task.id.startswith(prefix) and task.status in active
            for task in self.mass.tasks.list_tasks()
        )

    async def _pages_to_fetch(
        self, counts: dict[str, int]
    ) -> tuple[
        list[tuple[TargetT, list[tuple[CacheCall, CacheStatus]]]],
        list[tuple[TargetT, list[tuple[CacheCall, CacheStatus]]]],
    ]:
        """
        Return the pages with an entry a visit would wait on, and those with only stale ones.

        Each page comes with its calls that need fetching. A call shared by several pages
        is fetched once, for the first of them.

        :param counts: Run counters; pages is incremented for every page checked.
        """
        missing: list[tuple[TargetT, list[tuple[CacheCall, CacheStatus]]]] = []
        stale: list[tuple[TargetT, list[tuple[CacheCall, CacheStatus]]]] = []
        seen: set[tuple[Any, ...]] = set()
        for target in await self._targets():
            counts["pages"] += 1
            try:
                page_calls = await self._page_calls(target)
            except Exception as err:
                self.provider.logger.debug("Could not look up %s: %s", target, err)
                continue
            calls: list[tuple[CacheCall, CacheStatus]] = []
            for method, args, kwargs in page_calls:
                key = (method, args, tuple(sorted(kwargs.items())))
                if key in seen:
                    continue
                seen.add(key)
                status = await cache_status(method, *args, **kwargs)
                if status is not CacheStatus.FRESH:
                    calls.append(((method, args, kwargs), status))
            if any(status is CacheStatus.MISSING for _call, status in calls):
                missing.append((target, calls))
            elif calls:
                stale.append((target, calls))
        return missing, stale
