"""
Session context variables for Slack MCP.

Provides request-scoped context variables for session information.
"""

import contextvars

# Context variables for session management
fastmcp_session_id: contextvars.ContextVar = contextvars.ContextVar(
    "fastmcp_session_id", default=None
)
authenticated_user_id: contextvars.ContextVar = contextvars.ContextVar(
    "authenticated_user_id", default=None
)
