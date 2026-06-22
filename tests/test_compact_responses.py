"""
Tests for compact response helpers that strip Slack API payloads
down to LLM-essential fields.
"""

from slack_tools import (
    _compact_attachment,
    _compact_channel,
    _compact_message,
    _compact_search_match,
    _compact_user,
    _extract_block_text,
)


class TestCompactAttachment:
    def test_keeps_essential_fields(self):
        attachment = {
            "text": "body",
            "fallback": "fb",
            "author_name": "alice",
            "title": "hello",
            "color": "#fff",
            "actions": [{"type": "button"}],
        }
        assert _compact_attachment(attachment) == {
            "text": "body",
            "fallback": "fb",
            "author_name": "alice",
            "title": "hello",
        }

    def test_drops_empty_fields(self):
        assert _compact_attachment({"text": "", "fallback": "fb"}) == {"fallback": "fb"}

    def test_returns_empty_for_no_essentials(self):
        assert _compact_attachment({"color": "#fff", "ts": "123"}) == {}


class TestExtractBlockText:
    def test_returns_empty_for_empty_blocks(self):
        assert _extract_block_text([]) == ""

    def test_extracts_rich_text_section(self):
        blocks = [
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [
                            {"type": "text", "text": "hello "},
                            {"type": "text", "text": "world"},
                        ],
                    }
                ],
            }
        ]
        assert _extract_block_text(blocks) == "hello world"

    def test_extracts_links_users_channels(self):
        blocks = [
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [
                            {"type": "link", "url": "https://example.com"},
                            {"type": "user", "user_id": "U123"},
                            {"type": "channel", "channel_id": "C456"},
                        ],
                    }
                ],
            }
        ]
        assert _extract_block_text(blocks) == "https://example.com<@U123><#C456>"

    def test_newlines_between_sections_not_within(self):
        blocks = [
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [
                            {"type": "text", "text": "paragraph one "},
                            {"type": "user", "user_id": "U123"},
                        ],
                    },
                    {
                        "type": "rich_text_section",
                        "elements": [
                            {"type": "text", "text": "paragraph two"},
                        ],
                    },
                ],
            }
        ]
        assert _extract_block_text(blocks) == "paragraph one <@U123>\nparagraph two"

    def test_recurses_into_rich_text_list(self):
        blocks = [
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_list",
                        "style": "bullet",
                        "elements": [
                            {
                                "type": "rich_text_section",
                                "elements": [{"type": "text", "text": "item one"}],
                            },
                            {
                                "type": "rich_text_section",
                                "elements": [{"type": "text", "text": "item two"}],
                            },
                        ],
                    }
                ],
            }
        ]
        assert _extract_block_text(blocks) == "item one\nitem two"

    def test_recurses_into_rich_text_quote(self):
        blocks = [
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_quote",
                        "elements": [{"type": "text", "text": "quoted text"}],
                    }
                ],
            }
        ]
        assert _extract_block_text(blocks) == "quoted text"

    def test_extracts_section_block_text(self):
        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "section body"},
            }
        ]
        assert _extract_block_text(blocks) == "section body"

    def test_extracts_section_block_fields(self):
        blocks = [
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": "field one"},
                    {"type": "mrkdwn", "text": "field two"},
                ],
            }
        ]
        assert _extract_block_text(blocks) == "field one\nfield two"

    def test_extracts_header_block(self):
        blocks = [{"type": "header", "text": {"type": "plain_text", "text": "Title"}}]
        assert _extract_block_text(blocks) == "Title"


