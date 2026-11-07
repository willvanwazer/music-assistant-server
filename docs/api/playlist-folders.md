# Playlist folders API

Playlist folders organize **library playlists for the authenticated user**. They do not
modify a provider's playlist, its tracks, or its folders. Folders are one level deep.
A library playlist belongs to at most one folder per user; no membership means **Unfiled**.
The same playlist can be organized differently by different users.

## Transport and authentication

These commands use Music Assistant's existing HTTP and WebSocket command APIs:

- HTTP: `POST /api`, `Content-Type: application/json`. Successful responses are the JSON
  result directly; commands returning no value return JSON `null`.
- WebSocket: `/ws`, after the normal authentication handshake. Send the same envelope with
  a unique `message_id`; successful replies contain `message_id` and `result`.
- Authenticate with your normal account/session or an account's long-lived bearer token.
  Trusted LAN/Tailscale automatic login uses the configured user's own folders.
- All operations derive ownership from the authenticated user. There is **no `user_id`
  argument** and these commands do not opt into admin impersonation. Even admins cannot
  access someone else's folders by supplying their ID.
- `list` and `items` require `library.read`. Writes require `library.write`.
  A scoped token must include the corresponding scope; ordinary guest accounts cannot write.

Example (set `MASS_URL` and `MASS_TOKEN` in your environment):

```sh
curl --fail-with-body "$MASS_URL/api" \
  -H "Authorization: Bearer $MASS_TOKEN" \
  -H 'Content-Type: application/json' \
  --data '{"command":"music/playlist_folders/create","args":{"name":"Dinner"}}'
```

Example response:

```json
{"folder_id":"058f4d202bc941ae9e45872648f5176c","name":"Dinner","pinned":false}
```

Folder IDs are opaque strings. Playlist IDs are **string-form library IDs**, such as `"42"`,
not Spotify/Apple Music IDs or playlist URIs. Obtain them from `music/playlists/library_items`
or the `items` command below. Add external playlists to the MA library before organizing them.

## Commands

All names below are prefixed with `music/playlist_folders/`.

| Command | Arguments | Result |
| --- | --- | --- |
| `list` | None | Array of folder summaries, sorted by name |
| `create` | `name: string` | `{folder_id, name, pinned}` |
| `update` | `folder_id: string`, optional `name: string`, optional `pinned: boolean` | `{folder_id, name, pinned}` |
| `delete` | `folder_id: string` | `null` |
| `move` | `playlist_ids: string[]`, optional `folder_id: string \| null` | `null` |
| `items` | Optional filters and pagination below | Array of normal MA playlist summaries |

### List folders

```json
{"message_id":"folders-1","command":"music/playlist_folders/list","args":{}}
```

An empty account returns `[]`. Each returned folder has:

```json
{
  "folder_id":"058f4d202bc941ae9e45872648f5176c",
  "name":"Dinner",
  "pinned":true,
  "playlist_count":2,
  "playlist_ids":["42","57"],
  "preview":[]
}
```

`preview` contains up to four normal MA playlist summaries (omitted in this illustrative
example), suitable for rendering a cover collage. Counts, IDs, previews and item listings
respect the current user's provider filter and only include playlists still in a provider's
library. Membership is retained if a source is temporarily hidden, so it can reappear when
access returns. Empty folders remain listed. `playlist_ids` ordering is unspecified.

### Create, rename and pin

Names are trimmed, must contain 1–100 characters, and cannot contain control characters.
Names are unique per user, case-insensitively. Up to 100 folders may be created per user.
Folders start unpinned. `update` preserves any field omitted from the request.

```json
{"command":"music/playlist_folders/update","args":{"folder_id":"058f4d202bc941ae9e45872648f5176c","name":"Dinner parties","pinned":true}}
```

Pinning is stored on the server and appears in the sidebar on all the user's clients.

### Move playlists

```json
{"command":"music/playlist_folders/move","args":{"playlist_ids":["42","57"],"folder_id":"058f4d202bc941ae9e45872648f5176c"}}
```

