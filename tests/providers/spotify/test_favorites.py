"""Two-way favorite convergence and fail-closed snapshot tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType, TaskStatus
from music_assistant_models.media_items import Artist, ProviderMapping

from music_assistant.helpers.api import APICommandHandler
from music_assistant.providers.spotify.favorites import MEDIA_TYPES, FavoritesSync, reconcile


@pytest.mark.parametrize(
    ("previous", "local", "remote", "expected"),
    [
        (None, False, True, True),
        (None, True, False, True),
        (None, False, False, False),
        (True, False, True, False),
        (True, True, False, False),
        (False, True, False, True),
        (False, False, True, True),
        (True, False, False, False),
        (False, True, True, True),
    ],
)
def test_reconcile(previous: bool | None, local: bool, remote: bool, expected: bool) -> None:
    """Merge first-run differences; later changes and crash-converged pairs win."""
    assert reconcile(previous, local, remote) == expected


def artist(favorite: bool, item_id: str = "1") -> Artist:
    """Build a library artist with an exact Spotify mapping."""
    return Artist(
        item_id=item_id,
        provider="library",
        name="Artist",
        favorite=favorite,
        provider_mappings={
            ProviderMapping(
                item_id="sp1", provider_domain="spotify", provider_instance="spotify--test"
            )
        },
    )


@pytest.fixture
def sync(tmp_path: Any) -> FavoritesSync:
    """Create a sync with realistic mutable library and provider boundaries."""
    provider = MagicMock(instance_id="spotify--test", account_id="account", available=True)
    provider.mass.storage_path = str(tmp_path)
    provider.mass.tasks.list_tasks.return_value = []
    provider.library_add = AsyncMock(return_value=True)
    provider.library_remove = AsyncMock(return_value=True)
    return FavoritesSync(provider)


def snapshots(sync: Any, local: Artist | None, remote: bool) -> None:
    """Arrange complete snapshots and the current library read."""
    local_items: dict[MediaType, dict[str, Artist]] = {kind: {} for kind in MEDIA_TYPES}
    remote_items: dict[MediaType, dict[str, Artist]] = {kind: {} for kind in MEDIA_TYPES}
    if local:
        local_items[MediaType.ARTIST]["sp1"] = local
    if remote:
        remote_items[MediaType.ARTIST]["sp1"] = artist(False, "sp1")
    sync._local_snapshot = AsyncMock(return_value=local_items)
    sync._remote_snapshot = AsyncMock(return_value=remote_items)
    ctrl = sync.mass.music.get_controller.return_value
    ctrl.get_library_item_by_prov_id = AsyncMock(return_value=local)
    ctrl.add_item_to_library = AsyncMock(return_value=artist(False))
    ctrl.set_favorite = AsyncMock()


@pytest.mark.asyncio
async def test_initial_import(sync: Any) -> None:
    """Spotify saves become MA favorites without re-saving anything remotely."""
    snapshots(sync, artist(False), True)
    result = await sync.run()
    assert result["to_music_assistant"] == 1
    sync.mass.music.get_controller.return_value.set_favorite.assert_awaited_once_with("1", True)
    sync.provider.library_add.assert_not_awaited()
    assert sync._read_state()["baseline"]["artist"] == {"sp1": True}


@pytest.mark.asyncio
async def test_export_uses_provider_id(sync: Any) -> None:
    """An existing MA favorite saves the mapped Spotify ID, never the database ID."""
    item = artist(True)
    snapshots(sync, item, False)
    await sync.run()
    sent = sync.provider.library_add.call_args.args[0]
    assert sent.item_id == "sp1"
    assert item.item_id == "1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("local", "remote", "direction"),
    [(False, True, "to_spotify"), (True, False, "to_music_assistant")],
)
async def test_removals(sync: Any, local: bool, remote: bool, direction: str) -> None:
    """Each side's removal propagates after the initial union without deleting MA items."""
    sync._write_state(
        {"version": 1, "account_id": "account", "baseline": {"artist": {"sp1": True}}}
    )
    snapshots(sync, artist(local), remote)
    assert (await sync.run())[direction] == 1
    assert sync._read_state()["baseline"]["artist"]["sp1"] is False
    sync.mass.music.get_controller.return_value.remove_item_from_library.assert_not_called()


