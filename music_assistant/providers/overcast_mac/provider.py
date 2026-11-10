"""
Overcast (Mac) provider for Music Assistant.

Reads podcasts, the Overcast queue ("Up Next"), in-progress and starred episodes
from the Overcast app on a Mac through overcastctl's private, read-only bearer
listener. Audio always comes through the Mac: Overcast's download when there is
one, otherwise relayed from the feed by the Mac. That covers private member
feeds too, whose credential-bearing URLs never leave the Mac.

Synchronization is two-way. Overcast's progress is read on every listing, and what
is played in Music Assistant is written back through overcastctl's guarded
set_progress once playback stops (see ``progress_sync.py``): each write closes and
reopens Overcast on the Mac, so nothing is written while an episode plays.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import ContentType, ImageType, MediaType, StreamType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemImage,
    MediaItemMetadata,
    MediaItemType,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import CONF_ENTRY_UNOFFICIAL_PROVIDER
from music_assistant.helpers.podcast_parsers import rank_episodes_by_date
from music_assistant.models.music_provider import MusicProvider

from .client import OvercastCtlClient
from .constants import CONF_SERVER_URL, CONF_TOKEN, PODCASTS_CACHE_S, SPECIAL_FOLDERS
from .progress_sync import ProgressSync

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant_models.config_entries import ConfigEntry


_MIME_TYPES = {
    "audio/mpeg": ContentType.MP3,
    "audio/mp3": ContentType.MP3,
    "audio/mp4": ContentType.M4A,
    "audio/x-m4a": ContentType.M4A,
    "audio/aac": ContentType.AAC,
}


class OvercastMacProvider(MusicProvider):
    """Provider for the Overcast library on a Mac running overcastctl."""

    _client: OvercastCtlClient
    _progress: ProgressSync
    _podcasts: dict[str, dict[str, Any]]
    _podcasts_fetched: float = 0.0

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the (options) config entries; the address and token come from setup."""
        return (CONF_ENTRY_UNOFFICIAL_PROVIDER,)

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._client = OvercastCtlClient(
            self.mass.http_session,
            str(self.get_setup_value(CONF_SERVER_URL)),
            str(self.get_setup_value(CONF_TOKEN)),
            self.logger,
        )
        self._podcasts = {}
        self._progress = ProgressSync(self._client, self.logger)
        # fail setup early on a wrong address or token
        await self._get_podcasts(force=True)

    async def unload(self, is_removed: bool = False) -> None:
        """Cancel pending progress writes."""
        await self._progress.close()

    async def on_played(
        self,
        media_type: MediaType,
        prov_item_id: str,
        fully_played: bool,
        position: int,
        media_item: MediaItemType,
        is_playing: bool = False,
    ) -> None:
        """Queue the listening progress to be written back to Overcast."""
        if media_type != MediaType.PODCAST_EPISODE:
            return
        _, episode_id = self._split_episode_id(prov_item_id)
        duration = int(getattr(media_item, "duration", 0) or 0)
        self._progress.report(episode_id, int(position), fully_played, duration, is_playing)

    @property
    def is_streaming_provider(self) -> bool:
        """Return False: the library mirrors the user's own Overcast subscriptions."""
        return False

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast]:
        """Retrieve the subscribed podcasts."""
        for data in (await self._get_podcasts(force=True)).values():
            yield self._convert_podcast(data)

    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get a podcast by its Overcast id."""
        podcasts = await self._get_podcasts()
        if (data := podcasts.get(prov_podcast_id)) is None:
            podcasts = await self._get_podcasts(force=True)
            data = podcasts.get(prov_podcast_id)
        if data is None:
            raise MediaNotFoundError(f"Podcast {prov_podcast_id} is not in Overcast")
        return self._convert_podcast(data)

    async def get_podcast_episodes(self, prov_podcast_id: str) -> AsyncGenerator[PodcastEpisode]:
        """Get every episode the Mac knows for the podcast, played ones included."""
        episodes = await self._client.paged("list_episodes", podcast=prov_podcast_id, state="all")
        positions = rank_episodes_by_date([ep.get("published") for ep in episodes])
        await self._get_podcasts()
        for position, data in zip(positions, episodes, strict=True):
            yield self._convert_episode(data, position=position)

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get a single episode."""
        _, episode_id = self._split_episode_id(prov_episode_id)
        data = await self._client.call("get_episode", episode_id=episode_id)
        await self._get_podcasts()
        episode = self._convert_episode(data)
        if summary := (data.get("summary") or data.get("notes")):
            episode.metadata.description = summary
        return episode

    async def browse(self, path: str) -> list[MediaItemType | BrowseFolder]:
        """
        Browse the special folders and subscribed podcasts.

        :param path: The browse path to resolve.
        """
        item_path = path.split("://", 1)[1] if "://" in path else path
        if not item_path:
            items: list[MediaItemType | BrowseFolder] = [
                BrowseFolder(
                    item_id=folder_id,
                    provider=self.instance_id,
                    path=f"{self.instance_id}://{folder_id}",
                    name=name,
                )
                for folder_id, name in SPECIAL_FOLDERS.items()
            ]
            items.extend(
                self._convert_podcast(data) for data in (await self._get_podcasts()).values()
            )
            return items
        if item_path in SPECIAL_FOLDERS:
            await self._get_podcasts()
            return list(await self._special_folder(item_path))
        return [episode async for episode in self.get_podcast_episodes(item_path)]

    async def get_resume_position(
        self, item_id: str, media_type: MediaType
    ) -> tuple[bool, int, datetime | None]:
        """Return fully_played, resume position (ms) and its timestamp from Overcast."""
        if media_type != MediaType.PODCAST_EPISODE:
            raise NotImplementedError
        _, episode_id = self._split_episode_id(item_id)
        data = await self._client.call("get_episode", episode_id=episode_id)
        played = bool(data.get("played"))
        progress = int(data.get("progress_seconds") or 0)
        if not played and not progress:
            # no Overcast progress: let MA fall back to its own playlog
            raise NotImplementedError
        return played, 0 if played else progress * 1000, _parse_datetime(data.get("last_played"))

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Stream the episode through the Mac: its local download, else relayed from the feed."""
        _, episode_id = self._split_episode_id(item_id)
        data = await self._client.call("get_episode", episode_id=episode_id)
        duration = int(data.get("duration_seconds") or 0) or None
        # the Mac serves Overcast's download, or relays the (possibly private) feed
        content_type, size = await self._client.audio_info(episode_id)
        audio_type = _MIME_TYPES.get(content_type or "") or ContentType.try_parse(
            content_type or ""
        )
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(content_type=audio_type),
            media_type=MediaType.PODCAST_EPISODE,
            stream_type=StreamType.CUSTOM,
            duration=duration,
            # byte-offset seeking resyncs cleanly only for MP3 frames
            can_seek=audio_type == ContentType.MP3 and bool(size and duration),
            allow_seek=True,
            data={"episode_id": episode_id, "size": size},
        )

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes]:
        """Stream the downloaded file, seeking by an approximate byte offset."""
        info = streamdetails.data or {}
        offset = 0
        if seek_position and info.get("size") and streamdetails.duration:
            offset = int(info["size"] * min(seek_position / streamdetails.duration, 0.999))
        async for chunk in self._client.stream_audio(info["episode_id"], offset):
            yield chunk

    async def _special_folder(self, folder: str) -> list[PodcastEpisode]:
        if folder == "up_next":
            rows = await self._client.paged("get_queue", allow_unordered=True)
        elif folder == "in_progress":
            rows = await self._client.paged("list_episodes", state="in-progress")
            # most recently listened first, like Overcast's own in-progress view
            rows.sort(key=lambda r: r.get("last_played") or "", reverse=True)
        else:
            rows = await self._client.paged("list_episodes", state="starred")
        return [self._convert_episode(row) for row in rows]

    async def _get_podcasts(self, force: bool = False) -> dict[str, dict[str, Any]]:
        """Return the subscribed podcasts by id, cached briefly for artwork and names."""
        if (
            force
            or not self._podcasts
            or time.monotonic() - self._podcasts_fetched > PODCASTS_CACHE_S
        ):
            rows = await self._client.paged("list_podcasts")
            self._podcasts = {str(row["id"]): row for row in rows}
            self._podcasts_fetched = time.monotonic()
        return self._podcasts

    def _images(self, podcast_id: str) -> UniqueList[MediaItemImage]:
        artwork = (self._podcasts.get(podcast_id) or {}).get("artwork_url")
        if not artwork:
            return UniqueList()
        return UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=artwork,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            ]
        )

    def _convert_podcast(self, data: dict[str, Any]) -> Podcast:
        podcast_id = str(data["id"])
        return Podcast(
            item_id=podcast_id,
            provider=self.instance_id,
            name=data.get("title") or "",
            publisher=data.get("author") or None,
            total_episodes=data.get("episodes"),
            provider_mappings={
                ProviderMapping(
                    item_id=podcast_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            metadata=MediaItemMetadata(images=self._images(podcast_id)),
        )

    def _convert_episode(self, data: dict[str, Any], position: int = 0) -> PodcastEpisode:
        podcast_id, episode_id = str(data["podcast_id"]), str(data["id"])
        item_id = f"{podcast_id}:{episode_id}"
        played = bool(data.get("played"))
        episode = PodcastEpisode(
            item_id=item_id,
            provider=self.instance_id,
            name=data.get("display_title") or data.get("title") or "Unknown episode",
            podcast=ItemMapping(
                media_type=MediaType.PODCAST,
                item_id=podcast_id,
                provider=self.instance_id,
                name=data.get("podcast") or "",
                image=next(iter(self._images(podcast_id)), None),
            ),
            position=position,
            duration=int(data.get("duration_seconds") or 0),
            fully_played=played,
            resume_position_ms=0 if played else int(data.get("progress_seconds") or 0) * 1000,
            provider_mappings={
                ProviderMapping(
                    item_id=item_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            metadata=MediaItemMetadata(images=self._images(podcast_id)),
        )
        if published := _parse_datetime(data.get("published")):
            episode.metadata.release_date = published
        return episode

    @staticmethod
    def _split_episode_id(item_id: str) -> tuple[str, str]:
        podcast_id, _, episode_id = item_id.partition(":")
        if not episode_id.isdigit():
            raise MediaNotFoundError(f"Invalid Overcast episode id {item_id}")
        return podcast_id, episode_id


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
