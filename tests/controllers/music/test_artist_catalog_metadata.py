"""Metadata panels also work for streaming artists outside the library."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ProviderFeature, ProviderType
from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.media_items import Artist

from music_assistant.controllers.music.media.artists import ArtistsController


@pytest.mark.parametrize(
    ("method", "provider_method", "feature"),
    [
        ("top_tracks", "get_artist_toptracks", ProviderFeature.ARTIST_TOPTRACKS),
        ("top_albums", "get_artist_topalbums", ProviderFeature.ARTIST_TOPALBUMS),
        ("similar_artists", "get_similar_artists", ProviderFeature.SIMILAR_ARTISTS),
    ],
)
async def test_catalog_artist_uses_selected_metadata_source(
    method: str, provider_method: str, feature: ProviderFeature
) -> None:
    """Resolve the artist once and keep the metadata provider's cached result."""
    controller = ArtistsController.__new__(ArtistsController)
    controller.mass = MagicMock()
    provider = MagicMock(instance_id="lastfm", type=ProviderType.METADATA, available=True)
    provider.supported_features = {feature}
    fetch = AsyncMock(return_value=[])
    setattr(provider, provider_method, fetch)
    controller.mass.get_provider.return_value = provider
    artist = Artist(
        item_id="spotify-id", provider="spotify", name="Artist", provider_mappings=set()
    )
    get_artist = AsyncMock(return_value=artist)
    allowed = MagicMock(return_value=["lastfm"])
    controller.get = get_artist  # type: ignore[method-assign]
    controller._ensure_provider_filter = allowed  # type: ignore[method-assign]
    assert (
        await getattr(controller, method)("spotify-id", "spotify", provider_filter="lastfm") == []
    )
    get_artist.assert_awaited_once_with("spotify-id", "spotify")
    if method == "similar_artists":
        fetch.assert_awaited_once_with(artist, limit=25)
    else:
        fetch.assert_awaited_once_with(artist)
    allowed.return_value = []
    with pytest.raises(MusicAssistantError, match="does not match"):
        await getattr(controller, method)("spotify-id", "spotify", provider_filter="lastfm")
    assert fetch.await_count == 1
