"""
Authentication middleware to populate context state with Slack user information.

In the proxy authorization server pattern, FastMCP validates the MCP-issued
Bearer token and calls load_access_token() which attaches the Slack token
to claims. This middleware extracts those claims into context state.
"""

import logging

from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware, MiddlewareContext

logger = logging.getLogger(__name__)


class AuthInfoMiddleware(Middleware):
    """
    Middleware to extract Slack authentication information from
    FastMCP-validated access token claims and populate context state.
    """

    async def _process_request_for_auth(self, context: MiddlewareContext):
        """Extract slack_token and user_id from FastMCP-validated access token claims."""
        if not context.fastmcp_context:
            logger.warning("No fastmcp_context available")
            return

        try:
            access_token = get_access_token()
        except Exception as e:
            logger.debug("Could not get FastMCP access_token: %s", e)
            return

        if not access_token:
            logger.debug("No access token present (might be using stdio transport)")
            return

        claims = getattr(access_token, "claims", {}) or {}
        slack_token = claims.get("slack_token")
        slack_user_id = claims.get("slack_user_id")

        if slack_token and slack_user_id:
            context.fastmcp_context.set_state("slack_token", slack_token)
            context.fastmcp_context.set_state("authenticated_user_id", slack_user_id)
            logger.debug("Authenticated Slack user %s via proxy OAuth", slack_user_id)
        else:
            logger.warning("Access token valid but missing slack_token/slack_user_id in claims")

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        """Extract auth info from token claims and set in context state."""
        await self._process_request_for_auth(context)
        return await call_next(context)

    async def on_get_prompt(self, context: MiddlewareContext, call_next):
        """Extract auth info for prompt requests too."""
        await self._process_request_for_auth(context)
        return await call_next(context)
