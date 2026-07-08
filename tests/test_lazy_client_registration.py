"""Tests for lazy client re-registration and invalid_client error handling."""

import asyncio

import pytest
from auth.slack_oauth_provider import _ALLOWED_REDIRECT_URI_RE, SlackOAuthProvider
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl
from starlette.testclient import TestClient


def _make_provider(**overrides) -> SlackOAuthProvider:
    defaults = dict(
        slack_client_id="cid",
        slack_client_secret="secret",
        slack_redirect_uri="https://example.com/oauth2callback",
        slack_scopes=["channels:history", "search:read"],
        base_url="https://example.com",
        required_scopes=["channels:history", "search:read"],
    )
    defaults.update(overrides)
    return SlackOAuthProvider(**defaults)


# -----------------------------------------------------------------------
# _is_valid_client_id
# -----------------------------------------------------------------------


class TestIsValidClientId:
    def test_normal_id(self):
        assert SlackOAuthProvider._is_valid_client_id("abc-123-XYZ") is True

    def test_uuid_style(self):
        assert (
            SlackOAuthProvider._is_valid_client_id("550e8400-e29b-41d4-a716-446655440000") is True
        )

    def test_empty_string(self):
        assert SlackOAuthProvider._is_valid_client_id("") is False

    def test_too_long(self):
        assert SlackOAuthProvider._is_valid_client_id("a" * 129) is False

    def test_max_length_ok(self):
        assert SlackOAuthProvider._is_valid_client_id("a" * 128) is True

    def test_whitespace_rejected(self):
        assert SlackOAuthProvider._is_valid_client_id("has space") is False

    def test_tab_rejected(self):
        assert SlackOAuthProvider._is_valid_client_id("has\ttab") is False

    def test_newline_rejected(self):
        assert SlackOAuthProvider._is_valid_client_id("has\nnewline") is False

    def test_non_ascii_rejected(self):
        assert SlackOAuthProvider._is_valid_client_id("héllo") is False

    def test_null_byte_rejected(self):
        assert SlackOAuthProvider._is_valid_client_id("null\x00byte") is False

    def test_special_chars_accepted(self):
        # Printable ASCII punctuation should be fine.
        assert SlackOAuthProvider._is_valid_client_id("client!@#$%^&*()") is True


# -----------------------------------------------------------------------
# Allowed redirect URI regex
# -----------------------------------------------------------------------


class TestAllowedRedirectUriRegex:
    @pytest.mark.parametrize(
        "uri",
        [
            "http://localhost",
            "http://localhost:3000",
            "http://localhost/callback",
            "http://localhost:8080/some/path",
            "https://localhost",
            "http://127.0.0.1",
            "http://127.0.0.1:9999",
            "http://127.0.0.1/callback",
            "https://127.0.0.1:443/path",
            "cursor://callback",
            "cursor://host/path?query=1",
            "vscode://extension.auth/callback",
            "CURSOR://UPPER",  # case-insensitive
        ],
    )
    def test_allowed(self, uri):
        assert _ALLOWED_REDIRECT_URI_RE.match(uri), f"{uri} should be allowed"

    @pytest.mark.parametrize(
        "uri",
        [
            "http://evil.com",
            "https://attacker.example.com/callback",
            "http://localhost.evil.com",
            "ftp://localhost",
            "jetbrains://callback",
        ],
    )
    def test_rejected(self, uri):
        assert not _ALLOWED_REDIRECT_URI_RE.match(uri), f"{uri} should be rejected"


# -----------------------------------------------------------------------
# get_client – lazy registration path
# -----------------------------------------------------------------------


