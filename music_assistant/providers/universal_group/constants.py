"""Universal Group Player constants."""

from __future__ import annotations

from typing import Final

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, ContentType, PlayerFeature
from music_assistant_models.media_items import AudioFormat

from music_assistant.helpers.ffmpeg import DEFAULT_MP3_BIT_RATE

UGP_PREFIX: Final[str] = "ugp_"

# Features the group has no hardware of its own for: the player manager fans these
# commands out to the members. They are advertised only when a member can actually
# carry them out, which is the same condition under which the group state for them
# (group_volume / group_volume_muted) resolves to a value.
EXTRA_FEATURES_FROM_MEMBERS: Final[set[PlayerFeature]] = {
    PlayerFeature.VOLUME_SET,
    PlayerFeature.VOLUME_MUTE,
}

# Grace period (seconds) before a universal group releases its members after
# the active stream naturally ends (playback_state transitions to IDLE without
# an explicit stop). Matches the SyncGroup grace window.
IDLE_GRACE_SECONDS: Final[float] = 10.0

# Static output format options for the UGP stream. UGP is a heterogeneous-protocol
# compatibility group, not a hi-res delivery path; every member receives the same
# encoded stream, so the format is a single deliberate choice instead of a per-member
# negotiation.
CONF_UGP_OUTPUT_FORMAT: Final[str] = "ugp_output_format"

UGP_OUTPUT_MP3: Final[str] = "mp3"
UGP_OUTPUT_FLAC_44100_16: Final[str] = "flac_44100_16"
UGP_OUTPUT_FLAC_48000_24: Final[str] = "flac_48000_24"

# Map each option to (audio format the server will encode/serve, URL extension).
# The audio format also drives the internal PCM pivot used by UGPStream so the
# multiplexer runs at exactly the rate the encoder needs.
UGP_OUTPUT_FORMATS: Final[dict[str, tuple[AudioFormat, str]]] = {
    UGP_OUTPUT_MP3: (
        AudioFormat(
            content_type=ContentType.MP3,
            codec_type=ContentType.MP3,
            sample_rate=44100,
            bit_depth=16,
            bit_rate=DEFAULT_MP3_BIT_RATE,
        ),
        "mp3",
    ),
    UGP_OUTPUT_FLAC_44100_16: (
        AudioFormat(content_type=ContentType.FLAC, sample_rate=44100, bit_depth=16),
        "flac",
    ),
    UGP_OUTPUT_FLAC_48000_24: (
        AudioFormat(content_type=ContentType.FLAC, sample_rate=48000, bit_depth=24),
        "flac",
    ),
}


# How long the group stream holds its start for the members it was sent to. Speakers
# take anything from a fraction of a second to a few seconds to open the stream, and
# only the ones that are there at the start receive it from the beginning. A member
# that takes longer is placed on the running stream instead.
START_GATE_TIMEOUT: Final[float] = 4.0

# The same wait for a stream that was not told which members to expect.
START_GATE_UNKNOWN_MEMBERS: Final[float] = 0.25

# Audio the group stream holds before it starts, in seconds. Speakers begin to play
# once their own buffer has filled; handing every one of them this much at once makes
# that moment the same whether the source delivers ahead of playback or just in time.
CONF_UGP_START_CUSHION: Final[str] = "ugp_start_cushion"
DEFAULT_START_CUSHION: Final[int] = 2

# Longest a start waits, beyond the cushion itself, for a source that is slow to fill it.
START_CUSHION_GRACE: Final[float] = 2.0

# Per-member playback delay, in milliseconds, keyed by the member's player id.
CONF_UGP_MEMBER_DELAY_PREFIX: Final[str] = "ugp_member_delay_"
MAX_MEMBER_DELAY_MS: Final[int] = 2000

# A member that has not even fetched the audio from this long ago can not be playing
# in time anymore, so its feed moves up to the present instead of staying behind.
RESYNC_THRESHOLD: Final[float] = 3.0

# Audio the group stream keeps, in seconds, to place members that connect late,
# reconnect or fall behind. It spans what the stream may run ahead of playback (the
# opening burst of the pacing plus the start gate), the longest member delay and the
# resync threshold.
BACKLOG_SECONDS: Final[float] = 20.0

# First look at how far each member plays behind the stream, in seconds after a start.
# The members have settled on the stream by then.
TIMING_REPORT_DELAY: Final[float] = 12.0

# Change of the spread between the members, in seconds, that is reported again.
TIMING_REPORT_CHANGE: Final[float] = 0.5


CONFIG_ENTRY_UGP_NOTE = ConfigEntry(
    key="ugp_note",
    type=ConfigEntryType.ALERT,
    required=False,
)

CONF_ENTRY_UGP_OUTPUT_FORMAT = ConfigEntry(
    key=CONF_UGP_OUTPUT_FORMAT,
    type=ConfigEntryType.STRING,
    options=[
        ConfigValueOption(UGP_OUTPUT_MP3),
        ConfigValueOption(UGP_OUTPUT_FLAC_44100_16),
        ConfigValueOption(UGP_OUTPUT_FLAC_48000_24),
    ],
    default_value=UGP_OUTPUT_MP3,
    category="generic",
    requires_reload=True,
)


CONF_ENTRY_UGP_START_CUSHION = ConfigEntry(
    key=CONF_UGP_START_CUSHION,
    type=ConfigEntryType.INTEGER,
    range=(0, 6),
    default_value=DEFAULT_START_CUSHION,
    category="generic",
    advanced=True,
)


def member_delay_key(player_id: str) -> str:
    """
    Return the config key that holds a member's playback delay.

    :param player_id: The member the delay applies to.
    """
    return f"{CONF_UGP_MEMBER_DELAY_PREFIX}{player_id}"


def resolve_ugp_output_format(option: str) -> tuple[AudioFormat, str]:
    """Return the (output AudioFormat, URL extension) for a configured UGP option."""
    return UGP_OUTPUT_FORMATS.get(option, UGP_OUTPUT_FORMATS[UGP_OUTPUT_MP3])
