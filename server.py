#!/usr/bin/env python3
"""
Slack MCP Server
Main entry point for the Slack Model Context Protocol server.

Features secure multi-user authentication via OAuth 2.1 proxy authorization server.
"""

import logging
import os
import sys
from importlib import metadata
from typing import Annotated

import slack_tools
from auth.oauth_config import get_oauth_config
from fastapi import Request
from fastapi.responses import JSONResponse
from fastmcp import FastMCP

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

server = FastMCP("Slack MCP Server")


def configure_server_for_http():
    """
    Configure the OAuth 2.1 authentication provider for HTTP transport.
    Must be called BEFORE server.run().

    Sets up SlackOAuthProvider (proxy pattern) and AuthInfoMiddleware
    for extracting Slack tokens from MCP token claims.
    """
    config = get_oauth_config()

    if not config.is_configured():
        logger.warning("OAuth credentials not configured")
        return

    try:
        from auth.auth_info_middleware import AuthInfoMiddleware
        from auth.slack_oauth_provider import SlackOAuthProvider
        from mcp.server.auth.settings import ClientRegistrationOptions

        # base_url = what MCP clients connect to (the public-facing URL).
        # Must use get_oauth_base_url() which respects SLACK_EXTERNAL_URL,
        # because the OAuth metadata (issuer, authorization_endpoint, token_endpoint, etc.)
        # must advertise URLs reachable by external MCP clients, not localhost.
        # slack_redirect_uri = what Slack redirects to (same external URL)
        provider = SlackOAuthProvider(
            slack_client_id=config.client_id,
            slack_client_secret=config.client_secret,
            slack_redirect_uri=config.get_slack_callback_url(),
            slack_scopes=config.scopes,
            base_url=config.get_oauth_base_url(),
            required_scopes=sorted(config.scopes),
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=sorted(config.scopes),
                default_scopes=sorted(config.scopes),
            ),
        )
        server.auth = provider

        # Add AuthInfoMiddleware to extract Slack auth info from token claims
        auth_middleware = AuthInfoMiddleware()
        server.add_middleware(auth_middleware)

        logger.info("OAuth 2.1 enabled with proxy authorization server pattern")
        logger.info("MCP clients authenticate via standard OAuth 2.1 with this server")
        logger.info("Slack tokens are stored server-side, never exposed to clients")

    except Exception as exc:
        logger.error("Failed to initialize OAuth 2.1 provider: %s", exc, exc_info=True)
        raise


def safe_print(text):
    """Print to stderr safely, avoiding JSON parsing errors in MCP mode."""
    if not sys.stderr.isatty():
        logger.debug(f"[MCP Server] {text}")
        return

    try:
        print(text, file=sys.stderr)
    except UnicodeEncodeError:
        print(text.encode("ascii", errors="replace").decode(), file=sys.stderr)


@server.tool(
    description="Retrieve messages from a Slack channel with pagination support. Accepts channel IDs or #names.",
    annotations={"title": "Get Channel Messages", "readOnlyHint": True},
)
def slack_get_channel_messages(
    channel_id: Annotated[str, "Channel ID or name (e.g., 'C1234567890' or '#general')"],
    limit: Annotated[int, "Maximum number of messages to retrieve (max 1000)"] = 100,
    cursor: Annotated[str | None, "Pagination cursor from previous response"] = None,
    compact: Annotated[bool, "If True, returns only essential fields"] = True,
) -> dict:
    """Calls conversations_history with optional channel-name resolution."""
    return slack_tools.get_channel_messages(channel_id, limit, cursor, compact)


@server.tool(
    description="Get replies from a Slack thread. Accepts channel IDs or #names.",
    annotations={"title": "Get Thread Replies", "readOnlyHint": True},
)
def slack_get_thread_replies(
    channel_id: Annotated[str, "Channel ID or name where the thread exists"],
    thread_ts: Annotated[str, "Timestamp of the parent message (e.g., '1234567890.123456')"],
    limit: Annotated[int, "Maximum number of replies to retrieve (max 1000)"] = 100,
    cursor: Annotated[str | None, "Pagination cursor from previous response"] = None,
    compact: Annotated[bool, "If True, returns only essential fields"] = True,
) -> dict:
    """Calls conversations_replies with optional channel-name resolution."""
    return slack_tools.get_thread_replies(channel_id, thread_ts, limit, cursor, compact)


