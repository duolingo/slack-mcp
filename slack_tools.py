"""
Slack MCP Tools
Provides tools for interacting with Slack conversations, users, and channels.

All tools use session-based authentication and automatically retrieve
the appropriate user's credentials from the session context.
"""

import logging
import re
from datetime import datetime, timedelta
from typing import Optional

from auth import context
from auth.oauth_handler import get_slack_client_for_session, validate_session_token
from fastmcp.server.dependencies import get_context
from slack_sdk.errors import SlackApiError

logger = logging.getLogger(__name__)


def _parse_relative_date(date_str: str) -> Optional[str]:
    """
    Parse relative date strings like '7d', '1m', '2w' into YYYY-MM-DD format.

    Args:
        date_str: Relative date string (e.g., '7d', '1m', '2w', '1y')

    Returns:
        Date string in YYYY-MM-DD format, or None if invalid
    """
    match = re.match(r"^(\d+)([dmwy])$", date_str.lower())
    if not match:
        return None

    amount, unit = match.groups()
    amount = int(amount)

    today = datetime.now()
    if unit == "d":
        target_date = today - timedelta(days=amount)
    elif unit == "w":
        target_date = today - timedelta(weeks=amount)
    elif unit == "m":
        # Approximate month as 30 days
        target_date = today - timedelta(days=amount * 30)
    elif unit == "y":
        # Approximate year as 365 days
        target_date = today - timedelta(days=amount * 365)
    else:
        return None

    return target_date.strftime("%Y-%m-%d")


def _parse_date(date_str: str) -> Optional[str]:
    """
    Parse date string (absolute or relative) into YYYY-MM-DD format.

    Args:
        date_str: Date string (YYYY-MM-DD or relative like '7d')

    Returns:
        Date string in YYYY-MM-DD format, or None if invalid
    """
    # Try relative date first
    relative = _parse_relative_date(date_str)
    if relative:
        return relative

    # Try absolute date YYYY-MM-DD
    try:
        parsed = datetime.strptime(date_str, "%Y-%m-%d")
        return parsed.strftime("%Y-%m-%d")
    except ValueError:
        pass

    return None


def _build_search_query(
    base_query: str,
    from_user: Optional[str] = None,
    in_channel: Optional[str] = None,
    after_date: Optional[str] = None,
    before_date: Optional[str] = None,
) -> str:
    """
    Build Slack search query with filters.

    Args:
        base_query: Base search query text
        from_user: Filter by user ID or username
        in_channel: Filter by channel ID or name
        after_date: Messages after this date (YYYY-MM-DD)
        before_date: Messages before this date (YYYY-MM-DD)

    Returns:
        Formatted Slack search query string
    """
    query_parts = [base_query] if base_query else []

    if from_user:
        # Add @ if not present for usernames
        if not from_user.startswith("U") and not from_user.startswith("@"):
            from_user = f"@{from_user}"
        query_parts.append(f"from:{from_user}")

    if in_channel:
        # Add # if not present for channel names
        if not in_channel.startswith("C") and not in_channel.startswith("#"):
            in_channel = f"#{in_channel}"
        query_parts.append(f"in:{in_channel}")

    if after_date:
        query_parts.append(f"after:{after_date}")

    if before_date:
        query_parts.append(f"before:{before_date}")

    return " ".join(query_parts)


def _get_session_context():
    """
    Extract session ID and user ID from FastMCP context.
    Returns tuple of (session_id, user_id)
    """
    session_id = None
    user_id = None

    try:
        ctx = get_context()
        if ctx and hasattr(ctx, "session_id"):
            session_id = ctx.session_id
            context.fastmcp_session_id.set(session_id)

            # Get user ID from session store
            from auth.session_store import get_session_store

            store = get_session_store()
            user_id = store.get_user_by_session(session_id)
            if user_id:
                context.authenticated_user_id.set(user_id)
                logger.debug(f"Found user {user_id} for session {session_id}")
    except Exception as e:
        logger.error(f"Error getting session context: {e}")

    return session_id, user_id


def _get_authenticated_client():
    """
    Get authenticated Slack client for current session.

    Handles session validation and client initialization with proper error handling.

    Returns:
        tuple: (client, user_id, error_dict)
        - On success: (SlackClient, str, None)
        - On failure: (None, None, {"ok": False, "error": str})
    """
    session_id, user_id = _get_session_context()

    is_valid, error_msg = validate_session_token()
    if not is_valid:
        return None, None, {"ok": False, "error": error_msg}

    client = get_slack_client_for_session()
    if not client:
        return (
            None,
            None,
            {"ok": False, "error": "Failed to get Slack client for authenticated user"},
        )

    return client, user_id, None


