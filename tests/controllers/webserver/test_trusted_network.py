"""Trusted-LAN boundaries and token-free HTTP/WebSocket administrator sessions."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.api import CommandMessage
from music_assistant_models.auth import UserRole

from music_assistant.controllers.webserver.auth import AuthenticationManager
from music_assistant.controllers.webserver.controller import WebserverController
from music_assistant.controllers.webserver.helpers.auth_middleware import get_authenticated_user
from music_assistant.controllers.webserver.helpers.trusted_network import TrustedNetworkPolicy
from music_assistant.controllers.webserver.websocket_client import WebsocketClientHandler

if TYPE_CHECKING:
    from aiohttp import web

    from music_assistant.mass import MusicAssistant


@pytest.fixture
def _lan_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable the test LAN and one explicitly trusted edge proxy."""
    monkeypatch.delenv("MASS_LAN_ADMIN_USER_ID", raising=False)
    monkeypatch.setenv("MASS_LAN_ADMIN_NETWORKS", "10.1.0.0/24")
    monkeypatch.setenv("MASS_LAN_ADMIN_PROXIES", "172.31.97.2")
    monkeypatch.setenv("MASS_LAN_ADMIN_HOSTS", "10.1.0.10:8097,10.1.0.10:8095")


@pytest.fixture
async def auth_manager(
    mass_minimal: MusicAssistant, _lan_env: None
) -> AsyncGenerator[AuthenticationManager]:
    """Initialize a real isolated authentication database with LAN access enabled."""
    webserver = WebserverController(mass_minimal)
    mass_minimal.webserver = webserver
    webserver.config = await mass_minimal.config.get_core_config("webserver")
    mass_minimal.music = MagicMock()
    mass_minimal.music.database.execute = AsyncMock()
    mass_minimal.music.database.commit = AsyncMock()
    await webserver.auth.setup()
    try:
        yield webserver.auth
    finally:
        await webserver.auth.close()


@pytest.mark.parametrize(
    ("peer", "headers", "allowed"),
    [
        ("10.1.0.42", {}, True),
        ("::ffff:10.1.0.42", {}, True),
        ("10.1.1.42", {}, False),
        ("100.64.0.1", {}, False),
        ("127.0.0.1", {}, False),
        ("198.51.100.1", {"X-Forwarded-For": "10.1.0.42"}, False),
        ("10.1.0.42", {"X-Forwarded-For": "10.1.0.43"}, False),
        ("10.1.0.42", {"Forwarded": "for=10.1.0.43"}, False),
        ("172.31.97.2", {}, False),
        ("172.31.97.2", {"X-Forwarded-For": "10.1.0.42"}, True),
        ("172.31.97.3", {"X-Forwarded-For": "10.1.0.42"}, False),
        ("172.31.97.2", {"X-Forwarded-For": "198.51.100.1"}, False),
        ("172.31.97.2", {"X-Forwarded-For": "10.1.0.42, 198.51.100.1"}, False),
        ("172.31.97.2", {"X-Forwarded-For": "garbage"}, False),
        ("10.1.0.42", {"Host": "attacker.example"}, False),
        ("10.1.0.42", {"Origin": "https://attacker.example"}, False),
        ("10.1.0.42", {"Origin": "null"}, False),
        ("10.1.0.42", {"Origin": "http://10.1.0.10:8097"}, True),
        ("10.1.0.42", {"Origin": "http://10.1.0.10:8095"}, False),
        ("10.1.0.42", {"Sec-Fetch-Site": "cross-site"}, False),
    ],
)
@pytest.mark.usefixtures("_lan_env")
def test_request_boundary(peer: str, headers: dict[str, str], allowed: bool) -> None:
    """Only the configured subnet, edge and same-origin browser requests qualify."""
    assert TrustedNetworkPolicy().allows(_request(peer, headers)) is allowed


def test_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stock deployment never grants network-based administration."""
    monkeypatch.delenv("MASS_LAN_ADMIN_NETWORKS", raising=False)
    assert not TrustedNetworkPolicy().allows(_request("10.1.0.42"))


@pytest.mark.usefixtures("_lan_env")
def test_missing_hosts_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enabling the feature requires a host allowlist against DNS rebinding."""
    monkeypatch.delenv("MASS_LAN_ADMIN_HOSTS")
    with pytest.raises(ValueError, match="HOSTS"):
        TrustedNetworkPolicy()


async def test_http_admin_without_token(auth_manager: AuthenticationManager) -> None:
    """A LAN request gets a persisted admin identity without issuing any bearer token."""
    request = _request("10.1.0.42")
    request.app["mass"] = auth_manager.mass
    user = await get_authenticated_user(request)
    assert user is not None
    assert user.role == UserRole.ADMIN
    assert auth_manager.has_users
    assert await auth_manager.database.get_rows("auth_tokens") == []
    assert await auth_manager.authenticate_trusted_network(_request("10.1.1.42")) is None


async def test_explicit_token_is_not_elevated(auth_manager: AuthenticationManager) -> None:
    """Invalid explicit credentials cannot silently become a LAN admin session."""
    request = _request("10.1.0.42", {"Authorization": "Bearer invalid"})
    request.app["mass"] = auth_manager.mass
    assert await get_authenticated_user(request) is None


