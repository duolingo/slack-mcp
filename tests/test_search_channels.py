"""
Tests for slack_tools.get_channels' search mode (the `query` param).
"""

from unittest.mock import MagicMock, patch

import slack_tools
from slack_sdk.errors import SlackApiError


def _channel(id_, name):
    return {
        "id": id_,
        "name": name,
        "is_private": False,
        "is_archived": False,
        "is_member": True,
        "num_members": 1,
    }


class TestGetChannelsSearchMode:
    @patch("slack_tools._public_channels_only", return_value=False)
    @patch("slack_tools._get_authenticated_client")
    def test_matches_substring_case_insensitive(self, mock_auth, _mock_pco):
        client = MagicMock()
        mock_auth.return_value = (client, "user-1", None)
        client.conversations_list.return_value = {
            "ok": True,
            "channels": [
                _channel("chan-1", "engineering"),
                _channel("chan-2", "random"),
                _channel("chan-3", "eng-infra"),
            ],
            "response_metadata": {},
        }

        result = slack_tools.get_channels(query="ENG")

        assert result["ok"] is True
        assert [c["name"] for c in result["channels"]] == ["engineering", "eng-infra"]
        assert result["truncated"] is False

    @patch("slack_tools._public_channels_only", return_value=False)
    @patch("slack_tools._get_authenticated_client")
    def test_strips_hash_prefix(self, mock_auth, _mock_pco):
        client = MagicMock()
        mock_auth.return_value = (client, "user-1", None)
        client.conversations_list.return_value = {
            "ok": True,
            "channels": [_channel("chan-1", "general")],
            "response_metadata": {},
        }

        result = slack_tools.get_channels(query="#general")

        assert result["ok"] is True
        assert len(result["channels"]) == 1

    @patch("slack_tools._public_channels_only", return_value=False)
    @patch("slack_tools._get_authenticated_client")
    def test_paginates_across_multiple_pages(self, mock_auth, _mock_pco):
        client = MagicMock()
        mock_auth.return_value = (client, "user-1", None)
        client.conversations_list.side_effect = [
            {
                "ok": True,
                "channels": [_channel("chan-1", "team-eng")],
                "response_metadata": {"next_cursor": "page2"},
            },
            {
                "ok": True,
                "channels": [_channel("chan-2", "eng-help")],
                "response_metadata": {},
            },
        ]

        result = slack_tools.get_channels(query="eng")

        assert result["ok"] is True
        assert {c["name"] for c in result["channels"]} == {"team-eng", "eng-help"}
        assert client.conversations_list.call_count == 2
        assert client.conversations_list.call_args_list[1].kwargs["cursor"] == "page2"

    @patch("slack_tools._public_channels_only", return_value=False)
    @patch("slack_tools._get_authenticated_client")
    def test_truncates_at_limit(self, mock_auth, _mock_pco):
        client = MagicMock()
        mock_auth.return_value = (client, "user-1", None)
        client.conversations_list.return_value = {
            "ok": True,
            "channels": [_channel("chan-1", "eng-a"), _channel("chan-2", "eng-b")],
            "response_metadata": {},
        }

        result = slack_tools.get_channels(query="eng", limit=1)

        assert result["ok"] is True
        assert [c["name"] for c in result["channels"]] == ["eng-a"]
        assert result["truncated"] is True

    @patch("slack_tools._public_channels_only", return_value=False)
    @patch("slack_tools._get_authenticated_client")
    def test_no_match_returns_empty_list(self, mock_auth, _mock_pco):
        client = MagicMock()
        mock_auth.return_value = (client, "user-1", None)
        client.conversations_list.return_value = {
            "ok": True,
            "channels": [_channel("chan-1", "general")],
            "response_metadata": {},
        }

        result = slack_tools.get_channels(query="zzz-nonexistent")

        assert result["ok"] is True
        assert result["channels"] == []
        assert result["truncated"] is False

    @patch("slack_tools._public_channels_only", return_value=True)
    @patch("slack_tools._get_authenticated_client")
    def test_public_only_forces_public_channel_type(self, mock_auth, _mock_pco):
        client = MagicMock()
        mock_auth.return_value = (client, "user-1", None)
        client.conversations_list.return_value = {
            "ok": True,
            "channels": [],
            "response_metadata": {},
        }

        slack_tools.get_channels(query="eng", types="public_channel,private_channel")

        client.conversations_list.assert_called_once_with(limit=1000, types="public_channel")

    @patch("slack_tools._get_authenticated_client")
    def test_returns_auth_error(self, mock_auth):
        mock_auth.return_value = (None, None, {"ok": False, "error": "Not authenticated."})

        result = slack_tools.get_channels(query="eng")

        assert result["ok"] is False
        assert "Not authenticated" in result["error"]

    @patch("slack_tools._public_channels_only", return_value=False)
    @patch("slack_tools._get_authenticated_client")
    def test_returns_full_response_when_not_compact(self, mock_auth, _mock_pco):
        client = MagicMock()
        mock_auth.return_value = (client, "user-1", None)
        channel = {**_channel("chan-1", "engineering"), "creator": "user-2"}
        client.conversations_list.return_value = {
            "ok": True,
            "channels": [channel],
            "response_metadata": {},
        }

        result = slack_tools.get_channels(query="eng", compact=False)

        assert result["channels"][0] == channel

    @patch("slack_tools._public_channels_only", return_value=False)
    @patch("slack_tools._get_authenticated_client")
    def test_slack_api_error(self, mock_auth, _mock_pco):
        client = MagicMock()
        mock_auth.return_value = (client, "user-1", None)
        client.conversations_list.side_effect = SlackApiError(
            "rate_limited", {"error": "rate_limited"}
        )

        result = slack_tools.get_channels(query="eng")

        assert result["ok"] is False
        assert "rate_limited" in result["error"]

    @patch("slack_tools._public_channels_only", return_value=False)
    @patch("slack_tools._get_authenticated_client")
    def test_channel_id_takes_priority_over_query(self, mock_auth, _mock_pco):
        client = MagicMock()
        mock_auth.return_value = (client, "user-1", None)
        client.conversations_info.return_value = {
            "ok": True,
            "channel": _channel("chan-1", "engineering"),
        }

        result = slack_tools.get_channels(channel_id="chan-1", query="zzz-should-be-ignored")

        assert result["ok"] is True
        assert result["channel"]["name"] == "engineering"
        client.conversations_list.assert_not_called()
