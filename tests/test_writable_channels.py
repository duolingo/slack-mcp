import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from auth.auth_info_middleware import AuthInfoMiddleware, _parse_writable_channels
from slack_sdk.errors import SlackApiError
from slack_tools import (
    _MCP_FOOTER,
    _SECTION_TEXT_LIMIT,
    _build_blocks,
    _is_channel_id,
    _validate_writable_channel,
    reply_in_thread,
    send_message,
)


class TestParseWritableChannels:
    def test_parses_comma_separated_names(self):
        assert _parse_writable_channels("random,test") == ["random", "test"]

    def test_strips_whitespace(self):
        assert _parse_writable_channels(" random , test ") == ["random", "test"]

    def test_strips_hash_prefix(self):
        assert _parse_writable_channels("#random,#test") == ["random", "test"]

    def test_returns_empty_for_empty_string(self):
        assert _parse_writable_channels("") == []

    def test_returns_empty_for_none(self):
        assert _parse_writable_channels(None) == []

    def test_filters_empty_entries(self):
        assert _parse_writable_channels("random,,test,") == ["random", "test"]


class TestAuthInfoMiddlewareListTools:
    @staticmethod
    def _tools():
        return [
            SimpleNamespace(name="slack_get_channel_messages"),
            SimpleNamespace(name="slack_search_messages"),
            SimpleNamespace(name="slack_send_message"),
        ]

    def test_hides_search_for_bot_tokens(self):
        middleware = AuthInfoMiddleware()

        async def call_next(_context):
            return self._tools()

        access_token = SimpleNamespace(claims={"slack_token": "xoxb-token"})

        with (
            patch("auth.auth_info_middleware.get_access_token", return_value=access_token),
            patch("auth.auth_info_middleware.get_http_request", side_effect=RuntimeError),
        ):
            tools = asyncio.run(middleware.on_list_tools(MagicMock(), call_next))

        assert [tool.name for tool in tools] == ["slack_get_channel_messages"]

    def test_keeps_search_for_user_tokens(self):
        middleware = AuthInfoMiddleware()

        async def call_next(_context):
            return self._tools()

        access_token = SimpleNamespace(claims={"slack_token": "xoxp-token"})

        with (
            patch("auth.auth_info_middleware.get_access_token", return_value=access_token),
            patch("auth.auth_info_middleware.get_http_request", side_effect=RuntimeError),
        ):
            tools = asyncio.run(middleware.on_list_tools(MagicMock(), call_next))

        assert [tool.name for tool in tools] == [
            "slack_get_channel_messages",
            "slack_search_messages",
        ]


class TestValidateWritableChannel:
    def test_exact_match(self):
        ok, err = _validate_writable_channel("random", ["random", "test"])
        assert ok is True
        assert err is None

    def test_strips_hash_prefix(self):
        ok, err = _validate_writable_channel("#random", ["random", "test"])
        assert ok is True
        assert err is None

    def test_rejects_unlisted_channel(self):
        ok, err = _validate_writable_channel("secret", ["random", "test"])
        assert ok is False
        assert "secret" in err
        assert "random" in err

    def test_rejects_empty_allowlist(self):
        ok, err = _validate_writable_channel("random", [])
        assert ok is False
        assert "No writable channels configured" in err

    def test_rejects_none_allowlist(self):
        ok, err = _validate_writable_channel("random", None)
        assert ok is False
        assert "No writable channels configured" in err

    def test_channel_id_not_matched_by_name(self):
        ok, err = _validate_writable_channel("C12345", ["random"])
        assert ok is False
        assert "C12345" in err

    def test_matches_by_channel_id_when_allowlist_uses_ids(self):
        ok, err = _validate_writable_channel("random", ["C09KPE8EACW"], channel_id="C09KPE8EACW")
        assert ok is True
        assert err is None


