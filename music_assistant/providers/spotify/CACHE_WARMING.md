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

## Read-through and stale data

Every cached call a page makes serves an expired entry at once and refreshes it in the
background (stale-while-revalidate), so no page waits on Spotify once its entries
exist. That includes podcasts, podcast episodes, audiobooks and their chapters. Playback
resume positions are the exception: an audiobook's resume position reads its chapters
with `refresh_cache`, so it never comes from a stale entry.

## Throttling

The warmer (built on `CacheWarmer` in `controllers/cache/warmer.py`) checks each entry
with `cache_status` without calling Spotify. A run first fills the pages with a missing
entry, the ones a visit would wait on, then refreshes pages with only expired entries,
so the next visit shows current data. A warm, fresh cache costs a run nothing but cache
reads.

Its requests go through the provider's shared throttler, one page at a time with a
4 second pause between pages, so browsing is never queued behind a burst. A run fetches
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