class TestGetClientLazyRegistration:
    def test_already_registered_client_returned_directly(self):
        """A client that was explicitly registered is returned without re-registering."""
        provider = _make_provider()
        client = OAuthClientInformationFull(
            client_id="existing-client",
            redirect_uris=[AnyUrl("http://localhost:3000/callback")],
            token_endpoint_auth_method="none",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
        )
        asyncio.run(provider.register_client(client))

        result = asyncio.run(provider.get_client("existing-client"))
        assert result is not None
        assert result.client_id == "existing-client"
        # Should be the original client, not a newly-created one.
        assert result.redirect_uris == client.redirect_uris

    def test_unknown_valid_id_lazily_registered(self):
        """An unknown but structurally-valid client_id is lazily registered."""
        provider = _make_provider()
        # Confirm it's not already known.
        assert "brand-new-client" not in provider.clients

        result = asyncio.run(provider.get_client("brand-new-client"))
        assert result is not None
        assert result.client_id == "brand-new-client"
        assert result.token_endpoint_auth_method == "none"
        # Should now be stored.
        assert "brand-new-client" in provider.clients

    def test_lazy_registration_uses_loopback_redirect_uris(self):
        """Lazily-registered clients get only loopback redirect URIs."""
        provider = _make_provider()
        result = asyncio.run(provider.get_client("new-client"))
        assert result is not None
        uri_strings = [str(u) for u in result.redirect_uris]
        for uri in uri_strings:
            assert "localhost" in uri or "127.0.0.1" in uri

    def test_second_call_returns_same_registration(self):
        """Calling get_client twice for the same unknown ID returns the same client."""
        provider = _make_provider()
        first = asyncio.run(provider.get_client("repeat-client"))
        second = asyncio.run(provider.get_client("repeat-client"))
        assert first is not None
        assert second is not None
        assert first.client_id == second.client_id

    def test_invalid_id_returns_none(self):
        """A structurally invalid client_id returns None (not lazily registered)."""
        provider = _make_provider()
        # Empty
        assert asyncio.run(provider.get_client("")) is None
        # Too long
        assert asyncio.run(provider.get_client("x" * 200)) is None
        # Non-ASCII
        assert asyncio.run(provider.get_client("clîent")) is None
        # Whitespace
        assert asyncio.run(provider.get_client("has space")) is None


# -----------------------------------------------------------------------
# get_routes – invalid_client error code
# -----------------------------------------------------------------------


@pytest.fixture()
def provider():
    return _make_provider()


@pytest.fixture()
def app(provider):
    """Build a minimal Starlette app from the provider's routes."""
    from starlette.applications import Starlette

    return Starlette(routes=provider.get_routes())


