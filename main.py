#!/usr/bin/env python3
"""
Slack MCP Server
Main entry point for the Slack Model Context Protocol server.

Features secure multi-user authentication with session-based access control.
"""

import logging
import os
import sys
from importlib import metadata

import slack_tools
from auth import context
from auth.oauth_config import get_oauth_config, reload_oauth_config
from auth.oauth_handler import exchange_code_for_token
from auth.session_middleware import SlackSessionMiddleware
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_context
from starlette.middleware import Middleware

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Reload OAuth config
reload_oauth_config()

# Initialize FastMCP server with session middleware
session_middleware = Middleware(SlackSessionMiddleware)


class SecureFastMCP(FastMCP):
    """Custom FastMCP with session middleware for secure authentication."""

    def streamable_http_app(self):
        """Override to add secure middleware stack."""
        app = super().streamable_http_app()

        # Add session middleware
        app.user_middleware.insert(0, session_middleware)

        # Rebuild middleware stack
        app.middleware_stack = app.build_middleware_stack()
        logger.info("Added SlackSessionMiddleware for secure authentication")
        return app


server = SecureFastMCP("Slack MCP Server")


def safe_print(text):
    """Print to stderr safely, avoiding JSON parsing errors in MCP mode."""
    if not sys.stderr.isatty():
        logger.debug(f"[MCP Server] {text}")
        return

    try:
        print(text, file=sys.stderr)
    except UnicodeEncodeError:
        print(text.encode("ascii", errors="replace").decode(), file=sys.stderr)


@server.tool()
def slack_get_channel_messages(
    channel_id: str,
    limit: int = 100,
    cursor: str = None,
) -> dict:
    """
    Get messages from a Slack channel.

    Uses the authenticated user's credentials from the current session.
    Authentication is handled automatically - no user_id required.

    Args:
        channel_id: Channel ID or name (e.g., 'C1234567890' or '#general')
        limit: Maximum number of messages to retrieve (default: 100, max: 1000)
        cursor: Pagination cursor from previous response (optional)

    Returns:
        Dictionary with messages and pagination info
    """
    return slack_tools.get_channel_messages(channel_id, limit, cursor)


@server.tool()
def slack_get_thread_replies(
    channel_id: str,
    thread_ts: str,
    limit: int = 100,
    cursor: str = None,
) -> dict:
    """
    Get replies from a Slack thread.

    Uses the authenticated user's credentials from the current session.
    Authentication is handled automatically - no user_id required.

    Args:
        channel_id: Channel ID or name where the thread exists
        thread_ts: Timestamp of the parent message (e.g., '1234567890.123456')
        limit: Maximum number of replies to retrieve (default: 100, max: 1000)
        cursor: Pagination cursor from previous response (optional)

    Returns:
        Dictionary with thread messages and pagination info
    """
    return slack_tools.get_thread_replies(channel_id, thread_ts, limit, cursor)


@server.tool()
def slack_search_messages(
    query: str,
    count: int = 20,
    page: int = 1,
    from_user: str = None,
    in_channel: str = None,
    after_date: str = None,
    before_date: str = None,
    sort_by: str = "relevance",
    sort_order: str = "desc",
) -> dict:
    """
    Search for messages across all Slack conversations with advanced filters.

    Uses the authenticated user's credentials from the current session.
    Authentication is handled automatically - no user_id required.

    Args:
        query: Search query string (can be empty if using only filters)
        count: Number of results per page (default: 20, max: 100)
        page: Page number for pagination (default: 1)
        from_user: Filter by user ID or username (e.g., 'U123ABC' or '@john')
        in_channel: Filter by channel ID or name (e.g., 'C123ABC' or '#general')
        after_date: Messages after this date (YYYY-MM-DD or relative like '7d', '1m')
        before_date: Messages before this date (YYYY-MM-DD or relative)
        sort_by: Sort results by 'timestamp' or 'relevance' (default: 'relevance')
        sort_order: Sort order 'asc' or 'desc' (default: 'desc')

    Returns:
        Dictionary with search results and pagination info

    Examples:
        - Search in last 7 days: slack_search_messages("important", after_date="7d")
        - Search from user in channel: slack_search_messages("meeting", from_user="@john", in_channel="#team")
        - Date range: slack_search_messages("report", after_date="2025-01-01", before_date="2025-01-31")
    """
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
    )