class TestSendMessage:
    @patch("slack_tools.get_context")
    @patch("slack_tools._get_authenticated_client")
    def test_rejects_unlisted_channel(self, mock_auth, mock_ctx):
        mock_auth.return_value = (MagicMock(), "U123", None)
        ctx = MagicMock()
        ctx.get_state.return_value = ["random"]
        mock_ctx.return_value = ctx

        result = send_message("secret", "hello")
        assert result["ok"] is False
        assert "secret" in result["error"]

    @patch("slack_tools.get_context")
    @patch("slack_tools._get_authenticated_client")
    def test_rejects_when_no_writable_channels(self, mock_auth, mock_ctx):
        mock_auth.return_value = (MagicMock(), "U123", None)
        ctx = MagicMock()
        ctx.get_state.return_value = None
        mock_ctx.return_value = ctx

        result = send_message("random", "hello")
        assert result["ok"] is False
        assert "No writable channels configured" in result["error"]

    @patch("slack_tools._public_channels_only", return_value=False)
    @patch("slack_tools.get_context")
    @patch("slack_tools._get_authenticated_client")
    def test_sends_to_allowed_channel(self, mock_auth, mock_ctx, _mock_byok):
        mock_client = MagicMock()
        mock_client.chat_postMessage.return_value = {
            "ok": True,
            "ts": "1234.5678",
            "channel": "C999",
        }
        mock_client.chat_getPermalink.return_value = {
            "ok": True,
            "permalink": "https://workspace.slack.com/archives/C999/p12345678",
        }
        mock_auth.return_value = (mock_client, "U123", None)
        ctx = MagicMock()
        ctx.get_state.return_value = ["random"]
        mock_ctx.return_value = ctx

        result = send_message("random", "hello")
        assert result["ok"] is True
        assert result["ts"] == "1234.5678"
        assert result["permalink"] == "https://workspace.slack.com/archives/C999/p12345678"
        mock_client.chat_postMessage.assert_called_once_with(
            channel="random", text="hello", blocks=_build_blocks("hello")
        )

    @patch("slack_tools._public_channels_only", return_value=False)
    @patch("slack_tools.get_context")
    @patch("slack_tools._get_authenticated_client")
    def test_strips_hash_prefix(self, mock_auth, mock_ctx, _mock_byok):
        mock_client = MagicMock()
        mock_client.chat_postMessage.return_value = {
            "ok": True,
            "ts": "1234.5678",
            "channel": "C999",
        }
        mock_auth.return_value = (mock_client, "U123", None)
        ctx = MagicMock()
        ctx.get_state.return_value = ["random"]
        mock_ctx.return_value = ctx

        result = send_message("#random", "hello")
        assert result["ok"] is True
        mock_client.chat_postMessage.assert_called_once_with(
            channel="random", text="hello", blocks=_build_blocks("hello")
        )

    @patch("slack_tools.get_context")
    @patch("slack_tools._get_authenticated_client")
    def test_returns_auth_error(self, mock_auth, mock_ctx):
        mock_auth.return_value = (None, None, {"ok": False, "error": "Not authenticated."})

        result = send_message("random", "hello")
        assert result["ok"] is False
        assert "Not authenticated" in result["error"]


class TestReplyInThread:
    @patch("slack_tools.get_context")
    @patch("slack_tools._get_authenticated_client")
    def test_rejects_unlisted_channel(self, mock_auth, mock_ctx):
        mock_auth.return_value = (MagicMock(), "U123", None)
        ctx = MagicMock()
        ctx.get_state.return_value = ["random"]
        mock_ctx.return_value = ctx

        result = reply_in_thread("secret", "1234.5678", "hello")
        assert result["ok"] is False
        assert "secret" in result["error"]

    @patch("slack_tools._public_channels_only", return_value=False)
    @patch("slack_tools.get_context")
    @patch("slack_tools._get_authenticated_client")
    def test_replies_to_allowed_channel(self, mock_auth, mock_ctx, _mock_byok):
        mock_client = MagicMock()
        mock_client.chat_postMessage.return_value = {
            "ok": True,
            "ts": "1234.9999",
            "channel": "C999",
        }
        mock_client.chat_getPermalink.return_value = {
            "ok": True,
            "permalink": "https://workspace.slack.com/archives/C999/p12349999",
        }
        mock_auth.return_value = (mock_client, "U123", None)
        ctx = MagicMock()
        ctx.get_state.return_value = ["random"]
        mock_ctx.return_value = ctx

        result = reply_in_thread("random", "1234.5678", "reply text")
        assert result["ok"] is True
        assert result["ts"] == "1234.9999"
        assert result["permalink"] == "https://workspace.slack.com/archives/C999/p12349999"
        mock_client.chat_postMessage.assert_called_once_with(
            channel="random",
            text="reply text",
            blocks=_build_blocks("reply text"),
            thread_ts="1234.5678",
        )

    @patch("slack_tools.get_context")
    @patch("slack_tools._get_authenticated_client")
    def test_returns_auth_error(self, mock_auth, mock_ctx):
        mock_auth.return_value = (None, None, {"ok": False, "error": "Not authenticated."})

        result = reply_in_thread("random", "1234.5678", "hello")
        assert result["ok"] is False
        assert "Not authenticated" in result["error"]


