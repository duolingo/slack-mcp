"""
OAuth handler for Slack authentication.
Manages the OAuth flow and token exchange with session binding.
"""

import logging
from typing import Optional, Tuple

import aiohttp
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from auth import context
from auth.session_store import get_session_store

logger = logging.getLogger(__name__)


async def exchange_code_for_token(code: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Exchange authorization code for access token and bind to session.

    Args:
        code: Authorization code from Slack OAuth callback

    Returns:
        Tuple of (access_token, user_id, error_message)
    """
    from auth.oauth_config import get_oauth_config

    config = get_oauth_config()

    if not config.is_configured():
        return None, None, "OAuth not configured: Missing client_id or client_secret"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://slack.com/api/oauth.v2.access",
                data={
                    "client_id": config.client_id,
                    "client_secret": config.client_secret,
                    "code": code,
                    "redirect_uri": config.redirect_uri,
                },
            ) as response:
                data = await response.json()

                if not data.get("ok"):
                    error_msg = data.get("error", "Unknown error")
                    logger.error(f"OAuth token exchange failed: {error_msg}")
                    return None, None, f"Token exchange failed: {error_msg}"

                # Extract user token and user_id
                access_token = data.get("authed_user", {}).get("access_token")
                user_id = data.get("authed_user", {}).get("id")

                if not access_token or not user_id:
                    logger.error("Missing access_token or user_id in OAuth response")
                    return None, None, "Invalid OAuth response"

                # Get session ID from context (if available)
                session_id = context.fastmcp_session_id.get()

                # Store the token with session binding
                store = get_session_store()
                try:
                    store.store_user_token(user_id, access_token, session_id)
                    logger.info(
                        f"Successfully authenticated user {user_id}"
                        + (f" and bound to session {session_id}" if session_id else "")
                    )
                except ValueError as e:
                    # Session binding conflict
                    logger.error(f"Session binding error: {e}")
                    return None, None, str(e)

                return access_token, user_id, None

    except Exception as e:
        logger.error(f"Error during token exchange: {e}")
        return None, None, f"Exception during token exchange: {e!s}"


def get_slack_client_for_session() -> Optional[WebClient]:
    """
    Get a Slack WebClient for the current authenticated session.

    This function uses the session context to securely retrieve the
    appropriate user's token without requiring user_id as a parameter.

    Returns:
        WebClient instance or None if user not authenticated
    """
    user_id = context.authenticated_user_id.get()
    session_id = context.fastmcp_session_id.get()

    if not user_id:
        logger.warning("No authenticated user in current session context")
        return None

    # Get token with session validation
    store = get_session_store()
    token = store.get_user_token_with_validation(user_id, session_id)

    if not token:
        logger.warning(f"No valid token found for user {user_id} in session {session_id}")
        return None

    return WebClient(token=token)


def validate_session_token() -> Tuple[bool, Optional[str]]:
    """
    Validate that the current session has a valid token.

    Returns:
        Tuple of (is_valid, error_message)
    """
    user_id = context.authenticated_user_id.get()

    if not user_id:
        return False, "No authenticated user in current session. Please complete OAuth flow first."

    client = get_slack_client_for_session()
    if not client:
        return False, f"User {user_id} is not authenticated. Please complete OAuth flow first."

    # Test the token by calling auth.test
    try:
        response = client.auth_test()
        if response.get("ok"):
            return True, None
        else:
            return False, f"Token validation failed: {response.get('error', 'Unknown error')}"
    except SlackApiError as e:
        return False, f"Token validation failed: {e.response.get('error', 'Unknown error')}"
    except Exception as e:
        return False, f"Token validation error: {e!s}"
