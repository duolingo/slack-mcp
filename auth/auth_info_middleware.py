"""
Middleware that hides tools unavailable to the request's auth context.

Tools read auth data (token claims, allowlist headers) directly via
FastMCP's dependency accessors; this middleware only filters the tool list.
"""

from fastmcp.server.dependencies import get_access_token, get_http_request
from fastmcp.server.middleware import Middleware, MiddlewareContext

from auth.token_auth import is_slack_bot_token

WRITABLE_CHANNELS_HEADER = "x-writable-channels"
ALLOW_PRIVATE_CHANNELS_HEADER = "x-allow-private-channels"
WRITE_TOOL_NAMES = frozenset({"slack_send_message", "slack_reply_in_thread"})
BOT_TOKEN_UNSUPPORTED_TOOL_NAMES = frozenset({"slack_search_messages"})


def _parse_writable_channels(header_value: str | None) -> list[str]:
    """Parse a comma-separated channel allowlist header into normalized names."""
    if not header_value:
        return []
    return [name for raw in header_value.split(",") if (name := raw.strip().lstrip("#"))]


class AuthInfoMiddleware(Middleware):
    """Hide write and search tools the request's auth context can't use."""

    async def on_list_tools(self, context: MiddlewareContext, call_next):
        """Hide tools that are unavailable for the request's auth context."""
        tools = await call_next(context)

        writable = []
        try:
            http_request = get_http_request()
            raw_header = http_request.headers.get(WRITABLE_CHANNELS_HEADER)
            writable = _parse_writable_channels(raw_header)
        except Exception:
            pass

        try:
            access_token = get_access_token()
            claims = getattr(access_token, "claims", {}) or {}
            slack_token = claims.get("slack_token")
        except Exception:
            slack_token = None

        if is_slack_bot_token(slack_token):
            tools = [t for t in tools if t.name not in BOT_TOKEN_UNSUPPORTED_TOOL_NAMES]

        if not writable:
            return [t for t in tools if t.name not in WRITE_TOOL_NAMES]
        return tools