Provide 1–500 IDs. Duplicate IDs are deduplicated. Every playlist must exist in the MA library
and be visible to the current user. The destination must belong to that user. Validation
happens before writing: **if any ID fails, none of the playlists move**. The batch is written
atomically. Moving to the existing destination is safe to repeat. Concurrent moves are
serialized; the last completed move determines a playlist's folder.

To return playlists to Unfiled, omit `folder_id` or pass `null`:

```json
{"command":"music/playlist_folders/move","args":{"playlist_ids":["42"],"folder_id":null}}
```

### List folder contents, Unfiled or all playlists

```json
{"command":"music/playlist_folders/items","args":{"folder_id":"058f4d202bc941ae9e45872648f5176c","limit":50,"offset":0}}
```

```json
{"command":"music/playlist_folders/items","args":{"unfiled":true,"limit":50,"offset":0}}
```

With neither `folder_id` nor `unfiled`, the command lists all visible library playlists.
`folder_id` and `unfiled: true` are mutually exclusive. Empty results return `[]`.
Folder and access filters are applied **before** pagination.

| Argument | Default | Meaning |
| --- | --- | --- |
| `folder_id` | `null` | Personal folder to list |
| `unfiled` | `false` | Only playlists with no folder for this user |
| `limit` | `100` | Page size, 1–500 |
| `offset` | `0` | Number of matching results to skip; nonnegative |
| `search` | `null` | Normal library playlist name search |
| `favorite` | `null` | Optional favorite-state filter |
| `order_by` | `"sort_name"` | Standard playlist library sort, e.g. `name`, `name_desc`, `last_played` |
| `provider` | `null` | Provider instance ID or array of IDs, constrained by account access |
| `genre` | `null` | Genre ID or array of IDs |

Continue requesting pages until fewer than `limit` items are returned. Results are the same
playlist-summary shape used by `music/playlists/library_items`, including `item_id`, `uri`,
`name`, `provider`, `media_type`, artwork metadata and provider mappings. Use ordinary playlist
and queue APIs for playback; a folder does not itself represent a playable media item.

### Delete a folder

```json
{"command":"music/playlist_folders/delete","args":{"folder_id":"058f4d202bc941ae9e45872648f5176c"}}
```

Deletes the folder and its memberships in one database operation. The playlists and their
tracks remain intact and appear under Unfiled. Any sidebar pin disappears with the folder.
Deleting a playlist from the MA library also cleans up its folder memberships.

## Errors

HTTP errors have a plain-text body. WebSocket errors use the standard MA error envelope,
including `message_id`, `error_code` and `details` (plus localization fields when available).

| Condition | HTTP | WebSocket error code |
| --- | --- | --- |
| Missing/invalid authentication | `401` | Standard MA authentication error |
| Insufficient scope or missing authenticated user | `403` | `22` (InsufficientPermissions) |
| Invalid name, duplicate name, folder limit, invalid move batch, invalid pagination | `400` | `3` (InvalidDataError) |
| Missing/foreign folder, or missing/inaccessible playlist in a move | `404` | `2` (MediaNotFoundError) |

Foreign folder IDs produce the same not-found response as nonexistent IDs. A failed move
never changes earlier members of the requested batch. Authentication failures and malformed
transport envelopes follow the server's existing command API behavior.

## Change notifications

After each successful mutation the server emits a standard `media_item_updated` event with
`object_id: "playlist_folders://<authenticated-user-id>"`. It contains **no folder metadata**.
Clients belonging to that user should refetch `list` and their active `items` page. On reconnect
or focus, refetch to recover missed events. Normal playlist deletion/change events may also
require refreshing folder counts and previews.

## Storage and compatibility

This fork stores folders in library database schema **59** (`playlist_folders` and
`playlist_folder_members`). The migration is additive and preserves playlists and provider
configuration. The API is available over both HTTP and WebSocket; it does not change the
existing playlist API or require a provider to support native playlist folders. Folder
organization is included in the normal NAS Music Assistant data backup.
