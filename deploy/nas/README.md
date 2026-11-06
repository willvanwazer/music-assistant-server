# NAS runtime

`build.sh IMAGE` builds the fork wheel over the pinned official Music Assistant 2.10.9
image. Official bundled integrations stay in their original runtime; credentials are
never extracted into this repository or a separate file. Update the pinned release,
requirements and frontend compatibility range together when upgrading upstream.

`MASS_FRONTEND_DIR` optionally selects the independently built web frontend directory.
The NAS mounts its checked frontend release at `/frontend/current`. Music Assistant
serves it directly on its default API/UI port 8095; streaming stays on 8097.

LAN administration is disabled unless `MASS_LAN_ADMIN_NETWORKS` contains explicit CIDRs.
`MASS_LAN_ADMIN_HOSTS` must list exact allowed host:port values. An optional comma-separated
`MASS_LAN_ADMIN_PROXIES` lists individual edge proxy IPs; each must overwrite
X-Forwarded-For with its actual client address. Proxy chains are deliberately rejected.
The NAS production server uses direct host networking and no trusted proxies.

An enabled installation creates a persistent Home LAN administrator with no password.
HTTP requests and explicit `auth` WebSocket commands with `trusted_network: true` are
checked against the socket peer, allowed host and browser origin. No bearer token is
issued or stored by this automatic login. Explicit token sessions retain their own role;
non-LAN requests continue to require authentication. Disabling the Home LAN user prevents
new LAN sessions; removing the environment settings and restarting disables the feature.
