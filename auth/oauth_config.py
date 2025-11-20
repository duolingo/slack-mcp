"""
OAuth Configuration Management for Slack MCP server.
Handles OAuth 2.0 configuration (tokens managed by session_store).
"""

import os
from urllib.parse import quote, urlparse


class SlackOAuthConfig:
    """
    Centralized OAuth configuration management for Slack.

    Note: User token storage has been moved to auth/session_store.py
    for secure multi-user session management.
    """

    def __init__(self):
        # Base server configuration
        self.base_uri = os.getenv("SLACK_MCP_BASE_URI", "http://localhost")
        self.port = int(os.getenv("SLACK_MCP_PORT", "8001"))
        # Determine base URL (with port if not already specified in base_uri)
        parsed = urlparse(self.base_uri)
        self.base_url = self.base_uri if parsed.port else f"{self.base_uri}:{self.port}"

        # External URL for reverse proxy scenarios
        self.external_url = os.getenv("SLACK_EXTERNAL_URL")

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

        # Redirect URI configuration
        self.redirect_uri = self._get_redirect_uri()

    def _get_redirect_uri(self) -> str:
        """Get the OAuth redirect URI.

        Uses SLACK_EXTERNAL_URL for ngrok/reverse proxy scenarios,
        falls back to base_url for local development.
        """
        # Use external URL if available (for ngrok/reverse proxy)
        base = self.external_url if self.external_url else self.base_url
        return f"{base}/oauth2callback"

    def is_configured(self) -> bool:
        """Check if OAuth is properly configured."""
        return bool(self.client_id and self.client_secret)

    def get_authorization_url(self, state: str = None) -> str:
        """Get the Slack OAuth authorization URL using user_scope.

        Slack returns a user token (xoxp-...) in authed_user.access_token only
        when scopes are passed via the user_scope parameter.

        Args:
            state: Optional state parameter to pass through OAuth flow (e.g., session ID)
        """
        user_scopes = ",".join(self.scopes)
        url = (
            f"https://slack.com/oauth/v2/authorize"
            f"?client_id={quote(self.client_id, safe='')}"
            f"&user_scope={quote(user_scopes, safe='')}"
            f"&redirect_uri={quote(self.redirect_uri, safe='')}"
        )
        if state:
            url += f"&state={quote(state, safe='')}"
        return url


# Global configuration instance
_oauth_config = None


def get_oauth_config() -> SlackOAuthConfig:
    """Get the global OAuth configuration instance."""
    global _oauth_config
    if _oauth_config is None:
        _oauth_config = SlackOAuthConfig()
    return _oauth_config


def reload_oauth_config() -> SlackOAuthConfig:
    """Reload the OAuth configuration from environment variables."""
    global _oauth_config
    _oauth_config = SlackOAuthConfig()
    return _oauth_config
