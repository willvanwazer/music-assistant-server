"""Exercise personal playlist folders against real library storage."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

import pytest
from music_assistant_models.auth import Scope, User
from music_assistant_models.errors import (
    InsufficientPermissions,
    InvalidDataError,
    MediaNotFoundError,
)
from music_assistant_models.media_items import Playlist, ProviderMapping

from music_assistant.controllers.music.playlist_folders import (
    PlaylistFoldersController,
    create_playlist_folder_tables,
)
from music_assistant.controllers.webserver.helpers.auth_middleware import current_user
from music_assistant.mass import MusicAssistant


@pytest.fixture
async def folders(music_mass_module: MusicAssistant) -> AsyncGenerator[PlaylistFoldersController]:
    """Return a controller under an authenticated, isolated test user."""
    mass = music_mass_module
    token = current_user.set(User(user_id="folder-test", username="test", role="admin"))
    await mass.music.database.delete("playlist_folders", {"user_id": "folder-test"})
    try:
        yield mass.music.playlist_folders
    finally:
        current_user.reset(token)


async def add_playlist(mass: MusicAssistant, name: str, provider: str = "builtin") -> str:
    """Add a library playlist without contacting a provider."""
    item = Playlist(
        item_id=name,
        name=name,
        provider=provider,
        owner="test",
        is_editable=False,
        provider_mappings={
            ProviderMapping(
                item_id=name, provider_domain=provider, provider_instance=provider, in_library=True
            )
        },
    )
    result = await mass.music.playlists.add_item_to_library(item)
    return result.item_id


async def test_create_rename_pin_delete_preserves_playlists(
    folders: PlaylistFoldersController,
) -> None:
    """Deletion only removes organization, and names/pins survive storage reads."""
    item_id = await add_playlist(folders.mass, "folder-lifecycle")
    folder = await folders.create("  Dinner  ")
    await folders.move([item_id], folder["folder_id"])
    updated = await folders.update(folder["folder_id"], name="Evening", pinned=True)
    assert updated["name"] == "Evening"
    assert updated["pinned"]
    rows = await folders.list_folders()
    assert rows[0]["playlist_count"] == 1
    assert rows[0]["playlist_ids"] == [item_id]
    assert rows[0]["preview"][0].item_id == item_id
    assert [p.item_id for p in await folders.items(folder_id=folder["folder_id"])] == [item_id]
    assert item_id not in [p.item_id for p in await folders.items(unfiled=True)]
    await folders.delete(folder["folder_id"])
    assert item_id in [p.item_id for p in await folders.items(unfiled=True)]
    assert (await folders.mass.music.playlists.get_library_item(item_id)).item_id == item_id


async def test_user_isolation_and_no_anonymous_access(folders: PlaylistFoldersController) -> None:
    """Foreign IDs cannot be read, renamed, deleted or used as a move target."""
    folder = await folders.create("Private")
    token = current_user.set(User(user_id="another-user", username="other", role="admin"))
    try:
        assert await folders.list_folders() == []
        for operation in (
            folders.items(folder_id=folder["folder_id"]),
            folders.update(folder["folder_id"], name="Stolen"),
            folders.delete(folder["folder_id"]),
            folders.move(["1"], folder["folder_id"]),
        ):
            with pytest.raises(MediaNotFoundError):
                await operation
    finally:
        current_user.reset(token)
    token = current_user.set(None)
    try:
        with pytest.raises(InsufficientPermissions):
            await folders.list_folders()
    finally:
        current_user.reset(token)


async def test_atomic_moves_and_single_membership(folders: PlaylistFoldersController) -> None:
    """An invalid member leaves every playlist in its original folder."""
    item_id = await add_playlist(folders.mass, "folder-atomic")
    first, second = await folders.create("First"), await folders.create("Second")
    await folders.move([item_id, item_id], first["folder_id"])
    with pytest.raises(MediaNotFoundError):
        await folders.move([item_id, "99999999"], second["folder_id"])
    assert len(await folders.items(folder_id=first["folder_id"])) == 1
    await folders.move([item_id], second["folder_id"])
    assert await folders.items(folder_id=first["folder_id"]) == []
    await folders.move([item_id])
    assert await folders.items(folder_id=second["folder_id"]) == []


async def test_validation_and_concurrent_create(folders: PlaylistFoldersController) -> None:
    """Names are bounded, case-insensitively unique, and concurrent writes serialize."""
    for name in (" ", "a" * 101, "a\nb", "a\x7fb"):
        with pytest.raises(InvalidDataError):
            await folders.create(name)
    results = await asyncio.gather(
        folders.create("Focus"), folders.create("focus"), return_exceptions=True
    )
    assert sum(isinstance(result, InvalidDataError) for result in results) == 1
    for ids in ([], ["invalid"], ["0"], ["1"] * 501):
        with pytest.raises(InvalidDataError):
            await folders.move(ids)
    with pytest.raises(InvalidDataError):
        await folders.items(limit=0)


async def test_provider_filter_applies_to_members_counts_and_moves(
    folders: PlaylistFoldersController,
) -> None:
    """Changing allowed music sources never exposes disallowed playlists through folders."""
    allowed = await add_playlist(folders.mass, "folder-allowed", "source-a")
    blocked = await add_playlist(folders.mass, "folder-blocked", "source-b")
    folder = await folders.create("Mixed sources")
    await folders.move([allowed, blocked], folder["folder_id"])
    token = current_user.set(
        User(user_id="folder-test", username="test", role="user", provider_filter=["source-a"])
    )
    try:
        listed = await folders.list_folders()
        assert listed[0]["playlist_count"] == 1
        assert listed[0]["playlist_ids"] == [allowed]
        assert [p.item_id for p in await folders.items(folder_id=folder["folder_id"])] == [allowed]
        with pytest.raises(MediaNotFoundError):
            await folders.move([blocked])
    finally:
        current_user.reset(token)


async def test_pagination_search_and_idempotent_schema(folders: PlaylistFoldersController) -> None:
    """Folder filtering occurs before pagination and schema setup preserves contents."""
    ids = [await add_playlist(folders.mass, f"paged-{n}") for n in range(3)]
    folder = await folders.create("Pages")
    await folders.move(ids, folder["folder_id"])
    await create_playlist_folder_tables(folders.mass.music.database)
    await folders.mass.music.database.commit()
    pages = [
        await folders.items(folder_id=folder["folder_id"], limit=1, offset=n) for n in range(3)
    ]
    assert len({page[0].item_id for page in pages}) == 3
    assert [
        p.item_id for p in await folders.items(folder_id=folder["folder_id"], search="paged-1")
    ] == [ids[1]]


async def test_api_scopes_and_playlist_deletion_cleanup(folders: PlaylistFoldersController) -> None:
    """All commands enforce scopes; removed playlists leave no orphaned membership."""
    for command in ("list", "items", "create", "update", "delete", "move"):
        handler = folders.mass.command_handlers[f"music/playlist_folders/{command}"]
        expected = Scope.LIBRARY_READ if command in ("list", "items") else Scope.LIBRARY_WRITE
        assert handler.required_scope == expected
        assert not handler.allow_impersonation
    item_id = await add_playlist(folders.mass, "deleted-folder-member")
    folder = await folders.create("Cleanup")
    await folders.move([item_id], folder["folder_id"])
    await folders.mass.music.database.delete("playlists", {"item_id": int(item_id)})
    assert (await folders.list_folders())[0]["playlist_count"] == 0
    assert (
        await folders.mass.music.database.get_row(
            "playlist_folder_members", {"playlist_id": int(item_id)}
        )
        is None
    )
