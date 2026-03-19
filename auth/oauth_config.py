"""
OAuth Configuration Management for Slack MCP server.
Handles OAuth 2.1 proxy authorization server configuration.
"""

import os
from threading import RLock
from urllib.parse import urlparse


class SlackOAuthConfig:
    """
    Centralized OAuth configuration management for Slack.

    Uses OAuth 2.1 proxy authorization server pattern where this MCP server
    acts as the authorization server for MCP clients, proxying to Slack.
    """

    def __init__(self):
        # Base server configuration
        self.base_uri = os.getenv("SLACK_MCP_BASE_URI", "http://localhost").rstrip("/")
        self.port = int(os.getenv("SLACK_MCP_PORT", "8001"))
        # Determine base URL (with port if not already specified in base_uri).
        # Only append port for non-standard schemes (e.g. http://localhost needs :8001,
        # but https://slack-mcp.internal.duolingo.com should NOT get :8001 appended).
        parsed = urlparse(self.base_uri)
        if parsed.port:
            # Port already explicit in the URI
            self.base_url = self.base_uri
        elif (parsed.scheme == "https" and self.port == 443) or (
            parsed.scheme == "http" and self.port == 80
        ):
            # Default port for the scheme — don't append
            self.base_url = self.base_uri
        else:
            # Local dev: http://localhost needs the port
            self.base_url = f"{self.base_uri}:{self.port}"

        # External URL for reverse proxy scenarios
        raw_external = os.getenv("SLACK_EXTERNAL_URL")
        self.external_url = raw_external.rstrip("/") if raw_external else None

        # OAuth client configuration
        self.client_id = os.getenv("SLACK_CLIENT_ID")
        self.client_secret = os.getenv("SLACK_CLIENT_SECRET")

        # OAuth scopes required for the tools (user token scopes)
        self.scopes = [
            "channels:history",
            "groups:history",
            "im:history",
            "mpim:history",
            "channels:read",
            "groups:read",
            "im:read",
            "mpim:read",
            "users:read",
            "users:read.email",
            "search:read",
        ]

    def is_configured(self) -> bool:
        """Check if OAuth is properly configured."""
        return bool(self.client_id and self.client_secret)

    def get_oauth_base_url(self) -> str:
        """Get OAuth base URL for constructing OAuth endpoints.

        Uses SLACK_EXTERNAL_URL if set (for reverse proxy scenarios),
        otherwise falls back to constructed base_url with port.
        """
        if self.external_url:
            return self.external_url
        return self.base_url

    def get_slack_callback_url(self) -> str:
        """Get the Slack OAuth callback URL for this server.

        In OAuth 2.1 proxy mode, the MCP server's /oauth2callback endpoint
        receives the callback from Slack after user authorization.
        """
        return f"{self.get_oauth_base_url()}/oauth2callback"


# Global configuration instance with thread-safe access
_oauth_config = None
_oauth_config_lock = RLock()


def get_oauth_config() -> SlackOAuthConfig:
    """Get the global OAuth configuration instance (thread-safe singleton)."""
    global _oauth_config
    with _oauth_config_lock:
        if _oauth_config is None:
            _oauth_config = SlackOAuthConfig()
        return _oauth_config