class TestIsChannelId:
    def test_recognizes_public_channel_id(self):
        assert _is_channel_id("C09KPE8EACW") is True

    def test_recognizes_private_channel_id(self):
        assert _is_channel_id("G01ABC123") is True

    def test_rejects_channel_name(self):
        assert _is_channel_id("random") is False

    def test_rejects_empty(self):
        assert _is_channel_id("") is False

    def test_rejects_hash_prefixed_name(self):
        assert _is_channel_id("#random") is False


class TestChannelIdResolution:
    @patch("slack_tools._public_channels_only", return_value=False)
    @patch("slack_tools.get_context")
    @patch("slack_tools._get_authenticated_client")
    def test_send_resolves_channel_id_to_name(self, mock_auth, mock_ctx, _mock_byok):
        mock_client = MagicMock()
        mock_client.conversations_info.return_value = {"channel": {"name": "random"}}
        mock_client.chat_postMessage.return_value = {
            "ok": True,
            "ts": "1234.5678",
            "channel": "C999",
        }
        mock_client.chat_getPermalink.return_value = {
            "ok": True,
            "permalink": "https://workspace.slack.com/archives/C999/p12345678",
        }
        mock_auth.return_value = (mock_client, "U123", None)
        ctx = MagicMock()
        ctx.get_state.return_value = ["random"]
        mock_ctx.return_value = ctx

        result = send_message("C999", "hello")
        assert result["ok"] is True
        mock_client.conversations_info.assert_called_once_with(channel="C999")
        mock_client.chat_postMessage.assert_called_once_with(
            channel="C999", text="hello", blocks=_build_blocks("hello")
        )

    @patch("slack_tools.get_context")
    @patch("slack_tools._get_authenticated_client")
    def test_send_rejects_channel_id_not_in_allowlist(self, mock_auth, mock_ctx):
        mock_client = MagicMock()
        mock_client.conversations_info.return_value = {"channel": {"name": "secret"}}
        mock_auth.return_value = (mock_client, "U123", None)
        ctx = MagicMock()
        ctx.get_state.return_value = ["random"]
        mock_ctx.return_value = ctx

        result = send_message("C999", "hello")
        assert result["ok"] is False
        assert "secret" in result["error"]

    @patch("slack_tools.get_context")
    @patch("slack_tools._get_authenticated_client")
    def test_send_returns_error_for_unknown_channel_id(self, mock_auth, mock_ctx):
        mock_client = MagicMock()
        mock_client.conversations_info.side_effect = SlackApiError(
            "channel_not_found", MagicMock(data={"error": "channel_not_found"})
        )
        mock_auth.return_value = (mock_client, "U123", None)
        ctx = MagicMock()
        ctx.get_state.return_value = ["random"]
        mock_ctx.return_value = ctx

        result = send_message("C000INVALID", "hello")
        assert result["ok"] is False
        assert "not found" in result["error"]

    @patch("slack_tools.get_context")
    @patch("slack_tools._get_authenticated_client")
    def test_send_returns_error_when_channel_has_no_name(self, mock_auth, mock_ctx):
        mock_client = MagicMock()
        mock_client.conversations_info.return_value = {"channel": {"id": "C999"}}
        mock_auth.return_value = (mock_client, "U123", None)
        ctx = MagicMock()
        ctx.get_state.return_value = ["random"]
        mock_ctx.return_value = ctx

        result = send_message("C999", "hello")
        assert result["ok"] is False
        assert "not found" in result["error"]
        mock_client.chat_postMessage.assert_not_called()

    @patch("slack_tools.get_context")
    @patch("slack_tools._get_authenticated_client")
    def test_send_allows_channel_id_when_allowlist_uses_ids(self, mock_auth, mock_ctx):
        # Allowlist holds the channel ID; the resolved name ("secret") is NOT listed.
        mock_client = MagicMock()
        mock_client.conversations_info.return_value = {"channel": {"name": "secret"}}
        mock_client.chat_postMessage.return_value = {
            "ok": True,
            "ts": "1234.5678",
            "channel": "C999",
        }
        mock_client.chat_getPermalink.return_value = {
            "ok": True,
            "permalink": "https://workspace.slack.com/archives/C999/p12345678",
        }
        mock_auth.return_value = (mock_client, "U123", None)
        ctx = MagicMock()
        ctx.get_state.return_value = ["C999"]
        mock_ctx.return_value = ctx

        result = send_message("C999", "hello")
        assert result["ok"] is True
        mock_client.chat_postMessage.assert_called_once_with(
            channel="C999", text="hello", blocks=_build_blocks("hello")
        )

    @patch("slack_tools._public_channels_only", return_value=False)
    @patch("slack_tools.get_context")
    @patch("slack_tools._get_authenticated_client")
    def test_reply_resolves_channel_id_to_name(self, mock_auth, mock_ctx, _mock_byok):
        mock_client = MagicMock()
        mock_client.conversations_info.return_value = {"channel": {"name": "random"}}
        mock_client.chat_postMessage.return_value = {
            "ok": True,
            "ts": "1234.9999",
            "channel": "C999",
        }
        mock_client.chat_getPermalink.return_value = {
            "ok": True,
            "permalink": "https://workspace.slack.com/archives/C999/p12349999",
        }
        mock_auth.return_value = (mock_client, "U123", None)
        ctx = MagicMock()
        ctx.get_state.return_value = ["random"]
        mock_ctx.return_value = ctx

        result = reply_in_thread("C999", "1234.5678", "reply text")
        assert result["ok"] is True
        mock_client.conversations_info.assert_called_once_with(channel="C999")
        mock_client.chat_postMessage.assert_called_once_with(
            channel="C999",
            text="reply text",
            blocks=_build_blocks("reply text"),
            thread_ts="1234.5678",
        )


