"""
Token-based (BYOK) authentication for the Slack MCP server.

Provides an alternative to the interactive OAuth 2.1 flow: a caller may
authenticate by supplying a Slack token directly as the ``Authorization:
Bearer <slack-token>`` credential. This is intended for trusted or scripted
deployments (e.g. ai-agents-backend) rather than interactive MCP clients.

Any Slack token type (``xoxp-`` user, ``xoxb-`` bot, etc.) is accepted. A token
used here should be scoped *without* private-channel access, since token auth
performs no per-user OAuth authorization and the server cannot otherwise prevent
reads of private conversations the token can see.
"""

import asyncio
import logging
import time

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

logger = logging.getLogger(__name__)

_SLACK_IDENTITY_CACHE_TTL_SECONDS = 10 * 60
_SLACK_IDENTITY_CACHE_MAX_SIZE = 1024
_slack_identity_cache: dict[str, tuple[float, str | None]] = {}


def is_slack_token(token: str | None) -> bool:
    """Return True if the value looks like a Slack token (``xox*``)."""
    return bool(token) and token.startswith("xox")


def is_slack_bot_token(token: str | None) -> bool:
    """Return True if the value looks like a Slack bot token."""
    return bool(token) and token.startswith("xoxb-")


def _cache_slack_identity(token: str, identity: str | None) -> None:
    now = time.monotonic()
    expired_tokens = [
        cached_token
        for cached_token, (expires_at, _) in _slack_identity_cache.items()
        if expires_at <= now
    ]
    for cached_token in expired_tokens:
        _slack_identity_cache.pop(cached_token, None)

    while token not in _slack_identity_cache and (
        len(_slack_identity_cache) >= _SLACK_IDENTITY_CACHE_MAX_SIZE
    ):
        oldest_token = next(iter(_slack_identity_cache))
        _slack_identity_cache.pop(oldest_token, None)

    _slack_identity_cache[token] = (now + _SLACK_IDENTITY_CACHE_TTL_SECONDS, identity)


async def resolve_slack_identity(token: str) -> str | None:
    """Validate a Slack token via ``auth.test`` and return its Slack identity.

    Returns the authenticated user (or bot) id on success, or None if the token
    is invalid or the call fails.
    """
    now = time.monotonic()
    cached_identity = _slack_identity_cache.get(token)
    if cached_identity:
        expires_at, identity = cached_identity
        if expires_at > now:
            return identity
        _slack_identity_cache.pop(token, None)

    client = WebClient(token=token)
    try:
        response = await asyncio.to_thread(client.auth_test)
    except SlackApiError as exc:
        logger.warning(
            "Slack token rejected by auth.test: %s",
            exc.response.get("error", "unknown error"),
        )
        _cache_slack_identity(token, None)
        return None

    if not response["ok"]:
        logger.warning("Slack token rejected by auth.test: %s", response.get("error"))
        _cache_slack_identity(token, None)
        return None

    identity = response.get("user_id") or response.get("bot_id")
    _cache_slack_identity(token, identity)
    return identity
