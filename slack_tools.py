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

from auth.auth_info_middleware import (
    ALLOW_PRIVATE_CHANNELS_HEADER,
    WRITABLE_CHANNELS_HEADER,
    _parse_writable_channels,
)
from fastmcp.server.dependencies import get_access_token, get_http_request
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
    return {k: a[k] for k in ("text", "fallback", "author_name", "title") if a.get(k)}


def _extract_block_text(blocks: list) -> str:
    """Extract plain text from Slack Block Kit blocks.

    When a message uses Block Kit, the top-level `text` field is often empty
    and all content lives in `blocks`. This extracts a readable text fallback.

    Inline elements within a single rich_text_section are concatenated without
    newlines (they form one paragraph). Newlines separate sections, list items,
    and other block-level containers.
    """
    parts = []

    def _inline_text(elements) -> str:
        """Concatenate inline elements within a single section."""
        fragments = []
        for item in elements:
            item_type = item.get("type", "")
            if item_type == "text":
                fragments.append(item.get("text", ""))
            elif item_type == "link":
                fragments.append(item.get("url", ""))
            elif item_type == "user":
                fragments.append(f"<@{item.get('user_id', '')}>")
            elif item_type == "channel":
                fragments.append(f"<#{item.get('channel_id', '')}>")
        return "".join(fragments)

    _INLINE_CONTAINERS = {"rich_text_section", "rich_text_quote", "rich_text_preformatted"}

    def _walk_rich_text(elements):
        for item in elements:
            item_type = item.get("type", "")
            if item_type in _INLINE_CONTAINERS:
                if text := _inline_text(item.get("elements", [])):
                    parts.append(text)
            elif item.get("elements"):
                _walk_rich_text(item["elements"])

    for block in blocks:
        block_type = block.get("type", "")
        if block_type == "rich_text":
            _walk_rich_text(block.get("elements", []))
        elif block_type == "section":
            text_obj = block.get("text", {})
            if isinstance(text_obj, dict) and text_obj.get("text"):
                parts.append(text_obj["text"])
            for field in block.get("fields", []):
                if isinstance(field, dict) and field.get("text"):
                    parts.append(field["text"])
        elif block_type == "header":
            text_obj = block.get("text", {})
            if isinstance(text_obj, dict) and text_obj.get("text"):
                parts.append(text_obj["text"])
    return "\n".join(parts)


