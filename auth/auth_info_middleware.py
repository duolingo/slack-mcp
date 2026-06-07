"""
Authentication middleware to populate context state with Slack user information.

In the proxy authorization server pattern, FastMCP validates the MCP-issued
Bearer token and calls load_access_token() which attaches the Slack token
to claims. This middleware extracts those claims into context state.
"""

import logging

from fastmcp.server.dependencies import get_access_token, get_http_request
from fastmcp.server.middleware import Middleware, MiddlewareContext

logger = logging.getLogger(__name__)

WRITABLE_CHANNELS_HEADER = "x-writable-channels"
WRITE_TOOL_NAMES = frozenset({"slack_send_message", "slack_reply_in_thread"})


def _parse_writable_channels(header_value: str | None) -> list[str]:
    """Parse a comma-separated channel allowlist header into normalized names."""
    if not header_value:
        return []
    return [
        name
        for raw in header_value.split(",")
        if (name := raw.strip().lstrip("#"))
    ]


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

        try:
            http_request = get_http_request()
            raw_header = http_request.headers.get(WRITABLE_CHANNELS_HEADER)
            if writable := _parse_writable_channels(raw_header):
                context.fastmcp_context.set_state("writable_channels", writable)
        except Exception:
            pass

    async def on_list_tools(self, context: MiddlewareContext, call_next):
        """Hide write tools when X-Writable-Channels header is absent."""
        tools = await call_next(context)

        writable = []
        try:
            http_request = get_http_request()
            raw_header = http_request.headers.get(WRITABLE_CHANNELS_HEADER)
            writable = _parse_writable_channels(raw_header)
        except Exception:
            pass

        if not writable:
            return [t for t in tools if t.name not in WRITE_TOOL_NAMES]
        return tools

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        """Extract auth info from token claims and set in context state."""
        await self._process_request_for_auth(context)
        return await call_next(context)

    async def on_get_prompt(self, context: MiddlewareContext, call_next):
        """Extract auth info for prompt requests too."""
        await self._process_request_for_auth(context)
        return await call_next(context)
