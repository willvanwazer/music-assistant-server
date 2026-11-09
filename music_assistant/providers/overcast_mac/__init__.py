"""Overcast (Mac) podcast provider for Music Assistant, backed by overcastctl."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .constants import SUPPORTED_FEATURES
from .provider import OvercastMacProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType

__all__ = ["OvercastMacProvider", "setup"]


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return OvercastMacProvider(mass, manifest, config, SUPPORTED_FEATURES)
