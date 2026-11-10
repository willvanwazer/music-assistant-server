"""Constants for the Overcast (Mac) provider."""

from __future__ import annotations

from music_assistant_models.enums import ProviderFeature

CONF_SERVER_URL = "server_url"
CONF_TOKEN = "token"

MCP_PROTOCOL_VERSION = "2025-11-25"
PAGE_SIZE = 200
# overcastctl serializes every operation across its listeners and answers "busy"
# rather than queueing, so calls are retried with a short backoff
BUSY_RETRIES = 6
BUSY_BACKOFF_S = 0.4
REQUEST_TIMEOUT_S = 30
# a progress write closes Overcast, writes, reopens it and waits for its sync
WRITE_TIMEOUT_S = 120
# how long after playback stops (or the last tick, if stop is never reported) the
# position is written; resuming within this window cancels the write
SYNC_DELAY_S = 20
SYNC_STALE_TICK_S = 600
# retry while Overcast is playing on the Mac, busy or unreachable
SYNC_RETRY_S = 300
SYNC_MAX_ATTEMPTS = 24
# MA reports fully_played on skip/stop too, so require the real position near the end
FULLY_PLAYED_FRACTION = 0.95
FULLY_PLAYED_REMAINING_S = 60
# podcast list and artwork change rarely; episode state is read fresh every time
PODCASTS_CACHE_S = 600

SPECIAL_FOLDERS = {
    "up_next": "Up Next",
    "in_progress": "In Progress",
    "starred": "Starred",
}

SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_PODCASTS,
    ProviderFeature.BROWSE,
}