@pytest.mark.parametrize("peer", ["10.1.0.21", "198.51.100.1"])
async def test_migrated_ha_token_stays_lan_only(
    auth_manager: AuthenticationManager, peer: str
) -> None:
    """A migrated integration keeps its service role and still cannot log in off-LAN."""
    token = await auth_manager.get_homeassistant_system_user_token()
    request = _request(peer, {"Authorization": f"Bearer {token}"})
    request.app["mass"] = auth_manager.mass
    user = await get_authenticated_user(request)
    if peer == "10.1.0.21":
        assert user is not None
        assert user.role == UserRole.SERVICE
    else:
        assert user is None


async def test_disabled_user_is_not_reenabled(auth_manager: AuthenticationManager) -> None:
    """Disabling the LAN identity prevents subsequent network logins."""
    user = await auth_manager.authenticate_trusted_network(_request("10.1.0.42"))
    assert user is not None
    await auth_manager.database.update("users", {"user_id": user.user_id}, {"enabled": False})
    assert await auth_manager.authenticate_trusted_network(_request("10.1.0.42")) is None


async def test_configured_identity_preserves_existing_account(
    auth_manager: AuthenticationManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Select an existing account on restart without replacing its preferences or tokens."""
    user = await auth_manager.create_user(username="will", role=UserRole.ADMIN, display_name="Will")
    await auth_manager.update_user_preferences(user, {"test_preference": "preserved"})
    monkeypatch.setenv("MASS_LAN_ADMIN_USER_ID", user.user_id)
    await auth_manager.close()
    auth_manager.trusted_network = TrustedNetworkPolicy()
    await auth_manager.setup()
    resolved = await auth_manager.authenticate_trusted_network(_request("10.1.0.42"))
    assert resolved is not None
    assert resolved.user_id == user.user_id
    assert resolved.display_name == "Will"
    assert resolved.preferences == {"test_preference": "preserved"}
    assert len(await auth_manager.database.get_rows("users")) == 2
    assert await auth_manager.database.get_rows("auth_tokens") == []
    await auth_manager.database.update("users", {"user_id": user.user_id}, {"enabled": False})
    assert await auth_manager.authenticate_trusted_network(_request("10.1.0.42")) is None


@pytest.mark.parametrize("identity", ["missing", "non_admin"])
async def test_invalid_configured_identity_has_no_fallback(
    auth_manager: AuthenticationManager, monkeypatch: pytest.MonkeyPatch, identity: str
) -> None:
    """A missing or non-admin target cannot fall back to the generated administrator."""
    user_id = "missing-user"
    if identity == "non_admin":
        user = await auth_manager.create_user(username="listener", role=UserRole.USER)
        user_id = user.user_id
    monkeypatch.setenv("MASS_LAN_ADMIN_USER_ID", user_id)
    await auth_manager.close()
    auth_manager.trusted_network = TrustedNetworkPolicy()
    await auth_manager.setup()
    assert await auth_manager.authenticate_trusted_network(_request("10.1.0.42")) is None


@pytest.mark.parametrize(
    ("peer", "headers", "allowed"),
    [
        ("172.31.97.2", {"X-Forwarded-For": "100.100.20.30"}, True),
        ("172.31.97.2", {"X-Forwarded-For": "fd7a:115c:a1e0::1"}, True),
        ("172.31.97.2", {"X-Forwarded-For": "198.51.100.1"}, False),
        ("172.31.97.3", {"X-Forwarded-For": "100.100.20.30"}, False),
        ("172.31.97.2", {"X-Forwarded-For": "198.51.100.1, 100.100.20.30"}, False),
        (
            "172.31.97.2",
            {"X-Forwarded-For": "100.100.20.30", "Origin": "https://evil.example"},
            False,
        ),
    ],
)
@pytest.mark.usefixtures("_lan_env")
def test_opt_in_tailnet_proxy(
    monkeypatch: pytest.MonkeyPatch, peer: str, headers: dict[str, str], allowed: bool
) -> None:
    """Only the named proxy can attest a Tailnet client for the HTTPS hostname."""
    host = "music-assistant.example.ts.net"
    monkeypatch.setenv("MASS_LAN_ADMIN_NETWORKS", "10.1.0.0/24,100.64.0.0/10,fd7a:115c:a1e0::/48")
    monkeypatch.setenv("MASS_LAN_ADMIN_HOSTS", host)
    request = _request(peer, {"Host": host, "Origin": f"https://{host}", **headers})
    assert TrustedNetworkPolicy().allows(request) is allowed


@pytest.mark.parametrize("peer", ["10.1.0.42", "198.51.100.1"])
async def test_websocket_auth_is_bound_to_peer(
    auth_manager: AuthenticationManager, peer: str
) -> None:
    """The same token-free WebSocket command succeeds only on the trusted LAN."""
    request = _request(peer)
    request.app["mass"] = auth_manager.mass
    handler = WebsocketClientHandler(auth_manager.webserver, request)
    with (
        patch.object(handler, "_send_message", new_callable=AsyncMock),
        patch.object(handler, "_subscribe_to_events"),
    ):
        await handler._handle_auth_command(
            CommandMessage(message_id="1", command="auth", args={"trusted_network": True})
        )
    if peer == "10.1.0.42":
        assert handler._authenticated_user is not None
        assert handler._authenticated_user.role == UserRole.ADMIN
        assert handler._current_token is None

    else:
        assert handler._authenticated_user is None


def _request(peer: str, headers: dict[str, str] | None = None) -> web.Request:
    transport = MagicMock()
    transport.get_extra_info.side_effect = lambda name: (
        (peer, 12345) if name == "peername" else None
    )
    return make_mocked_request(
        "GET", "/ws", headers={"Host": "10.1.0.10:8097", **(headers or {})}, transport=transport
    )