@pytest.mark.asyncio
async def test_failed_snapshot_has_no_writes(sync: Any) -> None:
    """A partial/failed provider read cannot be interpreted as removals."""
    snapshots(sync, artist(True), False)
    sync._remote_snapshot.side_effect = RuntimeError("rate limit")
    with pytest.raises(RuntimeError, match="rate limit"):
        await sync.run()
    sync.provider.library_add.assert_not_awaited()
    sync.provider.library_remove.assert_not_awaited()
    assert not sync.path.exists()


@pytest.mark.asyncio
async def test_failed_write_not_checkpointed(sync: Any) -> None:
    """Failed saves remain first-run differences on retry."""
    snapshots(sync, artist(True), False)
    sync.provider.library_add.side_effect = RuntimeError("write failed")
    with pytest.raises(RuntimeError, match="write failed"):
        await sync.run()
    assert sync._read_state()["baseline"] == {}


@pytest.mark.asyncio
async def test_concurrent_edit_deferred(sync: Any) -> None:
    """Do not overwrite a heart changed while snapshots were being read."""
    snapshots(sync, artist(False), True)
    sync.mass.music.get_controller.return_value.get_library_item_by_prov_id.return_value = artist(
        True
    )
    assert (await sync.run())["deferred"] == 1
    assert sync._read_state()["baseline"] == {}


@pytest.mark.asyncio
async def test_native_sync_defers(sync: Any) -> None:
    """Library membership must settle before favorite reconciliation."""
    sync.mass.tasks.list_tasks.return_value = [
        SimpleNamespace(id="music_sync_spotify--test_track", status=TaskStatus.RUNNING)
    ]
    with pytest.raises(RuntimeError, match="library sync is active"):
        await sync.run()
    sync.provider.library_remove.assert_not_awaited()


def test_account_change_blocks(sync: Any) -> None:
    """Never reuse another account's removal history."""
    sync._write_state({"version": 1, "account_id": "another", "baseline": {}})
    with pytest.raises(RuntimeError, match="different account"):
        sync._read_state()


@pytest.mark.asyncio
async def test_incomplete_pagination_blocks(sync: Any) -> None:
    """A valid HTTP response with missing items is not a complete empty library."""
    sync.provider._get_data = AsyncMock(return_value={"total": 2, "items": [], "next": None})
    with pytest.raises(RuntimeError, match="Incomplete Spotify"):
        await sync._read_collection(MediaType.TRACK)


@pytest.mark.asyncio
async def test_idempotent_after_restart(sync: Any) -> None:
    """A converged checkpoint makes a subsequent run a no-op."""
    sync._write_state(
        {"version": 1, "account_id": "account", "baseline": {"artist": {"sp1": True}}}
    )
    snapshots(sync, artist(True), True)
    assert await sync.run() == {"to_spotify": 0, "to_music_assistant": 0, "deferred": 0}
    sync.provider.library_add.assert_not_awaited()
    sync.mass.music.get_controller.return_value.set_favorite.assert_not_awaited()


def test_trigger_is_a_valid_api_handler(sync: Any) -> None:
    """The dynamically registered command resolves its runtime return annotation."""
    handler = APICommandHandler.parse("test/favorites", sync.trigger)
    assert handler.command == "test/favorites"


@pytest.mark.asyncio
async def test_missing_library_item_is_restored(sync: Any) -> None:
    """Losing a cached MA row must never turn into a Spotify unsave."""
    sync._write_state(
        {"version": 1, "account_id": "account", "baseline": {"artist": {"sp1": True}}}
    )
    snapshots(sync, None, True)
    assert (await sync.run())["to_music_assistant"] == 1
    sync.provider.library_remove.assert_not_awaited()