def _resolve_channel_name(client, channel_name: str) -> Optional[str]:
    """
    Resolve a channel name to its ID, paginating through all results.

    Args:
        client: Authenticated Slack client
        channel_name: Channel name (without #)

    Returns:
        Channel ID if found, None otherwise
    """
    cursor = None
    while True:
        kwargs = {"types": "public_channel,private_channel"}
        if cursor:
            kwargs["cursor"] = cursor

        channels_response = client.conversations_list(**kwargs)
        for channel in channels_response.get("channels", []):
            if channel.get("name") == channel_name:
                return channel.get("id")

        # Check if there are more pages
        cursor = channels_response.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    return None


def get_channel_messages(
    channel_id: str,
    limit: int = 100,
    cursor: Optional[str] = None,
) -> dict:
    """
    Get messages from a Slack channel.

    Uses the authenticated user's credentials from the current session context.

    Args:
        channel_id: Channel ID or name (e.g., 'C1234567890' or '#general')
        limit: Maximum number of messages to retrieve (default: 100, max: 1000)
        cursor: Pagination cursor from previous response

    Returns:
        Dictionary with messages and pagination info
    """
    client, user_id, error = _get_authenticated_client()
    if error:
        return error

    logger.debug(f"get_channel_messages called by user {user_id} for channel {channel_id}")

    try:
        # Handle channel name format (e.g., '#general' -> lookup ID)
        if channel_id.startswith("#"):
            channel_name = channel_id[1:]
            channel_id = _resolve_channel_name(client, channel_name)
            if not channel_id:
                return {"ok": False, "error": f"Channel '{channel_name}' not found"}

        # Fetch conversation history
        kwargs = {"channel": channel_id, "limit": min(limit, 1000)}
        if cursor:
            kwargs["cursor"] = cursor

        response = client.conversations_history(**kwargs)

        if not response.get("ok"):
            return {"ok": False, "error": response.get("error", "Unknown error")}

        return {
            "ok": True,
            "messages": response.get("messages", []),
            "has_more": response.get("has_more", False),
            "next_cursor": response.get("response_metadata", {}).get("next_cursor"),
        }

    except SlackApiError as e:
        logger.error(
            f"Slack API error in get_channel_messages: {e.response.get('error', 'Unknown error')}"
        )
        return {
            "ok": False,
            "error": f"Slack API error: {e.response.get('error', 'Unknown error')}",
        }
    except Exception as e:
        logger.error(f"Error in get_channel_messages: {e}")
        return {"ok": False, "error": f"Error: {e!s}"}


def get_thread_replies(
    channel_id: str,
    thread_ts: str,
    limit: int = 100,
    cursor: Optional[str] = None,
) -> dict:
    """
    Get replies from a Slack thread.

    Uses the authenticated user's credentials from the current session context.

    Args:
        channel_id: Channel ID or name where the thread exists
        thread_ts: Timestamp of the parent message
        limit: Maximum number of replies to retrieve (default: 100, max: 1000)
        cursor: Pagination cursor from previous response

    Returns:
        Dictionary with messages (replies) and pagination info
    """
    client, user_id, error = _get_authenticated_client()
    if error:
        return error

    logger.debug(
        f"get_thread_replies called by user {user_id} for channel {channel_id}, thread {thread_ts}"
    )

    try:
        # Handle channel name format
        if channel_id.startswith("#"):
            channel_name = channel_id[1:]
            channel_id = _resolve_channel_name(client, channel_name)
            if not channel_id:
                return {"ok": False, "error": f"Channel '{channel_name}' not found"}

        # Fetch thread replies
        kwargs = {"channel": channel_id, "ts": thread_ts, "limit": min(limit, 1000)}
        if cursor:
            kwargs["cursor"] = cursor

        response = client.conversations_replies(**kwargs)

        if not response.get("ok"):
            return {"ok": False, "error": response.get("error", "Unknown error")}

        return {
            "ok": True,
            "messages": response.get("messages", []),
            "has_more": response.get("has_more", False),
            "next_cursor": response.get("response_metadata", {}).get("next_cursor"),
        }

    except SlackApiError as e:
        logger.error(
            f"Slack API error in get_thread_replies: {e.response.get('error', 'Unknown error')}"
        )
        return {
            "ok": False,
            "error": f"Slack API error: {e.response.get('error', 'Unknown error')}",
        }
    except Exception as e:
        logger.error(f"Error in get_thread_replies: {e}")
        return {"ok": False, "error": f"Error: {e!s}"}


