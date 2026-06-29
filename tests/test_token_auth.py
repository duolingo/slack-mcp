import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest
from auth import token_auth
from auth.slack_oauth_provider import SlackOAuthProvider
from auth.token_auth import is_slack_bot_token, is_slack_token, resolve_slack_identity
from slack_sdk.errors import SlackApiError


def _make_provider() -> SlackOAuthProvider:
    return SlackOAuthProvider(
        slack_client_id="cid",
        slack_client_secret="secret",
        slack_redirect_uri="https://example.com/oauth2callback",
        slack_scopes=["channels:history", "search:read"],
        base_url="https://example.com",
        required_scopes=["channels:history", "search:read"],
    )


@pytest.fixture(autouse=True)
def clear_slack_identity_cache():
    token_auth._slack_identity_cache.clear()
    yield
    token_auth._slack_identity_cache.clear()


class TestIsSlackToken:
    def test_user_token(self):
        assert is_slack_token("xoxp-123") is True

    def test_bot_token(self):
        assert is_slack_token("xoxb-123") is True

    def test_mcp_token(self):
        assert is_slack_token("mcp_auth_abc") is False

    def test_none(self):
        assert is_slack_token(None) is False

    def test_empty(self):
        assert is_slack_token("") is False


class TestIsSlackBotToken:
    def test_bot_token(self):
        assert is_slack_bot_token("xoxb-123") is True

    def test_user_token(self):
        assert is_slack_bot_token("xoxp-123") is False

    def test_none(self):
        assert is_slack_bot_token(None) is False


class TestResolveSlackIdentity:
    @patch("auth.token_auth.WebClient")
    def test_returns_user_id(self, mock_client_cls):
        client = MagicMock()
        client.auth_test.return_value = {"ok": True, "user_id": "U123"}
        mock_client_cls.return_value = client

        assert asyncio.run(resolve_slack_identity("xoxp-valid")) == "U123"
        mock_client_cls.assert_called_once_with(token="xoxp-valid")

    @patch("auth.token_auth.WebClient")
    def test_caches_identity_for_ten_minutes(self, mock_client_cls):
        client = MagicMock()
        client.auth_test.return_value = {"ok": True, "user_id": "U123"}
        mock_client_cls.return_value = client

        assert asyncio.run(resolve_slack_identity("xoxp-valid")) == "U123"
        assert asyncio.run(resolve_slack_identity("xoxp-valid")) == "U123"

        mock_client_cls.assert_called_once_with(token="xoxp-valid")
        client.auth_test.assert_called_once()

    @patch("auth.token_auth.WebClient")
    def test_revalidates_after_cache_expiry(self, mock_client_cls):
        client = MagicMock()
        client.auth_test.side_effect = [
            {"ok": True, "user_id": "U123"},
            {"ok": True, "user_id": "U456"},
        ]
        mock_client_cls.return_value = client

        assert asyncio.run(resolve_slack_identity("xoxp-valid")) == "U123"

        # Force the cached entry to look expired so the next call re-validates.
        _, identity = token_auth._slack_identity_cache["xoxp-valid"]
        token_auth._slack_identity_cache["xoxp-valid"] = (time.monotonic() - 1, identity)

        assert asyncio.run(resolve_slack_identity("xoxp-valid")) == "U456"
        assert client.auth_test.call_count == 2

    @patch("auth.token_auth.WebClient")
    def test_cache_evicts_oldest_entry_at_max_size(self, mock_client_cls, monkeypatch):
        client = MagicMock()
        client.auth_test.side_effect = [
            {"ok": True, "user_id": "U1"},
            {"ok": True, "user_id": "U2"},
            {"ok": True, "user_id": "U3"},
        ]
        mock_client_cls.return_value = client
        monkeypatch.setattr(token_auth, "_SLACK_IDENTITY_CACHE_MAX_SIZE", 2)

        assert asyncio.run(resolve_slack_identity("xoxp-1")) == "U1"
        assert asyncio.run(resolve_slack_identity("xoxp-2")) == "U2"
        assert asyncio.run(resolve_slack_identity("xoxp-3")) == "U3"

        assert list(token_auth._slack_identity_cache) == ["xoxp-2", "xoxp-3"]

    @patch("auth.token_auth.WebClient")
    def test_falls_back_to_bot_id(self, mock_client_cls):
        client = MagicMock()
        client.auth_test.return_value = {"ok": True, "bot_id": "B123"}
        mock_client_cls.return_value = client

        assert asyncio.run(resolve_slack_identity("xoxb-valid")) == "B123"

    @patch("auth.token_auth.WebClient")
    def test_returns_none_on_slack_api_error(self, mock_client_cls):
        client = MagicMock()
        err_response = MagicMock()
        err_response.get.return_value = "invalid_auth"
        client.auth_test.side_effect = SlackApiError("invalid_auth", err_response)
        mock_client_cls.return_value = client

        assert asyncio.run(resolve_slack_identity("xoxp-bad")) is None

    @patch("auth.token_auth.WebClient")
    def test_returns_none_when_not_ok(self, mock_client_cls):
        client = MagicMock()
        client.auth_test.return_value = {"ok": False, "error": "invalid_auth"}
        mock_client_cls.return_value = client

        assert asyncio.run(resolve_slack_identity("xoxp-bad")) is None


class TestVerifyToken:
    @patch("auth.slack_oauth_provider.resolve_slack_identity")
    def test_accepts_raw_slack_token(self, mock_resolve):
        async def fake_resolve(token):
            return "U123"

        mock_resolve.side_effect = fake_resolve
        provider = _make_provider()

        access_token = asyncio.run(provider.verify_token("xoxb-byok"))

        assert access_token is not None
        assert access_token.token == "xoxb-byok"
        assert access_token.client_id == "U123"
        assert access_token.claims == {
            "slack_token": "xoxb-byok",
            "slack_user_id": "U123",
            "is_byok": True,
        }
        # Must carry the required scopes so RequireAuthMiddleware admits it.
        assert "channels:history" in access_token.scopes
        assert "search:read" in access_token.scopes

    @patch("auth.slack_oauth_provider.resolve_slack_identity")
    def test_rejects_invalid_slack_token(self, mock_resolve):
        async def fake_resolve(token):
            return None

        mock_resolve.side_effect = fake_resolve
        provider = _make_provider()

        assert asyncio.run(provider.verify_token("xoxb-bad")) is None

    def test_delegates_non_slack_token_to_oauth(self):
        provider = _make_provider()
        # No MCP token has been issued, so the standard verification returns None.
        assert asyncio.run(provider.verify_token("mcp_auth_unknown")) is None
