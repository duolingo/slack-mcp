from unittest.mock import MagicMock, patch

from slack_sdk.errors import SlackApiError

from auth.auth_info_middleware import _parse_writable_channels
from slack_tools import _MCP_FOOTER_ATTACHMENT, _validate_writable_channel, reply_in_thread, send_message


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

    @patch("slack_tools.get_context")
    @patch("slack_tools._get_authenticated_client")
    def test_sends_to_allowed_channel(self, mock_auth, mock_ctx):
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
            channel="random", text="hello", attachments=_MCP_FOOTER_ATTACHMENT
        )

    @patch("slack_tools.get_context")
    @patch("slack_tools._get_authenticated_client")
    def test_strips_hash_prefix(self, mock_auth, mock_ctx):
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
            channel="random", text="hello", attachments=_MCP_FOOTER_ATTACHMENT
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

    @patch("slack_tools.get_context")
    @patch("slack_tools._get_authenticated_client")
    def test_replies_to_allowed_channel(self, mock_auth, mock_ctx):
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
            channel="random", text="reply text", attachments=_MCP_FOOTER_ATTACHMENT, thread_ts="1234.5678"
        )

    @patch("slack_tools.get_context")
    @patch("slack_tools._get_authenticated_client")
    def test_returns_auth_error(self, mock_auth, mock_ctx):
        mock_auth.return_value = (None, None, {"ok": False, "error": "Not authenticated."})

        result = reply_in_thread("random", "1234.5678", "hello")
        assert result["ok"] is False
        assert "Not authenticated" in result["error"]