def search_messages(
    query: str,
    count: int = 20,
    page: int = 1,
    from_user: Optional[str] = None,
    in_channel: Optional[str] = None,
    after_date: Optional[str] = None,
    before_date: Optional[str] = None,
    sort_by: str = "relevance",
    sort_order: str = "desc",
) -> dict:
    """
    Search for messages across all conversations with advanced filters.

    Uses the authenticated user's credentials from the current session context.

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
        Dictionary with search results

    Examples:
        # Search in last 7 days
        search_messages("important", after_date="7d")

        # Search from specific user in a channel
        search_messages("meeting", from_user="@john", in_channel="#team")

        # Date range search
        search_messages("report", after_date="2025-01-01", before_date="2025-01-31")
    """
    client, user_id, error = _get_authenticated_client()
    if error:
        return error

    try:
        # Parse dates if provided
        parsed_after = None
        parsed_before = None

        if after_date:
            parsed_after = _parse_date(after_date)
            if not parsed_after:
                return {"ok": False, "error": f"Invalid after_date format: {after_date}"}

        if before_date:
            parsed_before = _parse_date(before_date)
            if not parsed_before:
                return {"ok": False, "error": f"Invalid before_date format: {before_date}"}

        # Build enhanced search query
        enhanced_query = _build_search_query(
            base_query=query,
            from_user=from_user,
            in_channel=in_channel,
            after_date=parsed_after,
            before_date=parsed_before,
        )

        logger.debug(
            f"search_messages called by user {user_id} with enhanced query: {enhanced_query}"
        )

        # Slack API doesn't support sort_by/sort_order parameters, so we apply
        # client-side sorting to the current page of results only
        response = client.search_messages(
            query=enhanced_query,
            count=min(count, 100),
            page=page,
        )

        if not response.get("ok"):
            return {"ok": False, "error": response.get("error", "Unknown error")}

        messages_data = response.get("messages", {})
        matches = messages_data.get("matches", [])

        # Apply client-side sorting if requested and different from default
        if sort_by == "timestamp" and matches:
            reverse = sort_order == "desc"
            matches = sorted(matches, key=lambda m: float(m.get("ts", 0)), reverse=reverse)

        return {
            "ok": True,
            "query": enhanced_query,
            "filters": {
                "from_user": from_user,
                "in_channel": in_channel,
                "after_date": parsed_after,
                "before_date": parsed_before,
                "sort_by": sort_by,
                "sort_order": sort_order,
            },
            "matches": matches,
            "total": messages_data.get("total", 0),
            "page": messages_data.get("page", 1),
            "page_count": messages_data.get("page_count", 1),
        }

    except SlackApiError as e:
        logger.error(
            f"Slack API error in search_messages: {e.response.get('error', 'Unknown error')}"
        )
        return {
            "ok": False,
            "error": f"Slack API error: {e.response.get('error', 'Unknown error')}",
        }
    except Exception as e:
        logger.error(f"Error in search_messages: {e}")
        return {"ok": False, "error": f"Error: {e!s}"}