@server.tool()
def slack_get_users(
    user_id: str = None,
    limit: int = 100,
    cursor: str = None,
) -> dict:
    """
    Get users from Slack workspace.

    This is a dual-mode tool:
    - Without user_id: Lists all users in the workspace with pagination
    - With user_id: Gets detailed profile for a specific user

    Uses the authenticated user's credentials from the current session.
    Authentication is handled automatically - no user_id required.

    Args:
        user_id: Optional user ID. If provided, gets specific user profile
        limit: Maximum number of users to retrieve when listing (default: 100, max: 1000)
        cursor: Pagination cursor from previous response (for listing mode)

    Returns:
        Dictionary with user(s) and pagination info
        - List mode: {"ok": True, "users": [...], "next_cursor": "..."}
        - Get mode: {"ok": True, "user": {...}}
    """
    return slack_tools.get_users(user_id, limit, cursor)


@server.tool()
def slack_get_channels(
    channel_id: str = None,
    types: str = None,
    limit: int = 100,
    cursor: str = None,
    include_members: bool = False,
) -> dict:
    """
    Get channels from Slack workspace.

    This is a dual-mode tool:
    - Without channel_id: Lists channels with optional type filter (defaults to public channels only)
    - With channel_id: Gets detailed info for a specific channel, optionally with members

    Uses the authenticated user's credentials from the current session.
    Authentication is handled automatically - no user_id required.

    Args:
        channel_id: Optional channel ID. If provided, gets specific channel info
        types: Filter by channel types when listing. Defaults to "public_channel" if not specified.
               Examples: "public_channel,private_channel", "im,mpim" (DMs and group DMs)
        limit: Maximum number of channels to retrieve when listing (default: 100, max: 1000)
        cursor: Pagination cursor from previous response (for listing mode)
        include_members: Include member list when getting specific channel (default: False)

    Returns:
        Dictionary with channel(s) and pagination info
        - List mode: {"ok": True, "channels": [...], "next_cursor": "..."}
        - Get mode: {"ok": True, "channel": {...}, "members": [...]}
    """
    return slack_tools.get_channels(channel_id, types, limit, cursor, include_members)


@server.tool()
def slack_get_oauth_url() -> dict:
    """
    Get the OAuth authorization URL for users to authenticate.

    Returns:
        Dictionary with authorization URL
    """
    from auth.session_store import get_session_store

    config = get_oauth_config()
    if not config.is_configured():
        return {
            "ok": False,
            "error": "OAuth not configured. Please set SLACK_CLIENT_ID and SLACK_CLIENT_SECRET.",
        }

    # Get session ID from FastMCP context
    session_id = None
    try:
        ctx = get_context()
        if ctx and hasattr(ctx, "session_id"):
            session_id = ctx.session_id
            logger.info(f"Got FastMCP session ID: {session_id}")
            # Also set it in our context variable for other functions
            context.fastmcp_session_id.set(session_id)
    except Exception as e:
        logger.error(f"Error getting FastMCP context: {e}")

    if not session_id:
        return {
            "ok": False,
            "error": "No session ID found. Unable to generate OAuth URL.",
        }

    # Generate cryptographic state for CSRF protection
    store = get_session_store()
    state = store.generate_oauth_state(session_id)

    return {
        "ok": True,
        "authorization_url": config.get_authorization_url(state=state),
        "instructions": (
            "Visit the authorization URL to authenticate. "
            "After authorization, you'll receive a user_id to use with other tools."
        ),
    }


# Add health check endpoint for ECS
@server.custom_route("/health", methods=["GET"])
async def health_check(request: Request):
    """Health check endpoint for load balancer."""
    return JSONResponse({"status": "healthy"})


