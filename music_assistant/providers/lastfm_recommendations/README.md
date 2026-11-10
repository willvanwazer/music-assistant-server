# Listening rankings from stats.fm

In **Settings → Metadata providers → Last.fm Recommendations**, enable personalized
recommendations and enter a public stats.fm username or profile URL. Choose the past
four weeks, past six months (default), or all time.

The provider displays **Your Top Artists** in stats.fm rank order and seeds the existing
**Discover Similar Artists** and **Discover Similar Tracks** rows from the top five
artists and top twenty distinct tracks in that period. Rankings follow the profile's
stats.fm sorting preference, including listening time or stream count. Artist cards
resolve to the connected music catalogs so they can be browsed and played.

Last.fm supplies similarity matches; stats.fm supplies the personal listening history.
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