class TestCompactMessage:
    def test_keeps_core_fields(self):
        msg = {
            "text": "hi",
            "user": "U1",
            "ts": "1.0",
            "blocks": [{"type": "section"}],
            "client_msg_id": "abc",
            "team": "T1",
        }
        assert _compact_message(msg) == {"text": "hi", "user": "U1", "ts": "1.0"}

    def test_falls_back_to_bot_id_when_no_user(self):
        result = _compact_message({"text": "hi", "bot_id": "B1", "ts": "1.0"})
        assert result["user"] == "B1"

    def test_includes_optional_fields_when_truthy(self):
        msg = {
            "text": "hi",
            "user": "U1",
            "ts": "1.0",
            "username": "alice",
            "thread_ts": "0.5",
            "reply_count": 3,
            "subtype": "thread_broadcast",
        }
        result = _compact_message(msg)
        assert result["username"] == "alice"
        assert result["thread_ts"] == "0.5"
        assert result["reply_count"] == 3
        assert result["subtype"] == "thread_broadcast"

    def test_omits_falsy_optional_fields(self):
        msg = {
            "text": "hi",
            "user": "U1",
            "ts": "1.0",
            "username": "",
            "thread_ts": None,
            "reply_count": 0,
            "subtype": "",
        }
        result = _compact_message(msg)
        for key in ("username", "thread_ts", "reply_count", "subtype"):
            assert key not in result

    def test_edited_collapsed_to_indicator(self):
        result = _compact_message(
            {"text": "hi", "user": "U1", "ts": "1.0", "edited": {"ts": "2.0", "user": "U1"}}
        )
        assert result["edited"] is True

    def test_reactions_compacted_to_name_and_count(self):
        msg = {
            "text": "hi",
            "user": "U1",
            "ts": "1.0",
            "reactions": [{"name": "thumbsup", "count": 2, "users": ["U1", "U2"]}],
        }
        result = _compact_message(msg)
        assert result["reactions"] == [{"name": "thumbsup", "count": 2}]

    def test_attachments_compacted_and_empty_filtered(self):
        msg = {
            "text": "hi",
            "user": "U1",
            "ts": "1.0",
            "attachments": [
                {"text": "body", "color": "#fff"},
                {"color": "#000"},
            ],
        }
        result = _compact_message(msg)
        assert result["attachments"] == [{"text": "body"}]

    def test_attachments_omitted_when_all_empty(self):
        msg = {
            "text": "hi",
            "user": "U1",
            "ts": "1.0",
            "attachments": [{"color": "#fff"}],
        }
        assert "attachments" not in _compact_message(msg)

    def test_files_compacted_to_name_and_filetype(self):
        msg = {
            "text": "hi",
            "user": "U1",
            "ts": "1.0",
            "files": [{"name": "doc.pdf", "filetype": "pdf", "url_private": "secret"}],
        }
        result = _compact_message(msg)
        assert result["files"] == [{"name": "doc.pdf", "filetype": "pdf"}]

    def test_block_kit_fallback_used_when_text_blank(self):
        msg = {
            "text": "",
            "user": "U1",
            "ts": "1.0",
            "blocks": [
                {
                    "type": "rich_text",
                    "elements": [
                        {
                            "type": "rich_text_section",
                            "elements": [{"type": "text", "text": "from blocks"}],
                        }
                    ],
                }
            ],
        }
        assert _compact_message(msg)["text"] == "from blocks"

    def test_block_kit_fallback_skipped_when_text_present(self):
        msg = {
            "text": "actual",
            "user": "U1",
            "ts": "1.0",
            "blocks": [
                {
                    "type": "rich_text",
                    "elements": [
                        {
                            "type": "rich_text_section",
                            "elements": [{"type": "text", "text": "from blocks"}],
                        }
                    ],
                }
            ],
        }
        assert _compact_message(msg)["text"] == "actual"

    def test_block_kit_fallback_for_section_fields(self):
        msg = {
            "text": "",
            "user": "U1",
            "ts": "1.0",
            "blocks": [
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": "Status: open"},
                        {"type": "mrkdwn", "text": "Priority: P1"},
                    ],
                }
            ],
        }
        assert _compact_message(msg)["text"] == "Status: open\nPriority: P1"


