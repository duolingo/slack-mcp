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


def test_callback_url():
    """Test callback URL generation."""
    config = SlackOAuthConfig()
    assert config.get_slack_callback_url().endswith("/oauth2callback")


def test_https_non_default_port_preserved(monkeypatch):
    """Test that non-default HTTPS port is included in base_url."""
    monkeypatch.setenv("SLACK_MCP_BASE_URI", "https://mcp.example")
    monkeypatch.setenv("SLACK_MCP_PORT", "8443")
    monkeypatch.delenv("SLACK_EXTERNAL_URL", raising=False)

    config = SlackOAuthConfig()
    assert config.base_url == "https://mcp.example:8443"


def test_https_default_port_omitted(monkeypatch):
    """Test that default HTTPS port (443) is omitted from base_url."""
    monkeypatch.setenv("SLACK_MCP_BASE_URI", "https://mcp.example")
    monkeypatch.setenv("SLACK_MCP_PORT", "443")
    monkeypatch.delenv("SLACK_EXTERNAL_URL", raising=False)

    config = SlackOAuthConfig()
    assert config.base_url == "https://mcp.example"


def test_explicit_port_in_uri_preserved(monkeypatch):
    """Test that explicit port in base URI is preserved as-is."""
    monkeypatch.setenv("SLACK_MCP_BASE_URI", "https://mcp.example:9090")
    monkeypatch.setenv("SLACK_MCP_PORT", "8443")
    monkeypatch.delenv("SLACK_EXTERNAL_URL", raising=False)

    config = SlackOAuthConfig()
    assert config.base_url == "https://mcp.example:9090"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
