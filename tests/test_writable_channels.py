from auth.auth_info_middleware import _parse_writable_channels


class TestParseWritableChannels:
    def test_parses_comma_separated_names(self):
        assert _parse_writable_channels("general,aaron-test") == ["general", "aaron-test"]

    def test_strips_whitespace(self):
        assert _parse_writable_channels(" general , aaron-test ") == ["general", "aaron-test"]

    def test_strips_hash_prefix(self):
        assert _parse_writable_channels("#general,#aaron-test") == ["general", "aaron-test"]

    def test_returns_empty_for_empty_string(self):
        assert _parse_writable_channels("") == []

    def test_returns_empty_for_none(self):
        assert _parse_writable_channels(None) == []

    def test_filters_empty_entries(self):
        assert _parse_writable_channels("general,,aaron-test,") == ["general", "aaron-test"]
