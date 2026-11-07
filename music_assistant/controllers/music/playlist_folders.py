"""User-owned playlist organization, independent of provider playlist contents."""

from __future__ import annotations

import asyncio
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from mashumaro.mixins.orjson import DataClassORJSONMixin
from music_assistant_models.auth import Scope
from music_assistant_models.enums import EventType
from music_assistant_models.errors import (
    InsufficientPermissions,
    InvalidDataError,
    MediaNotFoundError,
)
from music_assistant_models.media_items import Playlist  # noqa: TC002

from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user
from music_assistant.helpers.json import json_dumps

if TYPE_CHECKING:
    from music_assistant import MusicAssistant
    from music_assistant.helpers.database import DatabaseConnection

FOLDERS_TABLE = "playlist_folders"
MEMBERS_TABLE = "playlist_folder_members"
MAX_FOLDERS = 100
MAX_MOVE = 500


@dataclass
class PlaylistFolderSummary(DataClassORJSONMixin):
    """A personal folder with typed previews for deferred transport serialization."""

    folder_id: str
    name: str
    pinned: bool
    playlist_count: int
    playlist_ids: list[str]
    preview: list[Playlist]


async def create_playlist_folder_tables(database: DatabaseConnection) -> None:
    """Create the additive folder schema and cleanup triggers, idempotently."""
    await database.execute(f"""CREATE TABLE IF NOT EXISTS {FOLDERS_TABLE} (
        folder_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL,
        name_key TEXT NOT NULL, pinned BOOLEAN NOT NULL DEFAULT 0,
        UNIQUE(user_id, name_key))""")
    await database.execute(f"""CREATE TABLE IF NOT EXISTS {MEMBERS_TABLE} (
        user_id TEXT NOT NULL, playlist_id INTEGER NOT NULL, folder_id TEXT NOT NULL,
        PRIMARY KEY(user_id, playlist_id))""")
    await database.execute(f"""CREATE INDEX IF NOT EXISTS playlist_folder_members_folder
        ON {MEMBERS_TABLE}(folder_id, user_id)""")
    await database.execute(f"""CREATE TRIGGER IF NOT EXISTS playlist_folder_deleted
        AFTER DELETE ON {FOLDERS_TABLE} BEGIN
        DELETE FROM {MEMBERS_TABLE} WHERE folder_id=OLD.folder_id; END""")
    await database.execute(f"""CREATE TRIGGER IF NOT EXISTS playlist_folder_playlist_deleted
        AFTER DELETE ON playlists BEGIN
        DELETE FROM {MEMBERS_TABLE} WHERE playlist_id=OLD.item_id; END""")


