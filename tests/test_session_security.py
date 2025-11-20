"""
Tests for session-based security validation.

These tests verify that the Slack MCP server properly prevents
cross-user access attacks through session validation.
"""

import pytest
from auth.session_store import SlackSessionStore


class TestSessionSecurity:
    """Test security properties of session management."""

    def test_immutable_session_binding(self):
        """Test that session bindings are immutable (first auth wins)."""
        store = SlackSessionStore()

        # User A authenticates with session_1
        store.store_user_token("user_a", "token_a", session_id="session_1")

        # Verify binding
        assert store.get_user_by_session("session_1") == "user_a"

        # User B tries to bind the same session (SECURITY VIOLATION)
        with pytest.raises(ValueError, match="already bound to a different user"):
            store.store_user_token("user_b", "token_b", session_id="session_1")

        # Verify session is still bound to user_a
        assert store.get_user_by_session("session_1") == "user_a"

    def test_cross_user_access_prevention(self):
        """Test that sessions cannot access other users' tokens."""
        store = SlackSessionStore()

        # User A authenticates with session_1
        store.store_user_token("user_a", "token_a", session_id="session_1")

        # User B authenticates with session_2
        store.store_user_token("user_b", "token_b", session_id="session_2")

        # Session 1 tries to access user B's token (ATTACK)
        token = store.get_user_token_with_validation("user_b", session_id="session_1")
        assert token is None, "Session should not access other user's token"

        # Session 2 tries to access user A's token (ATTACK)
        token = store.get_user_token_with_validation("user_a", session_id="session_2")
        assert token is None, "Session should not access other user's token"

        # Verify legitimate access works
        token_a = store.get_user_token_with_validation("user_a", session_id="session_1")
        assert token_a == "token_a"

        token_b = store.get_user_token_with_validation("user_b", session_id="session_2")
        assert token_b == "token_b"

    def test_no_session_denied(self):
        """Test that requests without session ID are denied."""
        store = SlackSessionStore()

        # User authenticates
        store.store_user_token("user_a", "token_a", session_id="session_1")

        # Try to access token without session ID
        token = store.get_user_token_with_validation("user_a", session_id=None)
        assert token is None, "Access without session should be denied"

    def test_unbound_session_denied(self):
        """Test that unbound sessions cannot access tokens."""
        store = SlackSessionStore()

        # User A authenticates with session_1
        store.store_user_token("user_a", "token_a", session_id="session_1")

        # Try to access with a session that was never bound
        token = store.get_user_token_with_validation("user_a", session_id="session_999")
        assert token is None, "Unbound session should be denied"

    def test_session_binding_persistence(self):
        """Test that session bindings persist across multiple operations."""
        store = SlackSessionStore()

        # User authenticates
        store.store_user_token("user_a", "token_a", session_id="session_1")

        # Update the same user's token (e.g., token refresh)
        store.store_user_token("user_a", "new_token_a", session_id="session_1")

        # Verify session is still bound correctly
        assert store.get_user_by_session("session_1") == "user_a"
        token = store.get_user_token_with_validation("user_a", session_id="session_1")
        assert token == "new_token_a"

    def test_multiple_sessions_same_user(self):
        """Test that one user can have multiple sessions."""
        store = SlackSessionStore()

        # User A authenticates from two different clients
        store.store_user_token("user_a", "token_a", session_id="session_1")
        store.store_user_token("user_a", "token_a", session_id="session_2")

        # Both sessions should work for user_a
        token1 = store.get_user_token_with_validation("user_a", session_id="session_1")
        token2 = store.get_user_token_with_validation("user_a", session_id="session_2")

        assert token1 == "token_a"
        assert token2 == "token_a"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
