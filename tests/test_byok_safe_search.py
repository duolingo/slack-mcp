from unittest.mock import MagicMock, patch

import slack_tools
from slack_tools import _check_public_channel, _is_private_search_match


class TestIsPrivateSearchMatch:
    def test_public_channel(self):
        match = {"channel": {"id": "C123", "name": "general", "is_private": False}}
        assert _is_private_search_match(match) is False

    def test_private_channel(self):
        match = {"channel": {"id": "G123", "name": "secret", "is_private": True}}
        assert _is_private_search_match(match) is True

    def test_direct_message(self):
        match = {"channel": {"id": "D123", "is_im": True}}
        assert _is_private_search_match(match) is True

    def test_group_dm(self):
        match = {"channel": {"id": "G456", "is_mpim": True}}
        assert _is_private_search_match(match) is True

    def test_legacy_private_group_flag(self):
        match = {"channel": {"id": "G789", "name": "secret", "is_group": True}}
        assert _is_private_search_match(match) is True

    def test_top_level_type_im(self):
        match = {"type": "im", "channel": {"id": "D999"}}
        assert _is_private_search_match(match) is True

    def test_top_level_type_group(self):
        match = {"type": "group", "channel": {"id": "G999"}}
        assert _is_private_search_match(match) is True

    def test_missing_channel(self):
        assert _is_private_search_match({}) is False

    def test_channel_not_dict(self):
        assert _is_private_search_match({"channel": "C123"}) is False


class TestCheckPublicChannel:
    def test_allows_public_channel(self):
        client = MagicMock()
        client.conversations_info.return_value = {
            "ok": True,
            "channel": {"id": "C123", "is_private": False, "is_im": False, "is_mpim": False},
        }
        assert _check_public_channel(client, "C123") is None

    def test_blocks_private_channel(self):
        client = MagicMock()
        client.conversations_info.return_value = {
            "ok": True,
            "channel": {"id": "G123", "is_private": True},
        }
        err = _check_public_channel(client, "G123")
        assert err["ok"] is False
        assert "private channels or DMs" in err["error"]

    def test_blocks_dm(self):
        client = MagicMock()
        client.conversations_info.return_value = {
            "ok": True,
            "channel": {"id": "D123", "is_im": True},
        }
        err = _check_public_channel(client, "D123")
        assert err["ok"] is False
        assert "private channels or DMs" in err["error"]

    def test_blocks_group_dm(self):
        client = MagicMock()
        client.conversations_info.return_value = {
            "ok": True,
            "channel": {"id": "G456", "is_mpim": True},
        }
        err = _check_public_channel(client, "G456")
        assert err["ok"] is False
        assert "private channels or DMs" in err["error"]

    def test_blocks_legacy_private_group(self):
        client = MagicMock()
        client.conversations_info.return_value = {
            "ok": True,
            "channel": {"id": "G789", "is_group": True},
        }
        err = _check_public_channel(client, "G789")
        assert err["ok"] is False
        assert "private channels or DMs" in err["error"]


class TestByokSearchFiltering:
    """Verify that search results from private channels are filtered in BYOK mode."""

    def _make_search_response(self, matches):
        return {
            "ok": True,
            "messages": {
                "matches": matches,
                "total": len(matches),
                "page": 1,
                "page_count": 1,
            },
        }

    @patch("slack_tools._public_channels_only", return_value=True)
    @patch("slack_tools._get_authenticated_client")
    def test_filters_private_channels_in_byok_mode(self, mock_auth, mock_byok):
        client = MagicMock()
        mock_auth.return_value = (client, "U123", None)

        public_match = {
            "text": "public msg",
            "user": "U1",
            "ts": "1.0",
            "channel": {"id": "C1", "name": "general", "is_private": False},
        }
        private_match = {
            "text": "private msg",
            "user": "U2",
            "ts": "2.0",
            "channel": {"id": "G1", "name": "secret", "is_private": True},
        }
        dm_match = {
            "text": "dm msg",
            "user": "U3",
            "ts": "3.0",
            "channel": {"id": "D1", "is_im": True},
        }

        client.search_messages.return_value = self._make_search_response(
            [public_match, private_match, dm_match]
        )

        result = slack_tools.search_messages("test")

        assert result["ok"] is True
        assert len(result["matches"]) == 1
        assert result["matches"][0]["text"] == "public msg"

    @patch("slack_tools._public_channels_only", return_value=False)
    @patch("slack_tools._get_authenticated_client")
    def test_keeps_all_results_in_oauth_mode(self, mock_auth, mock_byok):
        client = MagicMock()
        mock_auth.return_value = (client, "U123", None)

        public_match = {
            "text": "public msg",
            "user": "U1",
            "ts": "1.0",
            "channel": {"id": "C1", "name": "general", "is_private": False},
        }
        private_match = {
            "text": "private msg",
            "user": "U2",
            "ts": "2.0",
            "channel": {"id": "G1", "name": "secret", "is_private": True},
        }

        client.search_messages.return_value = self._make_search_response(
            [public_match, private_match]
        )

        result = slack_tools.search_messages("test")

        assert result["ok"] is True
        assert len(result["matches"]) == 2