@server.tool(
    description=(
        "Search for messages across all Slack conversations with advanced filters. "
        "Supports date ranges (YYYY-MM-DD or relative like '7d', '1m'), user filters, and channel filters."
    ),
    annotations={"title": "Search Messages", "readOnlyHint": True},
)
def slack_search_messages(
    query: Annotated[str, "Search query string (can be empty if using only filters)"],
    count: Annotated[int, "Number of results per page (max 100)"] = 20,
    page: Annotated[int, "Page number for pagination"] = 1,
    from_user: Annotated[
        str | None, "Filter by user ID or username (e.g., 'U123ABC' or '@john')"
    ] = None,
    in_channel: Annotated[
        str | None, "Filter by channel ID or name (e.g., 'C123ABC' or '#general')"
    ] = None,
    after_date: Annotated[
        str | None, "Messages after this date (YYYY-MM-DD or relative like '7d', '1m')"
    ] = None,
    before_date: Annotated[str | None, "Messages before this date (YYYY-MM-DD or relative)"] = None,
    sort_by: Annotated[str, "Sort by 'timestamp' or 'relevance'"] = "relevance",
    sort_order: Annotated[str, "Sort order: 'asc' or 'desc'"] = "desc",
    compact: Annotated[bool, "If True, returns only essential fields"] = True,
) -> dict:
    """Builds enhanced query with filters and calls search_messages."""
    return slack_tools.search_messages(
        query=query,
        count=count,
        page=page,
        from_user=from_user,
        in_channel=in_channel,
        after_date=after_date,
        before_date=before_date,
        sort_by=sort_by,
        sort_order=sort_order,
        compact=compact,
    )


@server.tool(
    description="List workspace users or get a specific user's profile by ID.",
    annotations={"title": "Get Users", "readOnlyHint": True},
)
def slack_get_users(
    user_id: Annotated[
        str | None, "User ID to get a specific profile; omit to list all users"
    ] = None,
    limit: Annotated[int, "Maximum number of users when listing (max 1000)"] = 100,
    cursor: Annotated[str | None, "Pagination cursor from previous response"] = None,
    compact: Annotated[bool, "If True, returns only essential fields"] = True,
) -> dict:
    """Calls users_info (single) or users_list (all) depending on user_id."""
    return slack_tools.get_users(user_id, limit, cursor, compact)


@server.tool(
    description="List channels or get detailed info for a specific channel. Supports filtering by type (public, private, DM, group DM).",
    annotations={"title": "Get Channels", "readOnlyHint": True},
)
def slack_get_channels(
    channel_id: Annotated[
        str | None, "Channel ID to get specific channel info; omit to list channels"
    ] = None,
    types: Annotated[
        str | None, "Channel types to filter: 'public_channel,private_channel', 'im,mpim', etc."
    ] = None,
    limit: Annotated[int, "Maximum number of channels when listing (max 1000)"] = 100,
    cursor: Annotated[str | None, "Pagination cursor from previous response"] = None,
    include_members: Annotated[bool, "Include member list when getting a specific channel"] = False,
    compact: Annotated[bool, "If True, returns only essential fields"] = True,
) -> dict:
    """Calls conversations_info (single) or conversations_list (all) depending on channel_id."""
    return slack_tools.get_channels(channel_id, types, limit, cursor, include_members, compact)


@server.tool(
    description=(
        "Send a message to a Slack channel. "
        "Only available when X-Writable-Channels header is configured. Target channel must be in the allowlist. "
        "Accepts channel names or IDs."
    ),
    annotations={"title": "Send Message", "readOnlyHint": False},
)
def slack_send_message(
    channel: Annotated[
        str,
        "Channel name or ID to send to (e.g., 'general', '#general', or 'C1234567890'). Must be in the allowlist.",
    ],
    text: Annotated[
        str, "Message text. Supports Slack mrkdwn (*bold*, _italic_, <url|link text>, <@user_id>)."
    ],
) -> dict:
    """Validates channel against allowlist, then calls chat_postMessage with Block Kit footer."""
    return slack_tools.send_message(channel, text)


