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
