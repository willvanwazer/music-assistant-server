# Spotify cache warming

Spotify pages are read through the cache (`@use_cache` in `provider.py`), so the first
visit to a page waits on Spotify and later visits do not. `cache_warmer.py` makes that
first fetch in the background, on the server, for the pages people are likely to open,
whichever app they use.

It runs 5 minutes after the provider loads and then every 6 hours, visible in Background
tasks as **Keep Spotify pages cached (every 6 hours)**. An authenticated library
administrator can start a run with `spotify/<provider-instance>/warm_cache`.

## What is warmed

Items come from the Discover rows first (every row's items that have a mapping to this
Spotify instance), then the library's Spotify mappings: artists, albums, playlists,
podcasts, audiobooks and tracks, in that order. For each item it fills the same cache
entries, under the same keys, that opening its page would:

| Page | Cached calls |
| --- | --- |
| Artist | `get_artist`, `get_artist_albums`, `get_artist_toptracks` |
| Album | `_get_album_data` (read by `get_album`), `get_album_tracks` |
| Playlist | `get_playlist`, the first page of `get_playlist_tracks` |
| Podcast | `get_podcast`, `_get_podcast_episodes_data` |
| Audiobook | `get_audiobook` (with its chapters), where the account supports audiobooks |
| Track | `get_track`, `get_similar_tracks` at the track page's limit of 25 |

Reading the Discover rows also refreshes those rows' own caches (Made for you, recently
played, and the other providers' rows) the way opening Discover does.

## Throttling

The warmer only calls Spotify for an entry that a visit would have to fetch:
`needs_cache_fill` in `controllers/cache/helpers.py` checks the cache without calling the
method. An entry that is missing needs filling. An expired entry needs filling only for a
method without stale-while-revalidate (podcast episodes, for one); the other methods
serve an expired entry immediately and refresh it when it is next opened, so the warmer
leaves them alone. A warm cache costs a run nothing but cache reads.

Its requests go through the provider's shared throttler, one page at a time with a
4 second pause between pages, so browsing is never queued behind a burst. A run fills
at most 200 pages; the rest wait for the next run, so a cold cache fills over a few
runs. It defers to a running Spotify library sync, stops when Spotify asks it to back
off, and stops after 5 consecutive failed pages.

## Persistence

The cache is the SQLite `cache.db` in the server's cache directory, which survives
restarts and upgrades; the warmer keeps no state of its own and picks up from the cache
after a restart. Removing a provider drops only that provider's cache entries
(`MusicController.cleanup_provider`); it used to empty the whole cache. A library
schema migration, a library reset and the cache's **Clear cache** action still empty
it, and the warmer refills it on its next runs.
