# Spotify favorites sync

Enable **Sync favorites both ways** under the Spotify provider's sync options.
This opt-in feature uses the existing account's authenticated, throttled connection.
It runs on the Music Assistant server every 15 minutes and after startup.

The first successful run merges Spotify liked songs, saved albums, and followed
artists with Music Assistant hearts. Subsequent runs propagate additions and
removals from either side. Library membership is not used as an MA favorite.
Only exact mappings to this Spotify instance participate; unmatched local music
is left alone. Playlists and playlist folders are outside this feature.

The reconciler records per-item convergence in
`<storage>/spotify_favorites_<provider-instance>.json`. This contains IDs and
favorite state, never credentials. The checkpoint survives upgrades and restarts;
do not delete it to fix a transient failure, because that resets to a fresh merge.
A changed Spotify account or malformed checkpoint stops the job.

Complete remote snapshots are collected before any writes. Truncated responses,
authentication errors, and overlapping native library syncs stop the run and
retry at the next interval. More than 50 removals in one run requires review.
Unavailable imports, concurrent heart edits, and conflicting mappings between
multiple Spotify releases of one MA item are deferred. Remote changes only save
or unsave Spotify items; the reconciler never deletes MA library records.

Runs appear in Background tasks as **Sync Spotify favorites (every 15 minutes)**.
An authenticated library administrator can trigger a run with
`spotify/<provider-instance>/sync_favorites`. Disabling the setting cancels its
timer and running work. The last successful counts are recorded in the checkpoint.

Spotify's Web API does not expose playlist folders, so the Music Assistant folder
hierarchy cannot be mirrored through this connection:
https://developer.spotify.com/documentation/web-api/concepts/playlists
