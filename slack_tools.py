"""
Slack MCP Tools
Provides tools for interacting with Slack conversations, users, and channels.

All tools use OAuth 2.1 authentication and automatically retrieve
the appropriate user's credentials from the token claims.
"""

import logging
import re
from datetime import datetime, timedelta
from typing import Optional

from fastmcp.server.dependencies import get_context
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Compact response helpers
# Reduce token usage by 80-91% by stripping Slack API responses to
# LLM-essential fields. Set compact=False on any tool to get full responses.
# ---------------------------------------------------------------------------


def _compact_attachment(a: dict) -> dict:
    """Strip a Slack attachment to LLM-essential fields."""
    ca = {}
    if a.get("text"):
        ca["text"] = a["text"]
    if a.get("fallback"):
        ca["fallback"] = a["fallback"]
    if a.get("author_name"):
        ca["author_name"] = a["author_name"]
    if a.get("title"):
        ca["title"] = a["title"]
    return ca


def _compact_message(msg: dict) -> dict:
    """Strip a Slack message to LLM-essential fields."""
    result = {
        "text": msg.get("text", ""),
        "user": msg.get("user", msg.get("bot_id", "")),
        "ts": msg.get("ts", ""),
    }
    if msg.get("username"):
        result["username"] = msg["username"]
    if msg.get("thread_ts"):
        result["thread_ts"] = msg["thread_ts"]
    if msg.get("reply_count"):
        result["reply_count"] = msg["reply_count"]
    if msg.get("subtype"):
        result["subtype"] = msg["subtype"]
    if msg.get("edited"):
        result["edited"] = True
    if msg.get("reactions"):
        result["reactions"] = [
            {"name": r["name"], "count": r["count"]} for r in msg["reactions"]
        ]
    if msg.get("attachments"):
        compact_attachments = [ca for a in msg["attachments"] if (ca := _compact_attachment(a))]
        if compact_attachments:
            result["attachments"] = compact_attachments
    if msg.get("files"):
        result["files"] = [
            {"name": f.get("name", ""), "filetype": f.get("filetype", "")}
            for f in msg["files"]
        ]
    return result


def _compact_user(user: dict) -> dict:
    """Strip a Slack user to LLM-essential fields."""
    result = {
        "id": user.get("id", ""),
        "name": user.get("name", ""),
        "real_name": user.get("real_name", ""),
        "is_bot": user.get("is_bot", False),
        "deleted": user.get("deleted", False),
    }
    profile = user.get("profile", {})
    if profile:
        display_name = profile.get("display_name", "")
        if display_name:
            result["display_name"] = display_name
        title = profile.get("title", "")
        if title:
            result["title"] = title
        status_text = profile.get("status_text", "")
        if status_text:
            result["status_text"] = status_text
        email = profile.get("email", "")
        if email:
            result["email"] = email
    return result


def _compact_channel(channel: dict) -> dict:
    """Strip a Slack channel to LLM-essential fields."""
    result = {
        "id": channel.get("id", ""),
        "name": channel.get("name", ""),
        "is_private": channel.get("is_private", False),
        "is_archived": channel.get("is_archived", False),
        "is_member": channel.get("is_member", False),
        "num_members": channel.get("num_members", 0),
    }
    topic = channel.get("topic", {})
    if isinstance(topic, dict) and topic.get("value"):
        result["topic"] = topic["value"]
    purpose = channel.get("purpose", {})
    if isinstance(purpose, dict) and purpose.get("value"):
        result["purpose"] = purpose["value"]
    return result


def _compact_search_match(match: dict) -> dict:
    """Strip a Slack search match to LLM-essential fields."""
    result = {
        "text": match.get("text", ""),
        "username": match.get("username", ""),
        "ts": match.get("ts", ""),
        "permalink": match.get("permalink", ""),
    }
    channel = match.get("channel", {})
    if isinstance(channel, dict):
        result["channel"] = channel.get("name", channel.get("id", ""))
    if match.get("attachments"):
        compact_attachments = [ca for a in match["attachments"] if (ca := _compact_attachment(a))]
        if compact_attachments:
            result["attachments"] = compact_attachments
    return result


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


