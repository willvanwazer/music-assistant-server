"""Minimal client for overcastctl's bearer-authenticated Streamable HTTP listener."""

from __future__ import annotations

import asyncio
import itertools
from typing import TYPE_CHECKING, Any

import aiohttp
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    ResourceTemporarilyUnavailable,
)

from .constants import (
    BUSY_BACKOFF_S,
    BUSY_RETRIES,
    MCP_PROTOCOL_VERSION,
    PAGE_SIZE,
    REQUEST_TIMEOUT_S,
)

if TYPE_CHECKING:
    import logging
    from collections.abc import AsyncGenerator


class OvercastCtlError(ResourceTemporarilyUnavailable):
    """An overcastctl tool error, carrying its code (e.g. ``playing``, ``conflict``)."""

    def __init__(self, message: str, code: str | None) -> None:
        """
        Initialize the error.

        :param message: The error message.
        :param code: overcastctl's error code.
        """
        super().__init__(message)
        self.code = code


class OvercastCtlClient:
    """Calls overcastctl's typed tools and fetches episode audio through the Mac."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        token: str,
        logger: logging.Logger,
    ) -> None:
        """
        Initialize the client.

        :param session: Shared aiohttp session.
        :param base_url: Listener root, e.g. http://100.75.16.86:8429.
        :param token: The listener's static bearer token.
        :param logger: Provider logger.
        """
        self._session = session
        self._base_url = base_url.rstrip("/").removesuffix("/mcp")
        # kept out of URLs, logs and ffmpeg arguments; only ever sent as a header
        self._auth = {"Authorization": f"Bearer {token}"}
        self._logger = logger
        self._ids = itertools.count(1)

    async def call(
        self, tool: str, *, timeout: float = REQUEST_TIMEOUT_S, **arguments: Any
    ) -> dict[str, Any]:
        """
        Call one overcastctl tool and return its structured result.

        :param tool: The tool name, e.g. ``list_podcasts``.
        :param timeout: Seconds to wait; writes reopen Overcast and wait for its sync.
        :param arguments: The tool's arguments; None values are omitted.
        """
        payload = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": "tools/call",
            "params": {
                "name": tool,
                "arguments": {k: v for k, v in arguments.items() if v is not None},
            },
        }
        headers = {
            **self._auth,
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        }
        for attempt in range(BUSY_RETRIES + 1):
            try:
                async with self._session.post(
                    f"{self._base_url}/mcp",
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as response:
                    if response.status == 401:
                        raise LoginFailed("overcastctl rejected the bearer token")
                    if response.status != 200:
                        raise ResourceTemporarilyUnavailable(
                            f"overcastctl answered HTTP {response.status}"
                        )
                    body = await response.json(content_type=None)
            except (TimeoutError, aiohttp.ClientError) as err:
                raise ResourceTemporarilyUnavailable(
                    "The Mac running overcastctl is unreachable (asleep, offline or logged out?)"
                ) from err
            if "error" in body:
                raise ResourceTemporarilyUnavailable(
                    f"overcastctl protocol error: {body['error'].get('message')}"
                )
            result = body.get("result") or {}
            content = result.get("structuredContent") or {}
            if not result.get("isError"):
                return content
            error = content.get("error") or {}
            code = error.get("code")
            if code == "busy" and attempt < BUSY_RETRIES:
                await asyncio.sleep(BUSY_BACKOFF_S * (2**attempt))
                continue
            message = error.get("message") or "overcastctl call failed"
            if code == "not_found":
                raise MediaNotFoundError(message)
            raise OvercastCtlError(f"overcastctl {tool}: {message}", code)
        raise OvercastCtlError("overcastctl stayed busy", "busy")  # pragma: no cover

    async def paged(self, tool: str, **arguments: Any) -> list[dict[str, Any]]:
        """
        Call a paged tool until ``next_offset`` runs out and return every item.

        :param tool: A tool returning ``items`` and ``next_offset``.
        :param arguments: Further tool arguments.
        """
        items: list[dict[str, Any]] = []
        offset: int | None = 0
        while offset is not None:
            page = await self.call(tool, limit=PAGE_SIZE, offset=offset, **arguments)
            items.extend(page.get("items") or [])
            offset = page.get("next_offset")
        return items

    async def audio_info(self, episode_id: str) -> tuple[str | None, int | None]:
        """
        Return the content type and size of a downloaded episode, or raise if not downloaded.

        :param episode_id: The Overcast episode id.
        """
        try:
            async with self._session.head(
                self._audio_url(episode_id),
                headers=self._auth,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S),
            ) as response:
                if response.status == 404:
                    raise MediaNotFoundError(f"Episode {episode_id} has no playable audio")
                if response.status == 502:
                    raise ResourceTemporarilyUnavailable(
                        f"The feed host for episode {episode_id} did not answer the Mac"
                    )
                if response.status == 401:
                    raise LoginFailed("overcastctl rejected the bearer token")
                response.raise_for_status()
                return response.content_type, response.content_length
        except (TimeoutError, aiohttp.ClientError) as err:
            raise ResourceTemporarilyUnavailable(
                "The Mac running overcastctl is unreachable"
            ) from err

    async def stream_audio(self, episode_id: str, byte_offset: int = 0) -> AsyncGenerator[bytes]:
        """
        Stream a downloaded episode's file, optionally from a byte offset.

        :param episode_id: The Overcast episode id.
        :param byte_offset: Where to start reading.
        """
        headers = dict(self._auth)
        if byte_offset > 0:
            headers["Range"] = f"bytes={byte_offset}-"
        async with self._session.get(
            self._audio_url(episode_id),
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=60),
        ) as response:
            if response.status == 404:
                raise MediaNotFoundError(f"Episode {episode_id} has no playable audio")
            response.raise_for_status()
            async for chunk in response.content.iter_chunked(64 * 1024):
                yield chunk

    def _audio_url(self, episode_id: str) -> str:
        if not episode_id.isdigit():
            raise MediaNotFoundError(f"Invalid Overcast episode id {episode_id}")
        return f"{self._base_url}/audio/{episode_id}"