class TestInvalidClientErrorCode:
    """Verify that genuinely invalid client_ids produce ``invalid_client``."""

    def test_invalid_client_id_returns_invalid_client_error(self, app):
        """An empty client_id triggers ``invalid_client`` in the JSON error."""
        client = TestClient(app, raise_server_exceptions=False)
        # The /authorize endpoint requires several params; supply the
        # minimum to get past Pydantic validation and hit the client-lookup
        # branch.  An empty client_id will fail _is_valid_client_id.
        resp = client.get(
            "/authorize",
            params={
                "client_id": "",
                "response_type": "code",
                "code_challenge": "challenge",
                "code_challenge_method": "S256",
            },
            follow_redirects=False,
        )
        # The framework returns 400 for unknown clients (no redirect_uri to
        # redirect to).
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"] == "invalid_client"

    def test_valid_unknown_client_does_not_return_invalid_client(self, app):
        """A structurally valid but unknown client_id is lazily registered
        using its own redirect_uri, so it proceeds to a 302 toward Slack —
        not a 400 of any kind.
        """
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/authorize",
            params={
                "client_id": "lazy-test-client",
                "response_type": "code",
                "code_challenge": "challenge",
                "code_challenge_method": "S256",
                "redirect_uri": "http://localhost:3000/callback",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers["location"].startswith("https://slack.com/oauth/v2/authorize")

    def test_whitespace_client_id_returns_invalid_client(self, app):
        """A client_id with whitespace is genuinely invalid."""
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/authorize",
            params={
                "client_id": "bad client",
                "response_type": "code",
                "code_challenge": "challenge",
                "code_challenge_method": "S256",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"] == "invalid_client"

    def test_non_ascii_client_id_returns_invalid_client(self, app):
        """A client_id with non-ASCII characters is genuinely invalid."""
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/authorize",
            params={
                "client_id": "clïent-émoji-🎉",
                "response_type": "code",
                "code_challenge": "challenge",
                "code_challenge_method": "S256",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"] == "invalid_client"


# -----------------------------------------------------------------------
# get_routes /authorize – lazy registration honors the caller's redirect_uri
# -----------------------------------------------------------------------


class TestAuthorizePreregistersCallerRedirectUri:
    """A stale/unknown client_id reconnecting through /authorize is
    rehabilitated using the redirect_uri it actually presents, not just the
    bare loopback defaults — real MCP clients almost always use a specific
    port and callback path (e.g. http://localhost:53219/callback).
    """

    def test_disallowed_redirect_uri_is_not_honored(self, app):
        """A non-allowlisted redirect_uri must not be added to the lazily
        registered client — otherwise an unknown client_id would become an
        open redirect to any attacker-supplied URI.
        """
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/authorize",
            params={
                "client_id": "another-recycled-client",
                "response_type": "code",
                "code_challenge": "challenge",
                "code_challenge_method": "S256",
                "redirect_uri": "https://evil.example.com/callback",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"] == "invalid_request"
        assert body["error_description"].startswith("Redirect URI")

    def test_dcr_registered_client_is_not_extended_with_new_uri(self, provider, app):
        """A client registered through real DCR declared its redirect_uris;
        an /authorize call with a different one must not silently extend them.
        """
        dcr_client = OAuthClientInformationFull(
            client_id="dcr-client",
            redirect_uris=[AnyUrl("http://localhost:11111/callback")],
            token_endpoint_auth_method="none",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
        )
        asyncio.run(provider.register_client(dcr_client))

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/authorize",
            params={
                "client_id": "dcr-client",
                "response_type": "code",
                "code_challenge": "challenge",
                "code_challenge_method": "S256",
                "redirect_uri": "http://localhost:22222/callback",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"] == "invalid_request"
        assert body["error_description"].startswith("Redirect URI")

    def test_client_recreated_during_token_refresh_can_still_authorize(self, provider, app):
        """After a restart, a client's first contact is usually a /token
        refresh attempt, which carries no redirect_uri — get_client() lazily
        registers it with only the bare loopback defaults. The follow-up
        /authorize with the client's real redirect_uri must extend that lazy
        registration rather than fail redirect_uri validation.
        """
        # Simulate the /token path resolving the unknown client_id.
        asyncio.run(provider.get_client("recycled-client"))

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/authorize",
            params={
                "client_id": "recycled-client",
                "response_type": "code",
                "code_challenge": "challenge",
                "code_challenge_method": "S256",
                "redirect_uri": "http://localhost:3118/callback",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers["location"].startswith("https://slack.com/oauth/v2/authorize")

    def test_lazy_client_returning_with_new_port_is_extended(self, app):
        """A lazily-registered client that picks a fresh ephemeral callback
        port on its next authorization must still be able to authorize.
        """
        client = TestClient(app, raise_server_exceptions=False)
        first = client.get(
            "/authorize",
            params={
                "client_id": "reused-client-id",
                "response_type": "code",
                "code_challenge": "challenge",
                "code_challenge_method": "S256",
                "redirect_uri": "http://localhost:11111/callback",
            },
            follow_redirects=False,
        )
        assert first.status_code == 302

        second = client.get(
            "/authorize",
            params={
                "client_id": "reused-client-id",
                "response_type": "code",
                "code_challenge": "challenge",
                "code_challenge_method": "S256",
                "redirect_uri": "http://localhost:22222/callback",
            },
            follow_redirects=False,
        )
        assert second.status_code == 302
        assert second.headers["location"].startswith("https://slack.com/oauth/v2/authorize")

    def test_disallowed_redirect_uri_is_not_merged_into_lazy_client(self, app):
        """A lazily-registered client must not be extendable with a
        non-allowlisted redirect_uri.
        """
        client = TestClient(app, raise_server_exceptions=False)
        first = client.get(
            "/authorize",
            params={
                "client_id": "lazy-then-evil-client",
                "response_type": "code",
                "code_challenge": "challenge",
                "code_challenge_method": "S256",
                "redirect_uri": "http://localhost:11111/callback",
            },
            follow_redirects=False,
        )
        assert first.status_code == 302

        second = client.get(
            "/authorize",
            params={
                "client_id": "lazy-then-evil-client",
                "response_type": "code",
                "code_challenge": "challenge",
                "code_challenge_method": "S256",
                "redirect_uri": "https://evil.example.com/callback",
            },
            follow_redirects=False,
        )
        assert second.status_code == 400
        body = second.json()
        assert body["error"] == "invalid_request"
        assert body["error_description"].startswith("Redirect URI")

    def test_post_authorize_reads_form_params(self, app):
        """The SDK serves /authorize for POST with form-encoded parameters;
        preregistration must read those — a POST-only client would otherwise
        never get its redirect_uri registered.
        """
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/authorize",
            data={
                "client_id": "post-client",
                "response_type": "code",
                "code_challenge": "challenge",
                "code_challenge_method": "S256",
                "redirect_uri": "http://localhost:3118/callback",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers["location"].startswith("https://slack.com/oauth/v2/authorize")

    def test_malformed_loopback_redirect_uri_returns_invalid_request(self, app):
        """A redirect_uri matching the loopback allowlist pattern but invalid
        as a URL (port above 65535) must surface the SDK's invalid_request,
        not crash preregistration into a 500.
        """
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/authorize",
            params={
                "client_id": "overflow-client",
                "response_type": "code",
                "code_challenge": "challenge",
                "code_challenge_method": "S256",
                "redirect_uri": "http://localhost:999999/callback",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_request"

    def test_malformed_redirect_uri_does_not_crash_lazy_client_extension(self, app):
        """The extension branch also parses the raw redirect_uri; a malformed
        port on a returning lazy client must not 500 either.
        """
        client = TestClient(app, raise_server_exceptions=False)
        first = client.get(
            "/authorize",
            params={
                "client_id": "lazy-overflow-client",
                "response_type": "code",
                "code_challenge": "challenge",
                "code_challenge_method": "S256",
                "redirect_uri": "http://localhost:11111/callback",
            },
            follow_redirects=False,
        )
        assert first.status_code == 302

        second = client.get(
            "/authorize",
            params={
                "client_id": "lazy-overflow-client",
                "response_type": "code",
                "code_challenge": "challenge",
                "code_challenge_method": "S256",
                "redirect_uri": "http://localhost:999999/callback",
            },
            follow_redirects=False,
        )
        assert second.status_code == 400
        assert second.json()["error"] == "invalid_request"

    def test_explicit_scope_request_is_honored(self, app):
        """A client requesting the server's own supported scopes must not be
        rejected with invalid_scope — a lazily-registered client's `scope`
        must cover what the provider is configured to grant, matching what a
        fresh DCR registration would default to.
        """
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/authorize",
            params={
                "client_id": "scope-test-client",
                "response_type": "code",
                "code_challenge": "challenge",
                "code_challenge_method": "S256",
                "redirect_uri": "http://localhost:3118/callback",
                "scope": "channels:history search:read",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers["location"].startswith("https://slack.com/oauth/v2/authorize")


# -----------------------------------------------------------------------
# _build_default_redirect_uris
# -----------------------------------------------------------------------


class TestBuildDefaultRedirectUris:
    def test_returns_loopback_uris(self):
        uris = SlackOAuthProvider._build_default_redirect_uris()
        assert len(uris) >= 2
        uri_strings = [str(u) for u in uris]
        assert any("localhost" in u for u in uri_strings)
        assert any("127.0.0.1" in u for u in uri_strings)

    def test_returns_anyurl_instances(self):
        uris = SlackOAuthProvider._build_default_redirect_uris()
        for u in uris:
            assert isinstance(u, AnyUrl)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