class TestBuildBlocks:
    def test_short_text_single_section(self):
        blocks = _build_blocks("hello")
        assert len(blocks) == 2
        assert blocks[0] == {"type": "section", "text": {"type": "mrkdwn", "text": "hello"}}
        assert blocks[-1] == _MCP_FOOTER

    def test_splits_long_text_at_newline(self):
        first = "a" * 2999 + "\n"
        second = "b" * 100
        text = first + second
        assert len(text) > _SECTION_TEXT_LIMIT
        blocks = _build_blocks(text)
        assert len(blocks) == 3
        assert blocks[0]["text"]["text"] == first.rstrip("\n")
        assert blocks[1]["text"]["text"] == second
        assert blocks[-1] == _MCP_FOOTER

    def test_splits_long_text_at_space(self):
        first = "a" * 2999
        second = "b" * 100
        text = first + " " + second
        assert len(text) > _SECTION_TEXT_LIMIT
        blocks = _build_blocks(text)
        assert len(blocks) == 3
        assert blocks[0]["text"]["text"] == first
        assert blocks[1]["text"]["text"] == " " + second
        assert blocks[-1] == _MCP_FOOTER

    def test_preserves_paragraph_break_across_split(self):
        # A blank line (\n\n) straddling the split boundary must keep the
        # intentional paragraph break instead of collapsing all newlines.
        first = "a" * 2999
        second = "b" * 100
        text = first + "\n\n" + second
        assert len(text) > _SECTION_TEXT_LIMIT
        blocks = _build_blocks(text)
        assert len(blocks) == 3
        assert blocks[0]["text"]["text"] == first
        assert blocks[1]["text"]["text"] == "\n" + second
        assert blocks[-1] == _MCP_FOOTER

    def test_hard_splits_when_no_whitespace(self):
        text = "a" * (_SECTION_TEXT_LIMIT + 500)
        blocks = _build_blocks(text)
        assert len(blocks) == 3
        assert blocks[0]["text"]["text"] == "a" * _SECTION_TEXT_LIMIT
        assert blocks[1]["text"]["text"] == "a" * 500
        assert blocks[-1] == _MCP_FOOTER

    def test_exactly_at_limit_no_split(self):
        text = "a" * _SECTION_TEXT_LIMIT
        blocks = _build_blocks(text)
        assert len(blocks) == 2
        assert blocks[0]["text"]["text"] == text
        assert blocks[-1] == _MCP_FOOTER