def _get_oauth21_client():
    """
    Get a Slack client from OAuth 2.1 context (set by AuthInfoMiddleware from token claims).

    In proxy mode, the Slack token was already validated during the OAuth exchange
    and is stored server-side. AuthInfoMiddleware extracts it from token claims.

    Returns:
        tuple: (client, user_id) or (None, None)
    """
    try:
        ctx = get_context()
        if ctx:
            slack_token = ctx.get_state("slack_token")
            user_id = ctx.get_state("authenticated_user_id")
            if slack_token and user_id:
                logger.debug(f"OAuth 2.1: Got Slack token from context for user {user_id}")
                return WebClient(token=slack_token), user_id
    except Exception as e:
        logger.debug(f"OAuth 2.1: Could not get token from FastMCP context: {e}")

    return None, None


def _get_authenticated_client():
    """
    Get authenticated Slack client for current session via OAuth 2.1.

    Returns:
        tuple: (client, user_id, error_dict)
        - On success: (SlackClient, str, None)
        - On failure: (None, None, {"ok": False, "error": str})
    """
    client, user_id = _get_oauth21_client()
    if client and user_id:
        return client, user_id, None
    return None, None, {"ok": False, "error": "Not authenticated. Please authenticate via the /mcp menu (Slack MCP → Authenticate), then retry."}


def check_auth() -> dict:
    """
    Check if the current session has valid Slack authentication.

    Returns:
        Dictionary with authentication status
    """
    client, user_id = _get_oauth21_client()
    if client and user_id:
        return {"ok": True, "authenticated": True, "user_id": user_id}
    return {
        "ok": True,
        "authenticated": False,
        "error": "Not authenticated. Please authenticate via the /mcp menu (Slack MCP → Authenticate).",
    }


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
    compact: bool = True,
) -> dict:
    """
    Get messages from a Slack channel.

    Uses the authenticated user's credentials from the current session context.

    Args:
        channel_id: Channel ID or name (e.g., 'C1234567890' or '#general')
        limit: Maximum number of messages to retrieve (default: 100, max: 1000)
        cursor: Pagination cursor from previous response
        compact: If True (default), return only essential fields. False for full Slack API response.

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

        messages = response.get("messages", [])
        if compact:
            messages = [_compact_message(m) for m in messages]

        return {
            "ok": True,
            "messages": messages,
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
    compact: bool = True,
) -> dict:
    """
    Get replies from a Slack thread.

    Uses the authenticated user's credentials from the current session context.

    Args:
        channel_id: Channel ID or name where the thread exists
        thread_ts: Timestamp of the parent message
        limit: Maximum number of replies to retrieve (default: 100, max: 1000)
        cursor: Pagination cursor from previous response
        compact: If True (default), return only essential fields. False for full Slack API response.

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

        messages = response.get("messages", [])
        if compact:
            messages = [_compact_message(m) for m in messages]

        return {
            "ok": True,
            "messages": messages,
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
    compact: bool = True,
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
        compact: If True (default), return only essential fields. False for full Slack API response.

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

        if compact:
            matches = [_compact_search_match(m) for m in matches]

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
    compact: bool = True,
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
        compact: If True (default), return only essential fields. False for full Slack API response.

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

            user = response.get("user", {})
            if compact:
                user = _compact_user(user)

            return {
                "ok": True,
                "user": user,
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

            users = response.get("members", [])
            if compact:
                users = [_compact_user(u) for u in users]

            return {
                "ok": True,
                "users": users,
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
    compact: bool = True,
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
        compact: If True (default), return only essential fields. False for full Slack API response.

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

            channel = response.get("channel", {})
            if compact:
                channel = _compact_channel(channel)

            result = {
                "ok": True,
                "channel": channel,
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

            channels = response.get("channels", [])
            if compact:
                channels = [_compact_channel(c) for c in channels]

            return {
                "ok": True,
                "channels": channels,
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
