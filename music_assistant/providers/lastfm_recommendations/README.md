# Listening rankings from stats.fm

In **Settings → Metadata providers → Last.fm Recommendations**, enable personalized
recommendations and enter a public stats.fm username or profile URL. Choose the past
four weeks, past six months (default), or all time.

The provider displays **Your Top Artists** in stats.fm rank order and seeds the existing
**Discover Similar Artists** and **Discover Similar Tracks** rows from the top five
artists and top twenty distinct tracks in that period. Rankings follow the profile's
stats.fm sorting preference, including listening time or stream count. Artist cards
resolve to the connected music catalogs so they can be browsed and played.

Last.fm supplies similar artists. Track discovery first asks the connected music
catalog for recommendations, with Last.fm as fallback. This supports recent releases
that Last.fm has not yet matched. Results are interleaved across the ranked seed tracks,
with duplicates, the seeds themselves, and owned songs removed. Stats.fm supplies the
personal listening history.
The row subtitles identify stats.fm and the selected period. A blank stats.fm profile
keeps the existing Music Assistant recent-play behavior.

The integration uses the public read-only endpoints documented in the
[stats.fm API](https://beta-api.stats.fm/swagger):
`/api/v1/users/{username}/top/artists` and `/top/tracks`, with a bounded `limit=20` and
`range=weeks`, `months`, or `lifetime`. It needs no login token and never uploads plays.
Successful rankings are cached by provider, profile, period, and media type for six
hours. The existing scheduled refresh and **Refresh Recommendations** action rebuild
the rows. Failed or private-profile responses are not cached as empty listening
history and do not silently switch the recommendation source. Existing rows stay
visible while a replacement is built; on failure the previous generation is retained.

# Artist page cache warming

Artist pages show Last.fm's top tracks and similar artists for the artist. Both are
read through the cache and serve an expired entry at once while it refreshes, but the
first visit used to wait on Last.fm and on resolving every item to a playable catalog
item. `cache_warmer.py` (built on `CacheWarmer` in `controllers/cache/warmer.py`) fills
them in the background, as **Keep Last.fm artist panels cached (every 6 hours)**,
starting 5 minutes after the provider loads. An administrator can start a run with
`lastfm_recommendations/<provider-instance>/warm_cache`.

It covers the artists on every Discover row first, then every library artist, favorites
first. Each artist is looked up the way its page does (the library's copy when there is
one), so the entries carry the same keys: `_get_artist_toptracks` at the default limit
of 25 and `_get_similar_artists` at the apps' limit of 25. Missing entries are filled
first, then expired ones refreshed.

Resolving an item searches the streaming providers through their throttles, so a run
fetches at most 100 artists, one at a time with a 4 second pause. It waits while a
library sync runs, while a music provider is loading, or while no streaming provider
can be searched, since a panel resolved then would be cached without those items. It
stops when Last.fm asks it to back off. Last.fm's similar tracks are not warmed: track
pages ask the track's own provider first and fall back to Last.fm only when it has none.
