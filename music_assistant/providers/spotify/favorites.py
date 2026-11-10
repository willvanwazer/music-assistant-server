"""Opt-in reconciliation of Spotify saves and Music Assistant favorites."""

from __future__ import annotations

import asyncio
import json
import os
from copy import copy
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.auth import Scope
from music_assistant_models.enums import MediaType, TaskStatus
from music_assistant_models.media_items import Album, Artist, Track

from music_assistant.helpers.datetime import utc

from .parsers import parse_album, parse_artist, parse_track

if TYPE_CHECKING:
    from music_assistant_models.background_task import BackgroundTask

    from music_assistant.controllers.music.media.base import MediaControllerBase

    from .provider import SpotifyProvider

type FavoriteItem = Artist | Album | Track

CONF_SYNC_FAVORITES = "sync_favorites"
INTERVAL = 15 * 60
MEDIA_TYPES = (MediaType.TRACK, MediaType.ALBUM, MediaType.ARTIST)


def reconcile(previous: bool | None, local: bool, remote: bool) -> bool:
    """Merge first-run favorites, then propagate the side changed since convergence."""
    if local == remote:
        return local
    if previous is None:
        return local or remote
    return remote if local == previous else local


class FavoritesSync:
    """Keep one Spotify account's favorites converged without copying credentials."""

    def __init__(self, provider: SpotifyProvider) -> None:
        """Initialize the sync attached to a loaded Spotify provider."""
        self.provider = provider
        self.mass = provider.mass
        self.task_id = f"spotify_favorites_{provider.instance_id}"
        self.timer_id = f"{self.task_id}_timer"
        self.path = Path(self.mass.storage_path) / f"{self.task_id}.json"
        self.lock = asyncio.Lock()
        self.unregister = self.mass.register_api_command(
            f"spotify/{provider.instance_id}/sync_favorites",
            self.trigger,
            required_scope=Scope.LIBRARY_MANAGE,
        )

    def start(self) -> None:
        """Schedule the first reconciliation after provider startup."""
        self.mass.call_later(60, self.queue, task_id=self.timer_id)

    async def stop(self) -> None:
        """Stop the timer and drain a running reconciliation before provider unload."""
        self.mass.cancel_timer(self.timer_id)
        self.unregister()
        await self.mass.tasks.unregister_scheduled_task_and_wait(
            self.task_id, clear_persisted_state=False
        )

    async def trigger(self) -> BackgroundTask:
        """Queue a reconciliation without overlapping an existing run."""
        return self.queue()

    def queue(self) -> BackgroundTask:
        """Queue work in the visible task manager and schedule the next check."""
        self.mass.call_later(INTERVAL, self.queue, task_id=self.timer_id)
        return self.mass.tasks.run_background_task(
            task_id=self.task_id,
            name=f"Sync {self.provider.name} favorites (every 15 minutes)",
            handler=self.run,
            metadata={"provider_instance": self.provider.instance_id},
            allow_retry=True,
        )

    async def run(self) -> dict[str, Any]:
        """Reconcile complete snapshots and persist only converged item states."""
        async with self.lock:
            if not self.provider.available or not self.provider.account_id:
                raise RuntimeError("Spotify is unavailable; favorites were not changed")
            self._check_library_sync()
            state = await asyncio.to_thread(self._read_state)
            remote = await self._remote_snapshot()
            local = await self._local_snapshot()
            self._check_library_sync()
            plans = self._plan(state, local, remote)
            counts = {"to_spotify": 0, "to_music_assistant": 0, "deferred": 0}
            try:
                for media_type, item_id, desired in plans:
                    self._check_library_sync()
                    changed = await self._apply(
                        media_type, item_id, desired, local[media_type], remote[media_type]
                    )
                    if changed == "deferred":
                        counts["deferred"] += 1
                        continue
                    if changed:
                        counts[changed] += 1
                    state["baseline"].setdefault(media_type.value, {})[item_id] = desired
            finally:
                # A retry can safely finish writes that succeeded before a crash: a converged
                # pair always wins, even if its checkpoint still has the previous value.
                await asyncio.to_thread(self._write_state, state)
            state["last_success"] = utc().isoformat()
            state["counts"] = counts
            state["spotify_counts"] = {kind.value: len(items) for kind, items in remote.items()}
            await asyncio.to_thread(self._write_state, state)
            self.provider.logger.info("Favorites sync complete: %s", counts)
            return counts

    def _check_library_sync(self) -> None:
        active = (TaskStatus.RUNNING, TaskStatus.PENDING)
        prefix = f"music_sync_{self.provider.instance_id}_"
        if any(
            t.id.startswith(prefix) and t.status in active for t in self.mass.tasks.list_tasks()
        ):
            raise RuntimeError("Spotify library sync is active; favorites will retry next interval")

    def _read_state(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "account_id": self.provider.account_id, "baseline": {}}
        state = cast("dict[str, Any]", json.loads(self.path.read_text()))
        if state.get("version") != 1 or state.get("account_id") != self.provider.account_id:
            raise RuntimeError("Favorites checkpoint belongs to a different account or version")
        if not isinstance(state.get("baseline"), dict) or any(
            not isinstance(items, dict) or any(type(value) is not bool for value in items.values())
            for items in state["baseline"].values()
        ):
            raise RuntimeError("Invalid favorites checkpoint; no changes made")
        return state

    def _write_state(self, state: dict[str, Any]) -> None:
        temp = self.path.with_suffix(".tmp")
        with temp.open("w") as handle:
            json.dump(state, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(self.path)

    async def _remote_snapshot(self) -> dict[MediaType, dict[str, FavoriteItem]]:
        result: dict[MediaType, dict[str, FavoriteItem]] = {}
        for media_type in MEDIA_TYPES:
            result[media_type] = await self._read_collection(media_type)
        return result

    async def _read_collection(self, media_type: MediaType) -> dict[str, FavoriteItem]:
        # Saved membership changes independently of catalog metadata. Reconciliation needs
        # current, complete membership, not the ordinary library listing cache.
        endpoint = "me/following" if media_type == MediaType.ARTIST else f"me/{media_type.value}s"
        params: dict[str, Any] = {"limit": 50}
        if media_type == MediaType.ARTIST:
            params["type"] = "artist"
        else:
            params["offset"] = 0
        result: dict[str, FavoriteItem] = {}
        expected: int | None = None
        seen_pages: set[str] = set()
        while True:
            page = await self.provider._get_data(endpoint, **params)
            data = page["artists"] if media_type == MediaType.ARTIST else page
            total = data["total"]
            if expected is not None and total != expected:
                raise RuntimeError("Spotify favorites changed during pagination; retry required")
            expected = total
            for entry in data["items"]:
                raw = entry if media_type == MediaType.ARTIST else entry[media_type.value]
                if not raw or not raw.get("id"):
                    raise RuntimeError("Incomplete Spotify favorite entry; no changes made")
                if media_type == MediaType.TRACK:
                    result[raw["id"]] = parse_track(raw, self.provider)
                elif media_type == MediaType.ALBUM:
                    result[raw["id"]] = parse_album(raw, self.provider)
                else:
                    result[raw["id"]] = parse_artist(raw, self.provider)
            if not data["next"]:
                break
            if not data["items"] or data["next"] in seen_pages:
                raise RuntimeError("Incomplete Spotify pagination; no changes made")
            seen_pages.add(data["next"])
            if media_type == MediaType.ARTIST:
                params["after"] = data["cursors"]["after"]
            else:
                params["offset"] += len(data["items"])
        if len(result) != expected:
            raise RuntimeError("Incomplete Spotify favorites snapshot; no changes made")
        return result

    async def _local_snapshot(self) -> dict[MediaType, dict[str, FavoriteItem]]:
        result: dict[MediaType, dict[str, FavoriteItem]] = {}
        for media_type in MEDIA_TYPES:
            ctrl = cast(
                "MediaControllerBase[FavoriteItem]", self.mass.music.get_controller(media_type)
            )
            items: dict[str, FavoriteItem] = {}
            async for item in ctrl.iter_library_items(
                provider=self.provider.instance_id, library_items_only=False
            ):
                for mapping in item.provider_mappings:
                    if mapping.provider_instance == self.provider.instance_id:
                        items[mapping.item_id] = item
            result[media_type] = items
        return result

    def _plan(
        self,
        state: dict[str, Any],
        local: dict[MediaType, dict[str, FavoriteItem]],
        remote: dict[MediaType, dict[str, FavoriteItem]],
    ) -> list[tuple[MediaType, str, bool]]:
        plans = []
        removals = 0
        for media_type in MEDIA_TYPES:
            baseline = state["baseline"].get(media_type.value, {})
            local_items = local[media_type]
            remote_items = remote[media_type]
            keys = set(baseline) | set(remote_items)
            # A merged library item can have several Spotify releases. Retain saved/tracked
            # releases; for a new MA favorite choose only one exact mapping, not every edition.
            grouped: dict[str, list[str]] = {}
            for item_id, candidate in local_items.items():
                if candidate.favorite:
                    grouped.setdefault(candidate.item_id, []).append(item_id)
            for ids in grouped.values():
                if not keys.intersection(ids):
                    keys.add(min(ids))
            for item_id in sorted(keys):
                item = local_items.get(item_id)
                remote_favorite = item_id in remote_items
                # A removed database row is not an explicit unheart. Restore saved items
                # instead of using a library reset to erase Spotify favorites.
                local_favorite = bool(item.favorite) if item else remote_favorite
                desired = reconcile(baseline.get(item_id), local_favorite, remote_favorite)
                if not desired and (local_favorite or remote_favorite):
                    removals += 1
                plans.append((media_type, item_id, desired))
        if removals > 50:
            raise RuntimeError(
                "Refusing over 50 favorite removals in one run; review account changes"
            )
        grouped_targets: dict[tuple[MediaType, str], set[bool]] = {}
        for kind, item_id, desired in plans:
            if item := local[kind].get(item_id):
                grouped_targets.setdefault((kind, item.item_id), set()).add(desired)
        ambiguous = {key for key, targets in grouped_targets.items() if len(targets) > 1}
        if ambiguous:
            self.provider.logger.warning(
                "Deferring %s merged items with conflicting releases", len(ambiguous)
            )
        return [
            plan
            for plan in plans
            if not (item := local[plan[0]].get(plan[1])) or (plan[0], item.item_id) not in ambiguous
        ]

    async def _apply(
        self,
        media_type: MediaType,
        item_id: str,
        desired: bool,
        local: dict[str, FavoriteItem],
        remote: dict[str, FavoriteItem],
    ) -> str | None:
        ctrl = cast("MediaControllerBase[FavoriteItem]", self.mass.music.get_controller(media_type))
        item = local.get(item_id)
        current = await ctrl.get_library_item_by_prov_id(item_id, self.provider.instance_id)
        if bool(current and current.favorite) != bool(item and item.favorite):
            return "deferred"
        if (item_id in remote) != desired:
            # Use the provider's authenticated, throttled API. Never remove a MA library
            # item: Spotify unsaves and the MA heart are the only intended mutations.
            if desired:
                if not current:
                    return "deferred"
                provider_item = copy(current)
                provider_item.item_id = item_id
                provider_item.provider = self.provider.instance_id
                if not await self.provider.library_add(provider_item):
                    raise RuntimeError("Spotify did not accept the favorite")
                await self._mark_in_library(media_type, item_id, current)
            elif not await self.provider.library_remove(item_id, media_type):
                raise RuntimeError("Spotify did not accept the favorite removal")
            return "to_spotify"
        if current and desired:
            await self._mark_in_library(media_type, item_id, current)
        if bool(current and current.favorite) != desired:
            if current is None:
                if not remote[item_id].available:
                    return "deferred"
                for mapping in remote[item_id].provider_mappings:
                    mapping.in_library = True
                current = await ctrl.add_item_to_library(remote[item_id])
            await ctrl.set_favorite(current.item_id, desired)
            return "to_music_assistant"
        return None

    async def _mark_in_library(
        self, media_type: MediaType, item_id: str, item: FavoriteItem
    ) -> None:
        # A previously browsed item can exist only in the metadata cache. A heart is
        # invisible in library listings until its exact saved mapping is in-library.
        changed = False
        for mapping in item.provider_mappings:
            if (
                mapping.provider_instance == self.provider.instance_id
                and mapping.item_id == item_id
                and not mapping.in_library
            ):
                mapping.in_library = True
                changed = True
        if changed:
            ctrl = self.mass.music.get_controller(media_type)
            await ctrl.set_provider_mappings(item.item_id, item.provider_mappings)