# Add OAuth callback endpoint for HTTP transport
@server.custom_route("/oauth2callback", methods=["GET"])
async def oauth_callback(request: Request):
    """
    Handle OAuth callback from Slack.

    This endpoint exchanges the authorization code for a token and binds
    it to the current session for secure access.
    """
    code = request.query_params.get("code")
    error = request.query_params.get("error")

    if error:
        return HTMLResponse(
            content=f"""
            <html>
                <body>
                    <h1>OAuth Error</h1>
                    <p>Error: {error}</p>
                    <p>You can close this window.</p>
                </body>
            </html>
            """,
            status_code=400,
        )

    if not code:
        return HTMLResponse(
            content="""
            <html>
                <body>
                    <h1>OAuth Error</h1>
                    <p>No authorization code received.</p>
                    <p>You can close this window.</p>
                </body>
            </html>
            """,
            status_code=400,
        )

    # Extract state parameter for CSRF validation
    state = request.query_params.get("state")

    if not state:
        return HTMLResponse(
            content="""
            <html>
                <body>
                    <h1>Authentication Failed</h1>
                    <p>Error: Missing OAuth state parameter.</p>
                    <p>This may indicate a CSRF attack attempt.</p>
                    <p>You can close this window.</p>
                </body>
            </html>
            """,
            status_code=400,
        )

    # Validate OAuth state parameter and get the bound session ID (CSRF protection)
    from auth.session_store import get_session_store

    store = get_session_store()

    # Get the session_id that was bound to this state
    # We need to look it up before validation to know which session to validate against
    with store._lock:
        if state not in store._oauth_states:
            logger.error(f"Invalid OAuth state: {state} not found")
            return HTMLResponse(
                content="""
                <html>
                    <body>
                        <h1>Authentication Failed</h1>
                        <p>Error: Invalid or expired OAuth state parameter.</p>
                        <p>Please generate a new OAuth URL using the slack_get_oauth_url tool.</p>
                        <p>You can close this window.</p>
                    </body>
                </html>
                """,
                status_code=400,
            )
        session_id, _ = store._oauth_states[state]

    logger.info(f"OAuth callback with session ID: {session_id}")

    # Validate and consume the OAuth state
    if not store.validate_and_consume_oauth_state(state, session_id):
        logger.error(f"SECURITY: Invalid OAuth state for session {session_id}")
        return HTMLResponse(
            content="""
            <html>
                <body>
                    <h1>Authentication Failed</h1>
                    <p>Error: Invalid or expired OAuth state parameter.</p>
                    <p>This may indicate a CSRF attack attempt or an expired authorization request.</p>
                    <p>Please generate a new OAuth URL and try again.</p>
                    <p>You can close this window.</p>
                </body>
            </html>
            """,
            status_code=400,
        )

    # Set session context for token exchange
    context.fastmcp_session_id.set(session_id)

    try:
        # Exchange code for token (will bind to session automatically)
        token, user_id, exchange_error = await exchange_code_for_token(code)

        if exchange_error:
            return HTMLResponse(
                content=f"""
                <html>
                    <body>
                        <h1>Authentication Failed</h1>
                        <p>Error: {exchange_error}</p>
                        <p>You can close this window.</p>
                    </body>
                </html>
                """,
                status_code=500,
            )

        return HTMLResponse(
            content=f"""
            <html>
                <body>
                    <h1>✅ Authentication Successful!</h1>
                    <p>You have been authenticated as user: <strong>{user_id}</strong></p>
                    <p>Your session is now authorized to access Slack.</p>
                    <p>You can close this window and return to Cursor.</p>
                </body>
            </html>
            """
        )
    except Exception as e:
        # Catch any unexpected exceptions and return user-friendly error
        logger.error(f"Unexpected error in OAuth callback: {e}", exc_info=True)
        return HTMLResponse(
            content=f"""
            <html>
                <body>
                    <h1>Authentication Failed</h1>
                    <p>An unexpected error occurred during authentication.</p>
                    <p>Error: {e!s}</p>
                    <p>You can close this window and try again.</p>
                </body>
            </html>
            """,
            status_code=500,
        )


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
    safe_print("   🔐 slack_get_oauth_url - Get OAuth authorization URL")
    safe_print("")

    if not config.is_configured():
        safe_print("⚠️  Warning: OAuth not configured!")
        safe_print("   Please set SLACK_CLIENT_ID and SLACK_CLIENT_SECRET environment variables")
        safe_print("")

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
