"""
Tests for OAuth configuration.
"""

import os

import pytest
from auth.oauth_config import SlackOAuthConfig


def test_oauth_config_initialization():
    """Test OAuth config initialization with defaults."""
    config = SlackOAuthConfig()
    assert config.base_uri == os.getenv("SLACK_MCP_BASE_URI", "http://localhost")
    assert config.port == int(os.getenv("SLACK_MCP_PORT", "8001"))
    assert config.scopes is not None
    assert len(config.scopes) > 0


def test_oauth_config_with_env_vars(monkeypatch):
    """Test OAuth config with environment variables."""
    monkeypatch.setenv("SLACK_CLIENT_ID", "test_client_id")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "test_client_secret")
    monkeypatch.setenv("SLACK_MCP_PORT", "9999")

    config = SlackOAuthConfig()
    assert config.client_id == "test_client_id"
    assert config.client_secret == "test_client_secret"
    assert config.port == 9999
    assert config.is_configured() is True


def test_redirect_uri():
    """Test redirect URI generation."""
    config = SlackOAuthConfig()
    assert config.redirect_uri.endswith("/oauth2callback")


def test_authorization_url():
    """Test authorization URL generation."""
    config = SlackOAuthConfig()
    config.client_id = "test_client_id"

    auth_url = config.get_authorization_url()
    assert "slack.com/oauth/v2/authorize" in auth_url
    assert "client_id=test_client_id" in auth_url
    assert "user_scope=" in auth_url


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
