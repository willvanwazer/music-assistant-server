"""Helper utilities for the cache controller."""

from __future__ import annotations

import asyncio
import functools
import time
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from types import MethodType
from typing import (
    TYPE_CHECKING,
    Any,
    Concatenate,
    ParamSpec,
    Protocol,
    TypeVar,
    cast,
    get_type_hints,
)

from music_assistant.controllers.cache.constants import (
    BYPASS_CACHE,
    DEFAULT_CACHE_EXPIRATION,
    LOGGER,
)
from music_assistant.helpers.api import parse_value
from music_assistant.helpers.json import SerializableType

if TYPE_CHECKING:
    from music_assistant import MusicAssistant


class _Cacheable(Protocol):
    """Protocol for objects that can use the @use_cache decorator."""

    @property
    def domain(self) -> str: ...

    @property
    def mass(self) -> MusicAssistant: ...


ProviderT = TypeVar("ProviderT", bound="_Cacheable")
P = ParamSpec("P")
R = TypeVar("R")


def use_cache(
    expiration: int = DEFAULT_CACHE_EXPIRATION,
    category: int = 0,
    persistent: bool = False,
    cache_checksum: str | None = None,
    allow_bypass: bool | None = None,
    base_class: Any = None,
    allow_expired_cache: bool = False,
    cache_none: bool = True,
) -> Callable[
    [Callable[Concatenate[ProviderT, P], Awaitable[R]]],
    Callable[Concatenate[ProviderT, P], Coroutine[Any, Any, R]],
]:
    """
    Return decorator that can be used to cache a method's result.

    Concurrent callers that miss the cache on the same key share one execution and each
    get their own copy of the result, or the one object when it cannot be copied.

    :param expiration: Time in seconds the cache entry should be valid.
    :param category: Category to group cache objects.
    :param persistent: If True, the entry survives cache clears.
    :param cache_checksum: Optional checksum to store with the cache object.
    :param allow_bypass: Whether to respect the BYPASS_CACHE context variable.
    :param base_class: If provided, reconstruct cached data using base_class.from_dict().
        Handles both single dicts and lists of dicts automatically.
        If not provided, falls back to type-annotation based reconstruction.
    :param allow_expired_cache: If True, enable stale-while-revalidate. When the cached
        entry has expired, return it immediately and trigger a background refresh that
        re-runs the wrapped function and updates the cache. Expired entries also
        survive the cache auto-cleanup task so they remain available as fallback data.
    :param cache_none: Whether a None result is cached and served like any other value
        (a negative hit, e.g. "no lyrics exist for this track"). Set to False for
        methods where None signals a (transient) failure, so the call is retried on
        the next invocation instead of serving a cached None.
    """
    allow_bypass = not persistent if allow_bypass is None else allow_bypass

    def _decorator(
        func: Callable[Concatenate[ProviderT, P], Awaitable[R]],
    ) -> Callable[Concatenate[ProviderT, P], Coroutine[Any, Any, R]]:
        def _reconstruct(cachedata: Any) -> R:
            if base_class is not None:
                return cast("R", cachedata)
            # fallback: reconstruct using the (memoized) return-type annotation
            return cast("R", parse_value(func.__name__, cachedata, _resolve_return_hint(func)))

        _refresh = _refresher(
            func,
            cache_none,
            expiration=expiration,
            category=category,
            checksum=cache_checksum,
            persistent=persistent,
            allow_expired_cache=allow_expired_cache,
        )

        @functools.wraps(func)
        async def wrapper(self: ProviderT, *args: P.args, **kwargs: P.kwargs) -> R:
            cache = self.mass.cache
            provider_id = getattr(self, "instance_id", self.domain)

            cache_key = _cache_key(func.__name__, args, kwargs)

            # single lookup that returns the entry, its freshness and whether it was found;
            # found distinguishes a stored None (a negative hit) from a cache miss. expired
            # entries are only read (and deserialized) when this call serves stale data
            cachedata, is_fresh, found = await cache.get_with_freshness(
                cache_key,
                provider=provider_id,
                checksum=cache_checksum,
                category=category,
                allow_bypass=allow_bypass,
                base_class=base_class,
                include_expired=allow_expired_cache,
            )
            # a found value counts as a hit, except a None that this call opted out of
            # caching (cache_none=False) which is treated as a miss and re-fetched
            cache_hit = found and (cache_none or cachedata is not None)

            if cache_hit and is_fresh:
                return _reconstruct(cachedata)

            def _store_task(result: R) -> Any:
                return cache.set(
                    key=cache_key,
                    data=cast("SerializableType", result),
                    expiration=expiration,
                    provider=provider_id,
                    category=category,
                    checksum=cache_checksum,
                    persistent=persistent,
                    allow_expired_cache=allow_expired_cache,
                )

            if cache_hit and allow_expired_cache:
                # serve stale data and refresh in the background;
                # task_id deduplicates concurrent refreshes for the same entry
                async def _background_refresh() -> None:
                    try:
                        # A prior refresh may finish while this caller's SQLite read
                        # is still returning the old row. Recheck before fetching again.
                        if (
                            await cache.get_with_freshness(
                                cache_key,
                                provider=provider_id,
                                checksum=cache_checksum,
                                category=category,
                                allow_bypass=allow_bypass,
                            )
                        )[1]:
                            return
                        await _refresh(self, *args, **kwargs)
                    except Exception:
                        LOGGER.exception(
                            "Background cache refresh failed for %s/%s",
                            provider_id,
                            cache_key,
                        )

                self.mass.create_task(
                    _background_refresh(),
                    task_id=f"cache_refresh.{provider_id}.{cache_key}",
                )
                return _reconstruct(cachedata)

            # cache miss (or expired entry without stale-while-revalidate):
            # Keep the shared flight alive until the cache write finishes. Otherwise
            # a caller arriving between the fetch and the write starts another fetch.
            async def _fetch_and_store() -> R:
                result = await func(self, *args, **kwargs)
                if cache_none or result is not None:
                    try:
                        await _store_task(result)
                    except Exception:
                        LOGGER.exception("Cache write failed for %s/%s", provider_id, cache_key)
                return result

            # a caller that bypasses this method's cache asked for the backend, so it
            # fetches alone rather than joining or publishing a flight
            if allow_bypass and BYPASS_CACHE.get():
                return await _fetch_and_store()

            async def _flight() -> _FlightOutcome[R]:
                # the outcome is returned rather than raised, to keep a routine failure quiet:
                # a MediaNotFoundError out of a cached lookup is an ordinary result that
                # callers deal with themselves, while a raising task would both draw a warning
                # from create_task and, once every caller has gone, have asyncio report it
                # against the shielded future
                outcome: _FlightOutcome[R] = _FlightOutcome()
                try:
                    # A previous flight can finish while this caller's initial
                    # SQLite read is returning its miss. Recheck inside the new
                    # flight before issuing another provider request.
                    data, fresh, found = await cache.get_with_freshness(
                        cache_key,
                        provider=provider_id,
                        checksum=cache_checksum,
                        category=category,
                        allow_bypass=allow_bypass,
                        base_class=base_class,
                    )
                    if found and fresh and (cache_none or data is not None):
                        outcome.result = _reconstruct(data)
                        return outcome
                    outcome.result = await _fetch_and_store()
                except Exception as err:
                    outcome.error = err
                return outcome

            # task_id folds concurrent callers for this key onto one execution
            flight = self.mass.create_task(
                _flight(), task_id=f"cache_flight.{provider_id}.{cache_key}"
            )
            if flight is asyncio.current_task():
                # a body that calls back into itself for the same key is handed the very
                # fetch it is running in, and awaiting that would wait on itself forever
                return await _fetch_and_store()
            # the shield keeps the shared task out of every caller's cancellation scope: a
            # caller giving up cancels neither the fetch nor the callers still waiting
            outcome = await asyncio.shield(flight)
            if outcome.error is not None:
                raise outcome.error
            # the fetched objects stay behind with the flight, which keeps them pristine for
            # the cache write; callers get a clone each because they do mutate results in
            # place (per-user podcast resume state, for one)
            try:
                return deepcopy(cast("R", outcome.result))
            except Exception as err:
                LOGGER.warning(
                    "Cannot copy the shared result for %s/%s, callers share one object: %s",
                    provider_id,
                    cache_key,
                    err,
                )
                return cast("R", outcome.result)

        setattr(
            wrapper,
            _OPTIONS_ATTR,
            _CacheOptions(
                category=category, allow_expired_cache=allow_expired_cache, refresh=_refresh
            ),
        )
        return wrapper

    return _decorator