class TestByokChannelAccess:
    """Verify that BYOK mode blocks access to private channels for reads and writes."""

    @patch("slack_tools._public_channels_only", return_value=True)
    @patch("slack_tools._get_authenticated_client")
    def test_blocks_private_channel_messages(self, mock_auth, mock_byok):
        client = MagicMock()
        mock_auth.return_value = (client, "U123", None)
        client.conversations_info.return_value = {
            "ok": True,
            "channel": {"id": "G123", "is_private": True},
        }

        result = slack_tools.get_channel_messages("G123")

        assert result["ok"] is False
        assert "private channels or DMs" in result["error"]
        client.conversations_history.assert_not_called()

    @patch("slack_tools._public_channels_only", return_value=True)
    @patch("slack_tools._get_authenticated_client")
    def test_allows_public_channel_messages(self, mock_auth, mock_byok):
        client = MagicMock()
        mock_auth.return_value = (client, "U123", None)
        client.conversations_info.return_value = {
            "ok": True,
            "channel": {"id": "C123", "is_private": False, "is_im": False, "is_mpim": False},
        }
        client.conversations_history.return_value = {
            "ok": True,
            "messages": [{"text": "hello", "user": "U1", "ts": "1.0"}],
            "has_more": False,
        }

        result = slack_tools.get_channel_messages("C123")

        assert result["ok"] is True
        client.conversations_history.assert_called_once()

    @patch("slack_tools._public_channels_only", return_value=False)
    @patch("slack_tools._get_authenticated_client")
    def test_oauth_allows_private_channel_messages(self, mock_auth, mock_byok):
        client = MagicMock()
        mock_auth.return_value = (client, "U123", None)
        client.conversations_history.return_value = {
            "ok": True,
            "messages": [{"text": "secret", "user": "U1", "ts": "1.0"}],
            "has_more": False,
        }

        result = slack_tools.get_channel_messages("G123")

        assert result["ok"] is True
        client.conversations_info.assert_not_called()