def get_users(
    user_id: Optional[str] = None,
    limit: int = 100,
    cursor: Optional[str] = None,
) -> dict:
    """
    Get users from Slack workspace.

    Dual-mode function:
    - Without user_id: Lists all users in the workspace with pagination
    - With user_id: Gets detailed profile for a specific user

    Uses the authenticated user's credentials from the current session context.

    Args:
        user_id: Optional user ID. If provided, gets specific user profile
        limit: Maximum number of users to retrieve when listing (default: 100, max: 1000)
        cursor: Pagination cursor from previous response (for listing mode)

    Returns:
        Dictionary with user(s) and pagination info
    """
    client, authenticated_user_id, error = _get_authenticated_client()
    if error:
        return error

    try:
        if user_id:
            # Get specific user profile
            logger.debug(f"get_users called by user {authenticated_user_id} for user {user_id}")
            response = client.users_info(user=user_id)

            if not response.get("ok"):
                return {"ok": False, "error": response.get("error", "Unknown error")}

            return {
                "ok": True,
                "user": response.get("user", {}),
            }
        else:
            # List all users
            logger.debug(f"get_users called by user {authenticated_user_id} to list users")
            kwargs = {"limit": min(limit, 1000)}
            if cursor:
                kwargs["cursor"] = cursor

            response = client.users_list(**kwargs)

            if not response.get("ok"):
                return {"ok": False, "error": response.get("error", "Unknown error")}

            return {
                "ok": True,
                "users": response.get("members", []),
                "next_cursor": response.get("response_metadata", {}).get("next_cursor"),
            }

    except SlackApiError as e:
        logger.error(f"Slack API error in get_users: {e.response.get('error', 'Unknown error')}")
        return {
            "ok": False,
            "error": f"Slack API error: {e.response.get('error', 'Unknown error')}",
        }
    except Exception as e:
        logger.error(f"Error in get_users: {e}")
        return {"ok": False, "error": f"Error: {e!s}"}


def get_channels(
    channel_id: Optional[str] = None,
    types: Optional[str] = None,
    limit: int = 100,
    cursor: Optional[str] = None,
    include_members: bool = False,
) -> dict:
    """
    Get channels from Slack workspace.

    Dual-mode function:
    - Without channel_id: Lists channels with optional type filter (defaults to public channels only)
    - With channel_id: Gets detailed info for a specific channel, optionally with members

    Uses the authenticated user's credentials from the current session context.

    Args:
        channel_id: Optional channel ID. If provided, gets specific channel info
        types: Filter by channel types when listing. Defaults to "public_channel" if not specified.
               Examples: "public_channel,private_channel", "im,mpim" (DMs and group DMs)
        limit: Maximum number of channels to retrieve when listing (default: 100, max: 1000)
        cursor: Pagination cursor from previous response (for listing mode)
        include_members: Include member list when getting specific channel (default: False)

    Returns:
        Dictionary with channel(s) and pagination info
    """
    client, authenticated_user_id, error = _get_authenticated_client()
    if error:
        return error

    try:
        if channel_id:
            # Get specific channel info
            logger.debug(
                f"get_channels called by user {authenticated_user_id} for channel {channel_id}"
            )
            response = client.conversations_info(channel=channel_id)

            if not response.get("ok"):
                return {"ok": False, "error": response.get("error", "Unknown error")}

            result = {
                "ok": True,
                "channel": response.get("channel", {}),
            }

            # Optionally include members
            if include_members:
                try:
                    # Fetch all members with pagination
                    all_members = []
                    members_cursor = None
                    while True:
                        kwargs = {"channel": channel_id}
                        if members_cursor:
                            kwargs["cursor"] = members_cursor
                        members_response = client.conversations_members(**kwargs)
                        if members_response.get("ok"):
                            all_members.extend(members_response.get("members", []))
                            members_cursor = members_response.get("response_metadata", {}).get(
                                "next_cursor"
                            )
                            if not members_cursor:
                                break
                        else:
                            break
                    result["members"] = all_members
                except SlackApiError as e:
                    logger.warning(
                        f"Failed to get members for channel {channel_id}: {e.response.get('error')}"
                    )
                    # Don't fail the whole request if members fetch fails
                    result["members_error"] = e.response.get("error", "Unknown error")

            return result
        else:
            # List all channels
            logger.debug(f"get_channels called by user {authenticated_user_id} to list channels")
            kwargs = {"limit": min(limit, 1000)}
            if cursor:
                kwargs["cursor"] = cursor
            if types:
                kwargs["types"] = types

            response = client.conversations_list(**kwargs)

            if not response.get("ok"):
                return {"ok": False, "error": response.get("error", "Unknown error")}

            return {
                "ok": True,
                "channels": response.get("channels", []),
                "next_cursor": response.get("response_metadata", {}).get("next_cursor"),
            }

    except SlackApiError as e:
        logger.error(f"Slack API error in get_channels: {e.response.get('error', 'Unknown error')}")
        return {
            "ok": False,
            "error": f"Slack API error: {e.response.get('error', 'Unknown error')}",
        }
    except Exception as e:
        logger.error(f"Error in get_channels: {e}")
        return {"ok": False, "error": f"Error: {e!s}"}
