"""Opt-in, request-bound administrator access for explicitly trusted networks."""

from __future__ import annotations

import os
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from aiohttp import web


class TrustedNetworkPolicy:
    """Validate socket peers, proxy client addresses and browser origins."""

    def __init__(self) -> None:
        """Read operator-controlled settings once when the server starts."""
        self.networks = tuple(
            ip_network(value.strip())
            for value in os.environ.get("MASS_LAN_ADMIN_NETWORKS", "").split(",")
            if value.strip()
        )
        self.proxies = tuple(
            ip_address(value.strip())
            for value in os.environ.get("MASS_LAN_ADMIN_PROXIES", "").split(",")
            if value.strip()
        )
        self.hosts = frozenset(
            value.strip().lower()
            for value in os.environ.get("MASS_LAN_ADMIN_HOSTS", "").split(",")
            if value.strip()
        )
        if self.networks and not self.hosts:
            raise ValueError("MASS_LAN_ADMIN_HOSTS is required for LAN administration")

    @property
    def enabled(self) -> bool:
        """Return whether the operator enabled LAN administration."""
        return bool(self.networks)

    def allows(self, request: web.Request) -> bool:
        """Check the real peer and reject untrusted forwarding and cross-origin requests."""
        if not self.enabled or request.transport is None:
            return False
        try:
            peername = request.transport.get_extra_info("peername")
            if not peername:
                return False
            peer = _address(peername[0])
            forwarded = request.headers.getall("X-Forwarded-For", [])
            if peer in self.proxies:
                # Our edge must overwrite this header with its socket peer. Never accept
                # a client-supplied chain, missing header, or ambiguous duplicate headers.
                if len(forwarded) != 1 or "," in forwarded[0]:
                    return False
                peer = _address(forwarded[0])
            elif forwarded or "Forwarded" in request.headers:
                return False
            if not any(peer in network for network in self.networks):
                return False
            hosts = request.headers.getall("Host", [])
            if len(hosts) != 1 or hosts[0].lower() not in self.hosts:
                return False
            origins = request.headers.getall("Origin", [])
            if origins:
                if len(origins) != 1:
                    return False
                origin = urlsplit(origins[0])
                if (
                    origin.scheme not in ("http", "https")
                    or origin.netloc.lower() != hosts[0].lower()
                    or origin.username is not None
                    or origin.path
                    or origin.query
                    or origin.fragment
                ):
                    return False
            return request.headers.get("Sec-Fetch-Site") != "cross-site"
        except ValueError, TypeError, IndexError:
            return False


def _address(value: str) -> IPv4Address | IPv6Address:
    """Normalize IPv4-mapped peers and reject scoped addresses."""
    if "%" in value:
        raise ValueError("Scoped peer addresses are not supported")
    address = ip_address(value)
    if isinstance(address, IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped
    return address
