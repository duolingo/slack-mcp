"""
Session store for Slack MCP with security validation.

This module provides session management with immutable session-to-user bindings
to prevent cross-user credential access attacks.
"""

import logging
import secrets
import time
from threading import RLock
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class SlackSessionStore:
    """
    Session store for Slack OAuth tokens with security validation.

    Security Features:
    - Immutable session bindings (first auth wins, cannot be changed)
    - Session-to-user validation prevents cross-user access
    - Thread-safe with RLock
    """

    def __init__(self):
        # Maps user_id -> access_token
        self._user_tokens: Dict[str, str] = {}

        # Maps session_id -> user_id (immutable binding)
        self._session_bindings: Dict[str, str] = {}

        # OAuth state management (CSRF protection)
        # Maps state -> (session_id, timestamp)
        self._oauth_states: Dict[str, Tuple[str, float]] = {}

        # State expiration time (30 minutes)
        self._state_expiry_seconds = 1800

        # Thread safety
        self._lock = RLock()

    def store_user_token(
        self,
        user_id: str,
        access_token: str,
        session_id: Optional[str] = None,
    ) -> None:
        """
        Store user's Slack access token with optional session binding.

        Args:
            user_id: Slack user ID
            access_token: Slack access token (xoxp-...)
            session_id: Optional session ID to bind to this user

        Raises:
            ValueError: If session is already bound to a different user
        """
        with self._lock:
            # Store the token
            self._user_tokens[user_id] = access_token
            logger.info(f"Stored Slack token for user {user_id}")

            # Create immutable session binding if provided
            if session_id:
                if session_id not in self._session_bindings:
                    self._session_bindings[session_id] = user_id
                    logger.info(f"Created immutable session binding: {session_id} -> {user_id}")
                elif self._session_bindings[session_id] != user_id:
                    # Security: Attempt to bind session to different user
                    logger.error(
                        f"SECURITY: Attempt to rebind session {session_id} from "
                        f"{self._session_bindings[session_id]} to {user_id}"
                    )
                    raise ValueError(f"Session {session_id} is already bound to a different user")

    def get_user_token_with_validation(
        self,
        user_id: str,
        session_id: Optional[str] = None,
    ) -> Optional[str]:
        """
        Get user's access token with session validation.

        This method ensures that a session can only access tokens for its
        authenticated user, preventing cross-account access.

        Args:
            user_id: The user ID whose token is requested
            session_id: The current session ID

        Returns:
            Access token if validation passes, None otherwise
        """
        with self._lock:
            # Check if session is bound to a user
            if session_id:
                bound_user = self._session_bindings.get(session_id)
                if bound_user:
                    if bound_user != user_id:
                        logger.error(
                            f"SECURITY VIOLATION: Session {session_id} (bound to {bound_user}) "
                            f"attempted to access token for {user_id}"
                        )
                        return None
                    # Session binding matches, allow access
                    logger.debug(f"Session validation passed for {user_id}")
                    return self._user_tokens.get(user_id)
                else:
                    # Session not bound yet - this shouldn't happen in normal flow
                    logger.warning(
                        f"Session {session_id} not bound to any user. "
                        f"Denying access to {user_id}'s token."
                    )
                    return None
            else:
                # No session ID provided - deny access for security
                logger.warning(f"No session ID provided. Denying access to {user_id}'s token.")
                return None

    def get_user_by_session(self, session_id: str) -> Optional[str]:
        """
        Get user ID bound to a session.

        Args:
            session_id: Session ID

        Returns:
            User ID if session is bound, None otherwise
        """
        with self._lock:
            return self._session_bindings.get(session_id)

    def generate_oauth_state(self, session_id: str) -> str:
        """
        Generate a cryptographically secure OAuth state parameter.

        The state is bound to the session and expires after a timeout.

        Args:
            session_id: Session ID to bind the state to

        Returns:
            Cryptographically random state string
        """
        with self._lock:
            # Generate secure random state (32 bytes = 256 bits)
            state = secrets.token_urlsafe(32)

            # Store state with session and timestamp
            self._oauth_states[state] = (session_id, time.time())

            logger.info(f"Generated OAuth state for session {session_id}")
            return state

    def validate_and_consume_oauth_state(self, state: str, session_id: str) -> bool:
        """
        Validate OAuth state parameter and consume it (one-time use).

        This prevents CSRF attacks by ensuring:
        1. The state was generated by us
        2. The state is bound to the correct session
        3. The state hasn't expired
        4. The state can only be used once

        Args:
            state: OAuth state parameter from callback
            session_id: Current session ID

        Returns:
            True if state is valid, False otherwise
        """
        with self._lock:
            # Check if state exists
            if state not in self._oauth_states:
                logger.error(f"Invalid OAuth state: {state} not found")
                return False

            # Get stored session and timestamp
            stored_session, timestamp = self._oauth_states[state]

            # Check if state has expired
            if time.time() - timestamp > self._state_expiry_seconds:
                logger.error(f"OAuth state expired: {state}")
                del self._oauth_states[state]
                return False

            # Check if state is bound to the correct session
            if stored_session != session_id:
                logger.error(
                    f"SECURITY: OAuth state {state} bound to session {stored_session} "
                    f"but used with session {session_id}"
                )
                return False

            # State is valid - consume it (delete for one-time use)
            del self._oauth_states[state]
            logger.info(f"OAuth state validated and consumed for session {session_id}")
            return True

    def cleanup_expired_states(self) -> None:
        """Remove expired OAuth states."""
        with self._lock:
            current_time = time.time()
            expired = [
                state
                for state, (_, timestamp) in self._oauth_states.items()
                if current_time - timestamp > self._state_expiry_seconds
            ]
            for state in expired:
                del self._oauth_states[state]
            if expired:
                logger.info(f"Cleaned up {len(expired)} expired OAuth states")


# Global instance
_global_store = SlackSessionStore()


def get_session_store() -> SlackSessionStore:
    """Get the global session store instance."""
    return _global_store