class CacheStatus(StrEnum):
    """State of the cache entry a @use_cache call would read."""

    # no entry, or an expired one the method will not serve: a caller waits on the fetch
    MISSING = "missing"
    # an expired entry the method serves at once while it refreshes in the background
    STALE = "stale"
    FRESH = "fresh"


async def cache_status(
    method: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any
) -> CacheStatus:
    """
    Return the state of the cache entry that calling a @use_cache method would read.

    The cache is only inspected, the method is not called.

    :param method: The bound @use_cache method, e.g. provider.get_artist.
    :param args: The positional arguments the method would be called with.
    :param kwargs: The keyword arguments the method would be called with, passed the same
        way (positionally or by keyword) as the callers whose cache entry is meant.
    """
    owner, func, options = _cached_method(method)
    expires = await owner.mass.cache.get_expiration(
        _cache_key(func.__name__, args, kwargs),
        provider=getattr(owner, "instance_id", owner.domain),
        category=options.category,
    )
    if expires is None:
        return CacheStatus.MISSING
    if expires >= time.time():
        return CacheStatus.FRESH
    return CacheStatus.STALE if options.allow_expired_cache else CacheStatus.MISSING


async def refresh_cache(method: Callable[..., Awaitable[R]], *args: Any, **kwargs: Any) -> R:
    """
    Fetch the data of a @use_cache call and store it, whatever the cache holds.

    Nested cached calls are read through the cache as usual. This is the refresh that a
    stale-while-revalidate read starts in the background, awaited.

    :param method: The bound @use_cache method, e.g. provider.get_artist.
    :param args: The positional arguments to call the method with.
    :param kwargs: The keyword arguments to call the method with.
    """
    owner, _func, options = _cached_method(method)
    return cast("R", await options.refresh(owner, *args, **kwargs))