def _compact_message(msg: dict) -> dict:
    """Strip a Slack message to LLM-essential fields."""
    text = msg.get("text", "")
    # When text is empty but blocks carry the content, extract a fallback
    if not text.strip() and msg.get("blocks"):
        text = _extract_block_text(msg["blocks"])
    result = {
        "text": text,
        "user": msg.get("user", msg.get("bot_id", "")),
        "ts": msg.get("ts", ""),
    }
    for key in ("username", "thread_ts", "reply_count", "subtype"):
        if msg.get(key):
            result[key] = msg[key]
    if msg.get("edited"):
        result["edited"] = True
    if msg.get("reactions"):
        result["reactions"] = [{"name": r["name"], "count": r["count"]} for r in msg["reactions"]]
    if msg.get("attachments"):
        compact_attachments = [ca for a in msg["attachments"] if (ca := _compact_attachment(a))]
        if compact_attachments:
            result["attachments"] = compact_attachments
    if msg.get("files"):
        result["files"] = [
            {"name": f.get("name", ""), "filetype": f.get("filetype", "")} for f in msg["files"]
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
    profile = user.get("profile") or {}
    for field in ("display_name", "title", "status_text", "email"):
        if val := profile.get(field, ""):
            result[field] = val
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
    text = match.get("text", "")
    # Block Kit fallback — same as _compact_message
    if not text.strip() and match.get("blocks"):
        text = _extract_block_text(match["blocks"])
    result = {
        "text": text,
        "user": match.get("user", match.get("username", "")),
        "username": match.get("username", ""),
        "ts": match.get("ts", ""),
        "permalink": match.get("permalink", ""),
    }
    if match.get("thread_ts"):
        result["thread_ts"] = match["thread_ts"]
    channel = match.get("channel", {})
    if isinstance(channel, dict):
        result["channel"] = channel.get("name", channel.get("id", ""))
    if match.get("attachments"):
        compact_attachments = [ca for a in match["attachments"] if (ca := _compact_attachment(a))]
        if compact_attachments:
            result["attachments"] = compact_attachments
    return result


def _is_private_search_match(match: dict) -> bool:
    """Return True if a search match comes from a private channel, DM, or group DM."""
    # Slack tags some matches only at the top level (type "im"/"group"/"mpim")
    # rather than via nested channel flags, so check both.
    if match.get("type") in ("im", "group", "mpim"):
        return True
    channel = match.get("channel", {})
    if not isinstance(channel, dict):
        return False
    return bool(
        channel.get("is_private")
        or channel.get("is_im")
        or channel.get("is_mpim")
        or channel.get("is_group")
    )


def _is_channel_id(value: str) -> bool:
    return bool(value) and value[0] in ("C", "G") and value[1:].isalnum()


def _resolve_channel_id(client, channel_id: str) -> dict | None:
    """Return the channel dict from conversations_info, or None on failure."""
    try:
        resp = client.conversations_info(channel=channel_id)
        return resp.get("channel")
    except SlackApiError:
        return None


def _validate_writable_channel(
    channel: str,
    writable_channels: list[str] | None,
    channel_id: str | None = None,
) -> tuple[bool, str | None]:
    """Check whether the channel is in the writable allowlist.

    Matches by name and, when provided, by channel id, so an allowlist of ids
    authorizes id-addressed writes just as a name allowlist authorizes names.
    """
    if not writable_channels:
        return False, "No writable channels configured. Set the X-Writable-Channels header."
    normalized = channel.lstrip("#")
    candidates = {normalized}
    if channel_id:
        candidates.add(channel_id.lstrip("#"))
    if candidates & set(writable_channels):
        return True, None
    return False, (
        f"Channel '{normalized}' is not in the writable allowlist. "
        f"Allowed channels: {', '.join(writable_channels)}"
    )


_MCP_FOOTER = {
    "type": "context",
    "elements": [{"type": "mrkdwn", "text": "(Sent using Slack MCP)"}],
}


_SECTION_TEXT_LIMIT = 3000


def _build_blocks(text: str) -> list[dict]:
    if len(text) <= _SECTION_TEXT_LIMIT:
        return [
            {"type": "section", "text": {"type": "mrkdwn", "text": text}},
            _MCP_FOOTER,
        ]
    blocks = []
    remaining = text
    while remaining:
        if len(remaining) <= _SECTION_TEXT_LIMIT:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": remaining}})
            break
        split_at = remaining.rfind("\n", 0, _SECTION_TEXT_LIMIT)
        # Only when we split on a newline do we consume that single delimiter;
        # any further leading newlines are intentional paragraph breaks and stay.
        drop_delimiter = split_at > 0
        if split_at <= 0:
            split_at = remaining.rfind(" ", 0, _SECTION_TEXT_LIMIT)
        if split_at <= 0:
            split_at = _SECTION_TEXT_LIMIT
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": remaining[:split_at]}})
        remaining = remaining[split_at + 1 :] if drop_delimiter else remaining[split_at:]
    blocks.append(_MCP_FOOTER)
    return blocks


def _get_permalink(client, channel_id: str, message_ts: str) -> str | None:
    try:
        resp = client.chat_getPermalink(channel=channel_id, message_ts=message_ts)
        return resp.get("permalink")
    except SlackApiError:
        return None


def _validate_write_channel(
    client, channel: str, writable_channels: list[str] | None
) -> tuple[str, dict | None]:
    """Resolve, validate writability, and enforce public-only for a write operation.

    Returns (normalized_channel_name, error_dict_or_None).
    """
    normalized = channel.lstrip("#")
    channel_name = normalized
    channel_id = None
    public_only = _public_channels_only()

    if _is_channel_id(normalized):
        channel_id = normalized
        ch = _resolve_channel_id(client, normalized)
        if not ch:
            return normalized, {"ok": False, "error": f"Channel '{normalized}' not found"}
        if public_only and (
            ch.get("is_private") or ch.get("is_im") or ch.get("is_mpim") or ch.get("is_group")
        ):
            return normalized, {
                "ok": False,
                "error": f"Token-based auth does not allow access to private channels or DMs ({normalized})",
            }
        channel_name = ch.get("name", normalized)

    ok, err = _validate_writable_channel(channel_name, writable_channels, channel_id=channel_id)
    if not ok:
        return normalized, {"ok": False, "error": err}

    if public_only and not channel_id:
        target = _resolve_channel_name(client, normalized, public_only=True)
        if not target:
            return normalized, {"ok": False, "error": f"Channel '{normalized}' not found"}

    return normalized, None


def send_message(channel: str, text: str) -> dict:
    """Send a message to a Slack channel (must be in the writable allowlist)."""
    client, user_id, error = _get_authenticated_client()
    if error:
        return error

    writable_channels = _get_writable_channels()
    normalized, err = _validate_write_channel(client, channel, writable_channels)
    if err:
        return err

    try:
        response = client.chat_postMessage(
            channel=normalized, text=text, blocks=_build_blocks(text)
        )
        logger.info("send_message", extra={"user_id": user_id, "channel": normalized})
        result = {
            "ok": True,
            "ts": response["ts"],
            "channel": response["channel"],
        }
        if permalink := _get_permalink(client, response["channel"], response["ts"]):
            result["permalink"] = permalink
        return result
    except SlackApiError as e:
        logger.warning(
            "send_message failed",
            extra={"user_id": user_id, "channel": normalized, "error": e.response["error"]},
        )
        return {"ok": False, "error": f"Slack API error: {e.response['error']}"}


def reply_in_thread(channel: str, thread_ts: str, text: str) -> dict:
    """Reply in a Slack thread (channel must be in the writable allowlist)."""
    client, user_id, error = _get_authenticated_client()
    if error:
        return error

    writable_channels = _get_writable_channels()
    normalized, err = _validate_write_channel(client, channel, writable_channels)
    if err:
        return err

    try:
        response = client.chat_postMessage(
            channel=normalized, text=text, blocks=_build_blocks(text), thread_ts=thread_ts
        )
        logger.info(
            "reply_in_thread",
            extra={"user_id": user_id, "channel": normalized, "thread_ts": thread_ts},
        )
        result = {
            "ok": True,
            "ts": response["ts"],
            "channel": response["channel"],
        }
        if permalink := _get_permalink(client, response["channel"], response["ts"]):
            result["permalink"] = permalink
        return result
    except SlackApiError as e:
        logger.warning(
            "reply_in_thread failed",
            extra={"user_id": user_id, "channel": normalized, "error": e.response["error"]},
        )
        return {"ok": False, "error": f"Slack API error: {e.response['error']}"}


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


def _get_claims() -> dict:
    """Return the verified access token's claims, or {} outside a request.

    SlackOAuthProvider attaches slack_token/slack_user_id (and is_byok for
    raw-token callers) to the claims when it validates the bearer token.
    """
    try:
        access_token = get_access_token()
    except Exception:
        return {}
    return getattr(access_token, "claims", {}) or {}


def _get_writable_channels() -> list[str]:
    """Return the request's X-Writable-Channels allowlist, [] if absent."""
    try:
        http_request = get_http_request()
    except Exception:
        return []
    return _parse_writable_channels(http_request.headers.get(WRITABLE_CHANNELS_HEADER))


def _allow_private_channels() -> bool:
    """Return True if the request opts into private channels via header."""
    try:
        http_request = get_http_request()
    except Exception:
        return False
    raw_header = http_request.headers.get(ALLOW_PRIVATE_CHANNELS_HEADER, "")
    return raw_header.strip().lower() in ("true", "1", "yes")


def _public_channels_only() -> bool:
    """Return True if the caller should be restricted to public channels.

    BYOK callers are restricted by default unless X-Allow-Private-Channels is set.
    """
    if not _get_claims().get("is_byok"):
        return False
    return not _allow_private_channels()


def _check_public_channel(client, channel_id: str) -> dict | None:
    """Return an error dict if the channel is private/DM/group-DM, None if public."""
    try:
        resp = client.conversations_info(channel=channel_id)
    except SlackApiError as exc:
        return {
            "ok": False,
            "error": f"Cannot verify channel {channel_id}: {exc.response['error']}",
        }
    ch = resp.get("channel", {})
    if ch.get("is_private") or ch.get("is_im") or ch.get("is_mpim") or ch.get("is_group"):
        return {
            "ok": False,
            "error": f"Token-based auth does not allow access to private channels or DMs ({channel_id})",
        }
    return None


def _get_oauth21_client():
    """
    Get a Slack client from the verified access token's claims.

    In proxy mode, the Slack token was already validated during the OAuth
    exchange and is stored server-side; SlackOAuthProvider attaches it to the
    MCP token's claims.

    Returns:
        tuple: (client, user_id) or (None, None)
    """
    claims = _get_claims()
    slack_token = claims.get("slack_token")
    user_id = claims.get("slack_user_id")
    if slack_token and user_id:
        logger.debug(f"OAuth 2.1: Got Slack token from claims for user {user_id}")
        return WebClient(token=slack_token), user_id
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
    return None, None, {"ok": False, "error": "Not authenticated. Complete the OAuth flow first."}


def _resolve_channel_name(client, channel_name: str, *, public_only: bool = False) -> Optional[str]:
    """
    Resolve a channel name to its ID, paginating through all results.

    Args:
        client: Authenticated Slack client
        channel_name: Channel name (without #)
        public_only: If True, only resolve against public channels (prevents
            leaking private channel existence via distinct error messages).

    Returns:
        Channel ID if found, None otherwise
    """
    cursor = None
    types = "public_channel" if public_only else "public_channel,private_channel"
    while True:
        kwargs = {"types": types}
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
        public_only = _public_channels_only()
        if channel_id.startswith("#"):
            channel_name = channel_id[1:]
            channel_id = _resolve_channel_name(client, channel_name, public_only=public_only)
            if not channel_id:
                return {"ok": False, "error": f"Channel '{channel_name}' not found"}

        if public_only and (err := _check_public_channel(client, channel_id)):
            return err

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
        public_only = _public_channels_only()
        if channel_id.startswith("#"):
            channel_name = channel_id[1:]
            channel_id = _resolve_channel_name(client, channel_name, public_only=public_only)
            if not channel_id:
                return {"ok": False, "error": f"Channel '{channel_name}' not found"}

        if public_only and (err := _check_public_channel(client, channel_id)):
            return err

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

        response = client.search_messages(
            query=enhanced_query,
            count=min(count, 100),
            page=page,
            sort=sort_by if sort_by == "timestamp" else "score",
            sort_dir=sort_order,
        )

        if not response.get("ok"):
            return {"ok": False, "error": response.get("error", "Unknown error")}

        messages_data = response.get("messages", {})
        matches = messages_data.get("matches", [])

        public_only = _public_channels_only()
        if public_only:
            matches = [m for m in matches if not _is_private_search_match(m)]

        if compact:
            matches = [_compact_search_match(m) for m in matches]

        if public_only:
            total = len(matches)
            page = 1
            page_count = 1
        else:
            total = messages_data.get("total", 0)
            page = messages_data.get("page", 1)
            page_count = messages_data.get("page_count", 1)

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
            "total": total,
            "page": page,
            "page_count": page_count,
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
    query: Optional[str] = None,
    types: Optional[str] = None,
    limit: int = 100,
    cursor: Optional[str] = None,
    include_members: bool = False,
    compact: bool = True,
) -> dict:
    """
    Get channels from Slack workspace.

    Three modes, checked in order:
    - channel_id set: Gets detailed info for that specific channel, optionally with members
      (query is ignored)
    - query set: Searches all channels for a name match (see below)
    - neither set: Lists channels with optional type filter, one page per call

    Search mode paginates through the full conversations_list and filters client-side
    on a case-insensitive substring match, since Slack has no server-side name-search
    endpoint for non-admin tokens. It scans exhaustively like _resolve_channel_name
    (ignoring `cursor`) and returns up to `limit` matches plus a `truncated` flag
    instead of `next_cursor`.

    Uses the authenticated user's credentials from the current session context.

    Args:
        channel_id: Optional channel ID. If provided, gets specific channel info
        query: Optional substring to match against channel names (case-insensitive,
               '#' optional). Triggers search mode; ignored if channel_id is set.
        types: Filter by channel types. Defaults to "public_channel" if not specified.
               Examples: "public_channel,private_channel", "im,mpim" (DMs and group DMs)
        limit: Maximum number of channels to retrieve/match (default: 100, max: 1000)
        cursor: Pagination cursor from previous response (plain listing mode only)
        include_members: Include member list when getting specific channel (default: False)
        compact: If True (default), return only essential fields. False for full Slack API response.

    Returns:
        Dictionary with channel(s) and pagination info; shape depends on mode
    """
    client, authenticated_user_id, error = _get_authenticated_client()
    if error:
        return error

    try:
        public_only = _public_channels_only()
        if public_only:
            types = "public_channel"

        if channel_id:
            # Get specific channel info
            logger.debug(
                f"get_channels called by user {authenticated_user_id} for channel {channel_id}"
            )

            if public_only and (err := _check_public_channel(client, channel_id)):
                return err

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

        if query:
            # Search mode: scan every channel, filtering by substring match on name
            normalized_query = query.lstrip("#").lower()
            search_limit = min(limit, 1000)

            logger.debug(f"get_channels called by user {authenticated_user_id} to search channels")

            matches = []
            scan_cursor = None
            truncated = False

            while True:
                kwargs = {"limit": 1000}
                if types:
                    kwargs["types"] = types
                if scan_cursor:
                    kwargs["cursor"] = scan_cursor

                response = client.conversations_list(**kwargs)

                if not response.get("ok"):
                    return {"ok": False, "error": response.get("error", "Unknown error")}

                for channel in response.get("channels", []):
                    if normalized_query not in channel.get("name", "").lower():
                        continue
                    if len(matches) >= search_limit:
                        truncated = True
                        break
                    matches.append(channel)

                if truncated:
                    break

                scan_cursor = response.get("response_metadata", {}).get("next_cursor")
                if not scan_cursor:
                    break

            if compact:
                matches = [_compact_channel(c) for c in matches]

            return {
                "ok": True,
                "channels": matches,
                "truncated": truncated,
            }

        # List all channels (single page)
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