class TestByokWriteAccess:
    """Verify that BYOK mode blocks writes to private channels."""

    @patch("slack_tools._public_channels_only", return_value=True)
    @patch("slack_tools.get_context")
    @patch("slack_tools._get_authenticated_client")
    def test_blocks_send_to_private_channel_by_id(self, mock_auth, mock_ctx, mock_byok):
        client = MagicMock()
        mock_auth.return_value = (client, "U123", None)

        ctx = MagicMock()
        ctx.get_state.return_value = ["secret"]
        mock_ctx.return_value = ctx

        client.conversations_info.return_value = {
            "ok": True,
            "channel": {"id": "G123", "name": "secret", "is_private": True},
        }

        result = slack_tools.send_message("G123", "hello")

        assert result["ok"] is False
        assert "private channels or DMs" in result["error"]
        client.chat_postMessage.assert_not_called()

    @patch("slack_tools._public_channels_only", return_value=True)
    @patch("slack_tools._resolve_channel_name", return_value=None)
    @patch("slack_tools.get_context")
    @patch("slack_tools._get_authenticated_client")
    def test_blocks_send_to_private_channel_by_name(
        self, mock_auth, mock_ctx, mock_resolve, mock_byok
    ):
        client = MagicMock()
        mock_auth.return_value = (client, "U123", None)

        ctx = MagicMock()
        ctx.get_state.return_value = ["secret"]
        mock_ctx.return_value = ctx

        result = slack_tools.send_message("secret", "hello")

        assert result["ok"] is False
        assert "not found" in result["error"]
        client.chat_postMessage.assert_not_called()

    @patch("slack_tools._public_channels_only", return_value=True)
    @patch("slack_tools.get_context")
    @patch("slack_tools._get_authenticated_client")
    def test_blocks_reply_to_private_channel_by_id(self, mock_auth, mock_ctx, mock_byok):
        client = MagicMock()
        mock_auth.return_value = (client, "U123", None)

        ctx = MagicMock()
        ctx.get_state.return_value = ["secret"]
        mock_ctx.return_value = ctx

        client.conversations_info.return_value = {
            "ok": True,
            "channel": {"id": "G123", "name": "secret", "is_private": True},
        }

        result = slack_tools.reply_in_thread("G123", "1234.5678", "hello")

        assert result["ok"] is False
        assert "private channels or DMs" in result["error"]
        client.chat_postMessage.assert_not_called()

    @patch("slack_tools._public_channels_only", return_value=True)
    @patch("slack_tools._resolve_channel_name", return_value=None)
    @patch("slack_tools.get_context")
    @patch("slack_tools._get_authenticated_client")
    def test_blocks_reply_to_private_channel_by_name(
        self, mock_auth, mock_ctx, mock_resolve, mock_byok
    ):
        client = MagicMock()
        mock_auth.return_value = (client, "U123", None)

        ctx = MagicMock()
        ctx.get_state.return_value = ["secret"]
        mock_ctx.return_value = ctx

        result = slack_tools.reply_in_thread("secret", "1234.5678", "hello")

        assert result["ok"] is False
        assert "not found" in result["error"]
        client.chat_postMessage.assert_not_called()

    @patch("slack_tools._public_channels_only", return_value=False)
    @patch("slack_tools.get_context")
    @patch("slack_tools._get_authenticated_client")
    def test_oauth_allows_send_to_private_channel(self, mock_auth, mock_ctx, mock_byok):
        client = MagicMock()
        mock_auth.return_value = (client, "U123", None)

        ctx = MagicMock()
        ctx.get_state.return_value = ["secret"]
        mock_ctx.return_value = ctx

        client.chat_postMessage.return_value = {
            "ok": True,
            "ts": "1.0",
            "channel": "G123",
        }

        result = slack_tools.send_message("secret", "hello")

        assert result["ok"] is True
        client.chat_postMessage.assert_called_once()


class TestAllowPrivateChannelsHeader:
    """Verify that X-Allow-Private-Channels overrides BYOK public-only default."""

    @patch("slack_tools._public_channels_only", return_value=False)
    @patch("slack_tools._get_authenticated_client")
    def test_byok_with_header_allows_private_channel_messages(self, mock_auth, _mock_pco):
        client = MagicMock()
        mock_auth.return_value = (client, "U123", None)
        client.conversations_history.return_value = {
            "ok": True,
            "messages": [{"text": "secret", "user": "U1", "ts": "1.0"}],
            "has_more": False,
        }

        result = slack_tools.get_channel_messages("G123")

        assert result["ok"] is True
        client.conversations_info.assert_not_called()

    @patch("slack_tools.get_context")
    def test_public_channels_only_returns_false_when_header_set(self, mock_ctx):
        ctx = MagicMock()
        ctx.get_state.side_effect = lambda key: {
            "is_byok": True,
            "allow_private_channels": True,
        }.get(key)
        mock_ctx.return_value = ctx

        assert slack_tools._public_channels_only() is False

    @patch("slack_tools.get_context")
    def test_public_channels_only_returns_true_when_header_not_set(self, mock_ctx):
        ctx = MagicMock()
        ctx.get_state.side_effect = lambda key: {
            "is_byok": True,
            "allow_private_channels": False,
        }.get(key)
        mock_ctx.return_value = ctx

        assert slack_tools._public_channels_only() is True

    @patch("slack_tools.get_context")
    def test_public_channels_only_returns_false_for_oauth(self, mock_ctx):
        ctx = MagicMock()
        ctx.get_state.side_effect = lambda key: {
            "is_byok": False,
            "allow_private_channels": False,
        }.get(key)
        mock_ctx.return_value = ctx

        assert slack_tools._public_channels_only() is False