class TestCompactUser:
    def test_keeps_core_fields(self):
        user = {
            "id": "U1",
            "name": "alice",
            "real_name": "Alice Smith",
            "is_bot": False,
            "deleted": False,
            "tz": "America/Los_Angeles",
        }
        assert _compact_user(user) == {
            "id": "U1",
            "name": "alice",
            "real_name": "Alice Smith",
            "is_bot": False,
            "deleted": False,
        }

    def test_includes_profile_fields_when_truthy(self):
        user = {
            "id": "U1",
            "name": "alice",
            "real_name": "Alice Smith",
            "profile": {
                "display_name": "ali",
                "title": "Engineer",
                "status_text": "OOO",
                "email": "a@example.com",
                "image_72": "https://avatar/72",
            },
        }
        result = _compact_user(user)
        assert result["display_name"] == "ali"
        assert result["title"] == "Engineer"
        assert result["status_text"] == "OOO"
        assert result["email"] == "a@example.com"
        assert "image_72" not in result

    def test_omits_empty_profile_fields(self):
        user = {
            "id": "U1",
            "name": "alice",
            "profile": {"display_name": "", "title": "", "status_text": "", "email": ""},
        }
        result = _compact_user(user)
        for key in ("display_name", "title", "status_text", "email"):
            assert key not in result

    def test_handles_missing_profile(self):
        result = _compact_user({"id": "U1", "name": "alice"})
        assert result["id"] == "U1"
        assert "email" not in result

    def test_handles_null_profile(self):
        # Slack API may return "profile": null — user.get("profile", {}) returns
        # None in that case (the default only applies when the key is missing).
        result = _compact_user({"id": "U1", "name": "alice", "profile": None})
        assert result["id"] == "U1"
        assert "email" not in result


class TestCompactChannel:
    def test_keeps_core_fields(self):
        channel = {
            "id": "C1",
            "name": "general",
            "is_private": False,
            "is_archived": False,
            "is_member": True,
            "num_members": 10,
            "creator": "U1",
        }
        assert _compact_channel(channel) == {
            "id": "C1",
            "name": "general",
            "is_private": False,
            "is_archived": False,
            "is_member": True,
            "num_members": 10,
        }

    def test_extracts_topic_and_purpose_value(self):
        channel = {
            "id": "C1",
            "name": "general",
            "topic": {"value": "the topic", "creator": "U1"},
            "purpose": {"value": "the purpose", "creator": "U1"},
        }
        result = _compact_channel(channel)
        assert result["topic"] == "the topic"
        assert result["purpose"] == "the purpose"

    def test_omits_topic_when_empty(self):
        result = _compact_channel({"id": "C1", "name": "g", "topic": {"value": ""}})
        assert "topic" not in result


class TestCompactSearchMatch:
    def test_keeps_core_fields(self):
        match = {
            "text": "hi",
            "user": "U1",
            "username": "alice",
            "ts": "1.0",
            "permalink": "https://slack/x",
            "channel": {"id": "C1", "name": "general"},
            "team": "T1",
        }
        result = _compact_search_match(match)
        assert result["text"] == "hi"
        assert result["user"] == "U1"
        assert result["username"] == "alice"
        assert result["channel"] == "general"

    def test_user_falls_back_to_username_when_user_missing(self):
        result = _compact_search_match(
            {"text": "hi", "username": "alice", "ts": "1.0", "permalink": ""}
        )
        assert result["user"] == "alice"

    def test_channel_falls_back_to_id_when_name_missing(self):
        match = {"text": "hi", "ts": "1.0", "permalink": "", "channel": {"id": "C1"}}
        assert _compact_search_match(match)["channel"] == "C1"

    def test_thread_ts_included_when_present(self):
        match = {"text": "hi", "ts": "1.0", "permalink": "", "thread_ts": "0.5"}
        assert _compact_search_match(match)["thread_ts"] == "0.5"

    def test_block_kit_fallback_used_when_text_blank(self):
        match = {
            "text": "",
            "ts": "1.0",
            "permalink": "",
            "blocks": [
                {
                    "type": "rich_text",
                    "elements": [
                        {
                            "type": "rich_text_section",
                            "elements": [{"type": "text", "text": "from blocks"}],
                        }
                    ],
                }
            ],
        }
        assert _compact_search_match(match)["text"] == "from blocks"

    def test_attachments_compacted(self):
        match = {
            "text": "hi",
            "ts": "1.0",
            "permalink": "",
            "attachments": [{"text": "att body", "color": "#fff"}],
        }
        assert _compact_search_match(match)["attachments"] == [{"text": "att body"}]