@server.tool(
    description=(
        "Reply to a thread in a Slack channel. "
        "Only available when X-Writable-Channels header is configured. Target channel must be in the allowlist. "
        "Accepts channel names or IDs."
    ),
    annotations={"title": "Reply in Thread", "readOnlyHint": False},
)
def slack_reply_in_thread(
    channel: Annotated[
        str, "Channel name or ID where the thread exists. Must be in the allowlist."
    ],
    thread_ts: Annotated[
        str, "Timestamp of the parent message to reply to (e.g., '1234567890.123456')"
    ],
    text: Annotated[str, "Reply text. Supports Slack mrkdwn formatting."],
) -> dict:
    """Validates channel against allowlist, then calls chat_postMessage with thread_ts and Block Kit footer."""
    return slack_tools.reply_in_thread(channel, thread_ts, text)


# Add health check endpoint for ECS
@server.custom_route("/health", methods=["GET"])
async def health_check(request: Request):
    """Health check endpoint for load balancer."""
    return JSONResponse({"status": "healthy"})


def main():
    """Main entry point for the Slack MCP server."""
    # Set port and base URI
    port = int(os.getenv("SLACK_MCP_PORT", "8001"))
    base_uri = os.getenv("SLACK_MCP_BASE_URI", "http://localhost")
    external_url = os.getenv("SLACK_EXTERNAL_URL")
    display_url = external_url if external_url else f"{base_uri}:{port}"

    safe_print("🔧 Slack MCP Server")
    safe_print("=" * 35)
    safe_print("📋 Server Information:")

    try:
        version = metadata.version("slack-mcp")
    except metadata.PackageNotFoundError:
        version = "dev"

    safe_print(f"   📦 Version: {version}")
    safe_print("   🌐 Transport: HTTP")
    safe_print(f"   🔗 URL: {display_url}")
    safe_print(f"   🔐 OAuth Callback: {display_url}/oauth2callback")
    safe_print(f"   🐍 Python: {sys.version.split()[0]}")
    safe_print("")

    # Active Configuration
    safe_print("⚙️ Active Configuration:")

    config = get_oauth_config()
    client_id = config.client_id or "Not Set"

    safe_print(f"   - SLACK_CLIENT_ID: {client_id}")
    safe_print(f"   - SLACK_MCP_BASE_URI: {base_uri}")
    safe_print(f"   - SLACK_MCP_PORT: {port}")
    safe_print("")

    safe_print("🛠️  Available Tools:")
    safe_print("   📜 slack_get_channel_messages - Retrieve channel messages")
    safe_print("   💬 slack_get_thread_replies - Get thread replies")
    safe_print("   🔍 slack_search_messages - Search messages")
    safe_print("   👤 slack_get_users - List users or get user profile")
    safe_print("   📢 slack_get_channels - List channels or get channel info")
    safe_print("   ✏️  slack_send_message - Send a message (requires X-Writable-Channels header)")
    safe_print(
        "   ↩️  slack_reply_in_thread - Reply in a thread (requires X-Writable-Channels header)"
    )
    safe_print("")

    if not config.is_configured():
        safe_print("⚠️  Warning: OAuth not configured!")
        safe_print("   Please set SLACK_CLIENT_ID and SLACK_CLIENT_SECRET environment variables")
        safe_print("")

    # Configure OAuth 2.1 (must be before server.run)
    configure_server_for_http()

    try:
        safe_print("🚀 Starting HTTP server")
        safe_print("✅ Ready for MCP connections")
        safe_print("")

        server.run(transport="streamable-http", host="0.0.0.0", port=port)

    except KeyboardInterrupt:
        safe_print("\n👋 Server shutdown requested")
        sys.exit(0)
    except Exception as e:
        safe_print(f"\n❌ Server error: {e}")
        logger.error(f"Unexpected error running server: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
