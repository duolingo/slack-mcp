"""
Tests for search filter functionality.
"""

from datetime import datetime, timedelta
from unittest.mock import patch

from slack_tools import _build_search_query, _parse_date, _parse_relative_date

# Fixed datetime for testing to avoid race conditions
FIXED_NOW = datetime(2025, 6, 15, 12, 0, 0)


class TestParseRelativeDate:
    """Test cases for relative date parsing."""

    @patch("slack_tools.datetime")
    def test_parse_days(self, mock_datetime):
        """Test parsing days (e.g., '7d')."""
        mock_datetime.now.return_value = FIXED_NOW
        result = _parse_relative_date("7d")
        expected = (FIXED_NOW - timedelta(days=7)).strftime("%Y-%m-%d")
        assert result == expected

    @patch("slack_tools.datetime")
    def test_parse_weeks(self, mock_datetime):
        """Test parsing weeks (e.g., '2w')."""
        mock_datetime.now.return_value = FIXED_NOW
        result = _parse_relative_date("2w")
        expected = (FIXED_NOW - timedelta(weeks=2)).strftime("%Y-%m-%d")
        assert result == expected

    @patch("slack_tools.datetime")
    def test_parse_months(self, mock_datetime):
        """Test parsing months (e.g., '1m')."""
        mock_datetime.now.return_value = FIXED_NOW
        result = _parse_relative_date("1m")
        expected = (FIXED_NOW - timedelta(days=30)).strftime("%Y-%m-%d")
        assert result == expected

    @patch("slack_tools.datetime")
    def test_parse_years(self, mock_datetime):
        """Test parsing years (e.g., '1y')."""
        mock_datetime.now.return_value = FIXED_NOW
        result = _parse_relative_date("1y")
        expected = (FIXED_NOW - timedelta(days=365)).strftime("%Y-%m-%d")
        assert result == expected

    def test_parse_invalid_format(self):
        """Test that invalid formats return None."""
        assert _parse_relative_date("invalid") is None
        assert _parse_relative_date("7") is None
        assert _parse_relative_date("d7") is None
        assert _parse_relative_date("7x") is None

    @patch("slack_tools.datetime")
    def test_parse_case_insensitive(self, mock_datetime):
        """Test that parsing is case insensitive."""
        mock_datetime.now.return_value = FIXED_NOW
        result1 = _parse_relative_date("7d")
        result2 = _parse_relative_date("7D")
        assert result1 == result2


class TestParseDate:
    """Test cases for date parsing (absolute and relative)."""

    def test_parse_absolute_date(self):
        """Test parsing absolute dates."""
        result = _parse_date("2025-01-15")
        assert result == "2025-01-15"

    @patch("slack_tools.datetime")
    def test_parse_relative_date(self, mock_datetime):
        """Test parsing relative dates."""
        mock_datetime.now.return_value = FIXED_NOW
        result = _parse_date("7d")
        expected = (FIXED_NOW - timedelta(days=7)).strftime("%Y-%m-%d")
        assert result == expected

    def test_parse_invalid_date(self):
        """Test that invalid dates return None."""
        assert _parse_date("not-a-date") is None
        assert _parse_date("2025-13-01") is None  # Invalid month
        assert _parse_date("2025-01-32") is None  # Invalid day


class TestBuildSearchQuery:
    """Test cases for building Slack search queries."""

    def test_base_query_only(self):
        """Test query with only base search text."""
        result = _build_search_query("important meeting")
        assert result == "important meeting"

    def test_from_user_with_username(self):
        """Test adding from_user filter with username."""
        result = _build_search_query("meeting", from_user="john")
        assert result == "meeting from:@john"

    def test_from_user_with_at_symbol(self):
        """Test from_user filter when @ is already present."""
        result = _build_search_query("meeting", from_user="@john")
        assert result == "meeting from:@john"

    def test_from_user_with_id(self):
        """Test from_user filter with user ID."""
        result = _build_search_query("meeting", from_user="U123ABC")
        assert result == "meeting from:U123ABC"

    def test_in_channel_with_name(self):
        """Test adding in_channel filter with channel name."""
        result = _build_search_query("meeting", in_channel="general")
        assert result == "meeting in:#general"

    def test_in_channel_with_hash(self):
        """Test in_channel filter when # is already present."""
        result = _build_search_query("meeting", in_channel="#general")
        assert result == "meeting in:#general"

    def test_in_channel_with_id(self):
        """Test in_channel filter with channel ID."""
        result = _build_search_query("meeting", in_channel="C123ABC")
        assert result == "meeting in:C123ABC"

    def test_after_date(self):
        """Test adding after_date filter."""
        result = _build_search_query("meeting", after_date="2025-01-01")
        assert result == "meeting after:2025-01-01"

    def test_before_date(self):
        """Test adding before_date filter."""
        result = _build_search_query("meeting", before_date="2025-12-31")
        assert result == "meeting before:2025-12-31"

    def test_date_range(self):
        """Test date range with both after and before."""
        result = _build_search_query("report", after_date="2025-01-01", before_date="2025-01-31")
        assert "after:2025-01-01" in result
        assert "before:2025-01-31" in result

    def test_all_filters_combined(self):
        """Test combining all filters."""
        result = _build_search_query(
            "important",
            from_user="john",
            in_channel="general",
            after_date="2025-01-01",
            before_date="2025-01-31",
        )
        assert "important" in result
        assert "from:@john" in result
        assert "in:#general" in result
        assert "after:2025-01-01" in result
        assert "before:2025-01-31" in result

    def test_empty_base_query(self):
        """Test query with only filters, no base text."""
        result = _build_search_query("", from_user="john", after_date="2025-01-01")
        assert "from:@john" in result
        assert "after:2025-01-01" in result