def _cached_method(
    method: Callable[..., Awaitable[Any]],
) -> tuple[_Cacheable, Callable[..., Any], _CacheOptions]:
    """Return the owner, function and cache options of a bound @use_cache method."""
    if not isinstance(method, MethodType):
        raise TypeError(f"{method!r} is not a bound method")
    func = method.__func__
    return (
        cast("_Cacheable", method.__self__),
        func,
        cast("_CacheOptions", getattr(func, _OPTIONS_ATTR)),
    )


_OPTIONS_ATTR = "_use_cache_options"


@dataclass(frozen=True, slots=True)
class _CacheOptions:
    """Options a @use_cache method was decorated with, read by cache_status and refresh_cache."""

    category: int
    allow_expired_cache: bool
    refresh: Callable[..., Awaitable[Any]]


def _refresher[OwnerT: _Cacheable, **ArgsP, ResultT](
    func: Callable[Concatenate[OwnerT, ArgsP], Awaitable[ResultT]],
    cache_none: bool,
    **cache_options: Any,
) -> Callable[Concatenate[OwnerT, ArgsP], Coroutine[Any, Any, ResultT]]:
    """
    Return a function that runs func and stores its result under the call's cache key.

    :param func: The function decorated with @use_cache.
    :param cache_none: Whether a None result is stored.
    :param cache_options: The cache.set options the decorator stores entries with.
    """

    async def _refresh(owner: OwnerT, *args: ArgsP.args, **kwargs: ArgsP.kwargs) -> ResultT:
        result = await func(owner, *args, **kwargs)
        if cache_none or result is not None:
            await owner.mass.cache.set(
                key=_cache_key(func.__name__, args, kwargs),
                data=cast("SerializableType", result),
                provider=getattr(owner, "instance_id", owner.domain),
                **cache_options,
            )
        return result

    return _refresh


def _cache_key(func_name: str, args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> str:
    """Return the cache key of a call, built from the method name and its arguments."""
    cache_key_parts = [func_name, *args]
    for key in sorted(kwargs.keys()):
        cache_key_parts.append(f"{key}{kwargs[key]}")
    return ".".join(map(str, cache_key_parts))


@dataclass(slots=True)
class _FlightOutcome[ResultT]:
    """Outcome of one shared fetch, handed to every caller awaiting that fetch."""

    result: ResultT | None = None
    error: Exception | None = None


@functools.cache
def _resolve_return_hint(func: Callable[..., Any]) -> Any:
    """
    Return the resolved return-type annotation of func, memoized per function.

    A function's return annotation is invariant for the process lifetime, so it is resolved
    once and cached instead of re-running get_type_hints() — which re-evaluates the PEP-563
    string annotations — on every cache hit. Resolution stays lazy (it happens on the first
    hit, not at decoration time), so forward-reference handling is unchanged.

    :param func: The decorated function whose return-type annotation to resolve.
    """
    return get_type_hints(func)["return"]
