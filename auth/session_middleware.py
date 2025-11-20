"""
Session middleware for Slack MCP.

This middleware intercepts MCP requests and extracts session information,
making it available to tool functions via context variables.
"""

import logging
from typing import Any, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from auth import context
from auth.session_store import get_session_store

logger = logging.getLogger(__name__)


class SlackSessionMiddleware(BaseHTTPMiddleware):
    """
    Middleware that extracts session information from requests and makes it
    available to MCP tool functions via context variables.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Any:
        """Process request and set session context."""
        logger.debug(f"SlackSessionMiddleware processing: {request.method} {request.url.path}")

        # Skip non-MCP paths (but allow OAuth callback)
        if not (request.url.path.startswith("/mcp") or request.url.path == "/oauth2callback"):
            logger.debug(f"Skipping non-MCP path: {request.url.path}")
            return await call_next(request)

        try:
            # Extract session information
            session_id = None
            user_id = None

            # Check for FastMCP session ID (from streamable HTTP transport)
            if hasattr(request.state, "session_id"):
                session_id = request.state.session_id
                logger.debug(f"Found FastMCP session ID: {session_id}")

            # If no session from FastMCP, try headers
            if not session_id:
                headers = dict(request.headers)
                session_id = (
                    headers.get("mcp-session-id")
                    or headers.get("Mcp-Session-Id")
                    or headers.get("x-session-id")
                    or headers.get("X-Session-ID")
                )
                if session_id:
                    logger.debug(f"Found session ID in headers: {session_id}")

            # Look up authenticated user from session binding
            if session_id:
                store = get_session_store()
                user_id = store.get_user_by_session(session_id)
                if user_id:
                    logger.debug(f"Found authenticated user {user_id} for session {session_id}")

            # Set context variables for easy access
            if session_id or user_id:
                logger.debug(
                    f"MCP request with session: session_id={session_id}, user_id={user_id}"
                )
                context.fastmcp_session_id.set(session_id)
                context.authenticated_user_id.set(user_id)

            # Process request
            response = await call_next(request)
            return response

        except Exception as e:
            logger.error(f"Error in Slack session middleware: {e}", exc_info=True)
            # Re-raise the exception to avoid duplicate request handling
            raise