class PlaylistFoldersController:
    """Manage flat folders belonging to the authenticated user."""

    def __init__(self, mass: MusicAssistant) -> None:
        """Register the folder API."""
        self.mass = mass
        self._lock = asyncio.Lock()
        for command, handler, scope in (
            ("list", self.list_folders, Scope.LIBRARY_READ),
            ("items", self.items, Scope.LIBRARY_READ),
            ("create", self.create, Scope.LIBRARY_WRITE),
            ("update", self.update, Scope.LIBRARY_WRITE),
            ("delete", self.delete, Scope.LIBRARY_WRITE),
            ("move", self.move, Scope.LIBRARY_WRITE),
        ):
            mass.register_api_command(
                f"music/playlist_folders/{command}", handler, required_scope=scope
            )

    async def list_folders(self) -> list[PlaylistFolderSummary]:
        """List personal folders with visible playlist counts and up to four cover previews."""
        user_id = self._user_id()
        async with self._lock:
            rows = await self.mass.music.database.get_rows(
                FOLDERS_TABLE, {"user_id": user_id}, limit=MAX_FOLDERS
            )
            members = await self._visible_members(user_id)
            result = []
            for row in sorted(rows, key=lambda row: row["name_key"]):
                ids = [str(m["playlist_id"]) for m in members if m["folder_id"] == row["folder_id"]]
                preview = await self.items(folder_id=row["folder_id"], limit=4)
                result.append(
                    PlaylistFolderSummary(
                        folder_id=row["folder_id"],
                        name=row["name"],
                        pinned=bool(row["pinned"]),
                        playlist_count=len(ids),
                        playlist_ids=ids,
                        preview=preview,
                    )
                )
            return result

    async def items(
        self,
        folder_id: str | None = None,
        unfiled: bool = False,
        favorite: bool | None = None,
        search: str | None = None,
        limit: int = 100,
        offset: int = 0,
        order_by: str = "sort_name",
        provider: str | list[str] | None = None,
        genre: int | list[int] | None = None,
    ) -> list[Any]:
        """List visible library playlists in a folder, unfiled, or across all folders."""
        user_id = self._user_id()
        if not 1 <= limit <= 500 or offset < 0:
            raise InvalidDataError("limit must be 1..500 and offset must be nonnegative")
        if folder_id is not None and unfiled:
            raise InvalidDataError("folder_id and unfiled are mutually exclusive")
        params: dict[str, Any] = {"folder_user": user_id}
        clauses: list[str] = []
        if folder_id is not None:
            await self._get_folder(folder_id, user_id)
            params["folder_id"] = folder_id
            clauses.append(f"""EXISTS (SELECT 1 FROM {MEMBERS_TABLE} fm
                WHERE fm.user_id=:folder_user AND fm.folder_id=:folder_id
                AND fm.playlist_id=playlists.item_id)""")
        elif unfiled:
            clauses.append(f"""NOT EXISTS (SELECT 1 FROM {MEMBERS_TABLE} fm
                WHERE fm.user_id=:folder_user AND fm.playlist_id=playlists.item_id)""")
        playlists = self.mass.music.playlists
        return await playlists.get_library_items_by_query(
            favorite=favorite,
            search=search,
            limit=limit,
            offset=offset,
            order_by=order_by,
            provider_filter=playlists._ensure_provider_filter(provider),
            genre_ids=genre,
            in_library_only=True,
            summary=True,
            extra_query_parts=clauses,
            extra_query_params=params,
        )

    async def create(self, name: str) -> dict[str, Any]:
        """Create an empty personal folder."""
        user_id = self._user_id()
        name = self._validate_name(name)
        async with self._lock:
            rows = await self.mass.music.database.get_rows(
                FOLDERS_TABLE, {"user_id": user_id}, limit=MAX_FOLDERS
            )
            if len(rows) >= MAX_FOLDERS:
                raise InvalidDataError("Maximum of 100 playlist folders reached")
            await self._check_name(name, user_id)
            folder_id = uuid4().hex
            await self.mass.music.database.insert(
                FOLDERS_TABLE,
                {
                    "folder_id": folder_id,
                    "user_id": user_id,
                    "name": name,
                    "name_key": name.casefold(),
                    "pinned": False,
                },
            )
        self._notify(user_id)
        return {"folder_id": folder_id, "name": name, "pinned": False}

    async def update(
        self,
        folder_id: str,
        name: str | None = None,
        pinned: bool | None = None,
    ) -> dict[str, Any]:
        """Rename a folder or change its sidebar pin."""
        user_id = self._user_id()
        async with self._lock:
            row = await self._get_folder(folder_id, user_id)
            values: dict[str, Any] = {}
            if name is not None:
                name = self._validate_name(name)
                await self._check_name(name, user_id, folder_id)
                values.update(name=name, name_key=name.casefold())
            if pinned is not None:
                values["pinned"] = pinned
            if values:
                await self.mass.music.database.update(
                    FOLDERS_TABLE, {"folder_id": folder_id, "user_id": user_id}, values
                )
            updated = {**row, **values}
        self._notify(user_id)
        return {"folder_id": folder_id, "name": updated["name"], "pinned": bool(updated["pinned"])}

    async def delete(self, folder_id: str) -> None:
        """Delete a folder, returning its playlists to Unfiled without deleting playlists."""
        user_id = self._user_id()
        async with self._lock:
            await self._get_folder(folder_id, user_id)
            await self.mass.music.database.delete(
                FOLDERS_TABLE, {"folder_id": folder_id, "user_id": user_id}
            )
        self._notify(user_id)

    async def move(self, playlist_ids: list[str], folder_id: str | None = None) -> None:
        """Atomically move visible library playlists into a personal folder or Unfiled."""
        user_id = self._user_id()
        if not playlist_ids or len(playlist_ids) > MAX_MOVE:
            raise InvalidDataError("Provide between 1 and 500 library playlist IDs")
        if any(
            not item.isascii()
            or not item.isdecimal()
            or len(item) > 19
            or not 1 <= int(item) <= 9223372036854775807
            for item in playlist_ids
        ):
            raise InvalidDataError("playlist_ids must be positive library playlist IDs")
        ids = sorted({str(int(item)) for item in playlist_ids})
        async with self._lock:
            if folder_id is not None:
                await self._get_folder(folder_id, user_id)
            playlists = self.mass.music.playlists
            visible = await playlists.get_library_items_by_query(
                extra_query_parts=["playlists.item_id IN (SELECT value FROM json_each(:ids))"],
                extra_query_params={"ids": json_dumps(ids)},
                limit=MAX_MOVE,
                provider_filter=playlists._ensure_provider_filter(None),
                in_library_only=True,
                summary=True,
            )
            if {str(item.item_id) for item in visible} != set(ids):
                raise MediaNotFoundError(
                    "One or more library playlists are missing or inaccessible"
                )
            params = {"user_id": user_id, "folder_id": folder_id, "ids": json_dumps(ids)}
            if folder_id is None:
                await self.mass.music.database.execute_write(
                    f"""DELETE FROM {MEMBERS_TABLE}
                    WHERE user_id=:user_id AND playlist_id IN
                    (SELECT value FROM json_each(:ids))""",
                    params,
                )
            else:
                await self.mass.music.database.execute_write(
                    f"""INSERT INTO {MEMBERS_TABLE}
                    (user_id, playlist_id, folder_id)
                    SELECT :user_id, CAST(value AS INTEGER), :folder_id
                    FROM json_each(:ids) WHERE true
                    ON CONFLICT(user_id, playlist_id) DO UPDATE SET folder_id=excluded.folder_id""",
                    params,
                )
        self._notify(user_id)

    def _user_id(self) -> str:
        user = get_current_user()
        if user is None:
            raise InsufficientPermissions("Playlist folders require an authenticated user")
        return user.user_id

    async def _get_folder(self, folder_id: str, user_id: str) -> dict[str, Any]:
        row = await self.mass.music.database.get_row(
            FOLDERS_TABLE, {"folder_id": folder_id, "user_id": user_id}
        )
        if row is None:
            raise MediaNotFoundError("Playlist folder not found")
        return dict(row)

    async def _check_name(self, name: str, user_id: str, folder_id: str | None = None) -> None:
        row = await self.mass.music.database.get_row(
            FOLDERS_TABLE, {"user_id": user_id, "name_key": name.casefold()}
        )
        if row and row["folder_id"] != folder_id:
            raise InvalidDataError("A playlist folder with this name already exists")

    def _validate_name(self, name: str) -> str:
        name = name.strip()
        if not name or len(name) > 100 or any(unicodedata.category(char) == "Cc" for char in name):
            raise InvalidDataError(
                "Folder names must contain 1..100 characters without control characters"
            )
        return name

    async def _visible_members(self, user_id: str) -> list[Any]:
        providers = self.mass.music.playlists._ensure_provider_filter(None)
        restriction = (
            "AND pm.provider_instance IN (SELECT value FROM json_each(:providers))"
            if providers
            else ""
        )
        return await self.mass.music.database.get_rows_from_query(
            f"""
            SELECT DISTINCT fm.folder_id, fm.playlist_id FROM {MEMBERS_TABLE} fm
            JOIN playlists p ON p.item_id=fm.playlist_id
            JOIN provider_mappings pm ON pm.item_id=p.item_id AND pm.media_type='playlist'
            WHERE fm.user_id=:user_id AND pm.in_library=1 {restriction}
            """,
            {"user_id": user_id, "providers": json_dumps(providers)},
            limit=0,
        )

    def _notify(self, user_id: str) -> None:
        # Broadcast only an invalidation key; folder names and contents stay user-scoped.
        self.mass.signal_event(EventType.MEDIA_ITEM_UPDATED, f"playlist_folders://{user_id}", {})
