"""
Proxy OAuth Authorization Server for Slack MCP.

The MCP server acts as its own OAuth 2.1 authorization server and proxies
the Slack OAuth flow internally. MCP clients do standard OAuth 2.1 with
the MCP server (which publishes discoverable metadata), and the server
handles Slack authentication behind the scenes.

The xoxp-* Slack token never leaves the server. MCP clients only see
MCP-issued tokens.
"""

import asyncio
import json
import logging
import re
import secrets
import time
from html import escape as html_escape
from urllib.parse import quote

from fastmcp.server.auth.auth import AccessToken as FastMCPAccessToken
from fastmcp.server.auth.providers.in_memory import InMemoryOAuthProvider
from mcp.server.auth.provider import (
    AuthorizationCode,
    AuthorizationParams,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl, ValidationError
from slack_sdk import WebClient
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.routing import Route

from auth.token_auth import is_slack_token, resolve_slack_identity

logger = logging.getLogger(__name__)

# Allowed redirect URI patterns for lazily-registered clients.
# Loopback addresses (localhost / 127.0.0.1) and well-known editor custom
# schemes are accepted; everything else is rejected.
_ALLOWED_REDIRECT_URI_RE = re.compile(
    r"^("
    r"https?://localhost(:\d+)?(/.*)?"  # http(s)://localhost[:port][/path]
    r"|https?://127\.0\.0\.1(:\d+)?(/.*)?"  # http(s)://127.0.0.1[:port][/path]
    r"|cursor://[^\s]*"  # cursor:// custom scheme
    r"|vscode://[^\s]*"  # vscode:// custom scheme
    r")$",
    re.IGNORECASE,
)


class SlackOAuthProvider(InMemoryOAuthProvider):
    """
    OAuth 2.1 Authorization Server that proxies Slack OAuth internally.

    Flow:
    1. MCP client discovers endpoints via /.well-known/oauth-authorization-server
    2. MCP client calls /authorize
    3. Provider redirects user to Slack's OAuth page
    4. Slack redirects back to /oauth2callback with a code
    5. Provider exchanges code for xoxp-* token (stored server-side)
    6. Provider redirects client to its redirect_uri with an MCP auth code
    7. MCP client exchanges MCP auth code for MCP-issued access token
    8. Tool calls use MCP token; provider looks up stored xoxp-* token
    """

    # Override parent's 1-hour default to 30 days. Note: since all token state
    # is in-memory, the effective TTL is min(this value, time until next
    # restart/deploy).
    ACCESS_TOKEN_EXPIRY_SECONDS = 30 * 24 * 60 * 60  # 30 days

    def __init__(
        self,
        slack_client_id: str,
        slack_client_secret: str,
        slack_redirect_uri: str,
        slack_scopes: list[str],
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._slack_client_id = slack_client_id
        self._slack_client_secret = slack_client_secret
        self._slack_redirect_uri = slack_redirect_uri
        self._slack_scopes = slack_scopes

        # client_ids we synthesized via lazy registration, as opposed to real
        # DCR registrations whose redirect_uris are authoritative
        self._lazy_client_ids: set[str] = set()
        # internal_state -> {client_id, redirect_uri, state, code_challenge, scopes, created_at}
        self._pending_authorizations: dict[str, dict] = {}
        # MCP access_token string -> {token: "xoxp-...", user_id: "U..."}
        self._slack_tokens: dict[str, dict] = {}
        # TTL for pending authorizations and code-keyed slack tokens (10 minutes)
        self._pending_ttl = 600

    def _cleanup_expired(self):
        """Remove expired pending authorizations and stale Slack tokens."""
        now = time.time()
        expired_pending = [
            k
            for k, v in self._pending_authorizations.items()
            if now - v.get("created_at", 0) > self._pending_ttl
        ]
        for k in expired_pending:
            del self._pending_authorizations[k]

        # Clean up code-keyed entries (unredeemed auth codes)
        expired_codes = [
            k
            for k, v in self._slack_tokens.items()
            if k.startswith("code:") and now - v.get("created_at", 0) > self._pending_ttl
        ]
        for k in expired_codes:
            del self._slack_tokens[k]

        # Clean up access-token-keyed entries whose MCP token has expired
        # AND is no longer referenced by any refresh token (needed for refresh flow)
        refreshable = set(self._refresh_to_access_map.values())
        expired_access = [
            k
            for k in self._slack_tokens
            if not k.startswith("code:") and k not in self.access_tokens and k not in refreshable
        ]
        for k in expired_access:
            del self._slack_tokens[k]

        total_cleaned = len(expired_pending) + len(expired_codes) + len(expired_access)
        if total_cleaned:
            logger.debug(
                "Cleaned up %d expired pending auths, %d expired code tokens, "
                "%d expired access tokens",
                len(expired_pending),
                len(expired_codes),
                len(expired_access),
            )

    # ------------------------------------------------------------------
    # Lazy client re-registration
    # ------------------------------------------------------------------

    @staticmethod
    def _is_valid_client_id(client_id: str) -> bool:
        """Return True if *client_id* looks structurally plausible.

        We accept any non-empty printable ASCII string up to 128 chars that
        does **not** contain whitespace.  This is intentionally liberal —
        the only purpose is to reject obviously bogus values (empty, way too
        long, binary garbage) while allowing the wide variety of client IDs
        that real MCP clients generate.
        """
        if not client_id or len(client_id) > 128:
            return False
        # Must be printable ASCII with no whitespace.
        return bool(re.match(r"^[\x21-\x7e]+$", client_id))

    @staticmethod
    def _parse_allowed_redirect_uri(redirect_uri: str | None) -> AnyUrl | None:
        """Parse *redirect_uri* if it matches the lazy-registration allowlist.

        Returns None for disallowed values and for values that match the
        allowlist pattern but aren't valid URLs (e.g. a port above 65535) —
        those fall through unregistered so the SDK handler returns its normal
        invalid_request response instead of the parse error becoming a 500.
        """
        if not isinstance(redirect_uri, str) or not _ALLOWED_REDIRECT_URI_RE.match(redirect_uri):
            return None
        try:
            return AnyUrl(redirect_uri)
        except ValidationError:
            return None

    @staticmethod
    def _build_default_redirect_uris() -> list[AnyUrl]:
        """Return the set of redirect URIs assigned to lazily-registered clients."""
        raw = [
            "http://localhost",
            "http://127.0.0.1",
        ]
        return [AnyUrl(u) for u in raw]

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """Register a client, clearing any lazy-registration marker.

        A real DCR registration declares its own redirect_uris, so the client
        must no longer get the looser treatment lazy registrations receive.
        """
        await super().register_client(client_info)
        self._lazy_client_ids.discard(client_info.client_id)

    async def _register_lazy_client(
        self, client_id: str, redirect_uris: list[AnyUrl]
    ) -> OAuthClientInformationFull:
        """Register and return a synthesized client for an unknown client_id."""
        new_client = OAuthClientInformationFull(
            client_id=client_id,
            redirect_uris=redirect_uris,
            token_endpoint_auth_method="none",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            # Without this, the client's registered scope defaults to empty and
            # the upstream handler rejects any explicitly-requested scope with
            # invalid_scope. Grant everything this server itself supports —
            # the same default a fresh DCR registration would receive.
            scope=" ".join(sorted(self._slack_scopes)),
        )
        await self.register_client(new_client)
        self._lazy_client_ids.add(client_id)
        logger.info(
            "Lazily registered unknown client_id=%s with redirect_uris=%s",
            client_id,
            [str(u) for u in redirect_uris],
        )
        return new_client

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        """Look up a registered client, lazily registering unknown IDs.

        The in-memory store loses all client registrations on restart.  When
        a previously-registered client comes back with the same *client_id*,
        ``super().get_client()`` returns ``None`` and the framework responds
        with ``invalid_request — Client ID not found``.

        To survive this, we intercept the ``None`` case:
        * If the *client_id* passes basic structural validation we register a
          new ``OAuthClientInformationFull`` on the fly with redirect_uris
          restricted to loopback addresses and known editor schemes, then
          return it.
        * If the *client_id* is structurally invalid (empty, too long, binary
          junk) we return ``None`` so the caller can surface the proper
          ``invalid_client`` OAuth error.

        A client_id alone carries no redirect_uri, so this only ever assigns
        the bare loopback defaults. ``/authorize`` requests go through
        ``_preregister_client_for_authorize`` first, which registers using
        the caller's actual redirect_uri — real clients almost always use a
        specific port and path, not a bare loopback URI — before this method
        is reached.
        """
        client = await super().get_client(client_id)
        if client is not None:
            return client

        # Unknown client_id — try to lazily register.
        if not self._is_valid_client_id(client_id):
            return None

        return await self._register_lazy_client(client_id, self._build_default_redirect_uris())

    async def _preregister_client_for_authorize(self, request: Request) -> None:
        """Lazily register an unknown client_id — or extend a lazily-registered
        one — using this request's own redirect_uri, before the upstream
        handler validates it against the client's registered URIs.

        Without this, a reconnecting client whose registration was lost (e.g.
        a task recycle wiping the in-memory store) would only ever get
        ``get_client()``'s bare loopback defaults, which won't match a real
        client's redirect_uri — those almost always carry a specific port and
        callback path (e.g. ``http://localhost:53219/callback``), not a bare
        ``http://localhost``.

        Extending an existing lazy registration matters because the lazy
        registration often happens on a request that carries no redirect_uri
        at all: after a restart, the client's first contact is typically a
        /token refresh attempt, whose ``get_client()`` call registers the
        client with only the bare defaults. It also covers clients that pick
        a fresh ephemeral callback port on each authorization. Clients
        registered through real DCR are never extended — their declared
        redirect_uris stay authoritative.
        """
        # The SDK serves /authorize for both GET and POST; POST carries the
        # parameters in the form body. Starlette caches the parsed form on
        # the request, so the downstream handler's own form() read still works.
        if request.method == "POST":
            params = await request.form()
        else:
            params = request.query_params

        client_id = params.get("client_id")
        if not isinstance(client_id, str) or not self._is_valid_client_id(client_id):
            return

        existing = await super().get_client(client_id)
        if existing is not None and client_id not in self._lazy_client_ids:
            return  # real DCR registration — don't clobber its redirect_uris

        candidate = self._parse_allowed_redirect_uri(params.get("redirect_uri"))

        if existing is None:
            redirect_uris = self._build_default_redirect_uris()
            if candidate is not None and candidate not in redirect_uris:
                redirect_uris.append(candidate)
            await self._register_lazy_client(client_id, redirect_uris)
            return

        registered = existing.redirect_uris or []
        if candidate is None or candidate in registered:
            return
        await self._register_lazy_client(client_id, [*registered, candidate])

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """
        Redirect the user to Slack's OAuth page instead of auto-approving.

        Stores the pending authorization (client redirect_uri, state, code_challenge, scopes)
        keyed by an internal state token, then returns Slack's authorize URL.
        """
        self._cleanup_expired()

        internal_state = secrets.token_urlsafe(32)

        self._pending_authorizations[internal_state] = {
            "client_id": client.client_id,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "state": params.state,
            "code_challenge": params.code_challenge,
            "scopes": params.scopes or [],
            "resource": params.resource,
            "created_at": time.time(),
        }

        user_scopes = ",".join(self._slack_scopes)
        slack_auth_url = (
            f"https://slack.com/oauth/v2/authorize"
            f"?client_id={quote(self._slack_client_id, safe='')}"
            f"&user_scope={quote(user_scopes, safe='')}"
            f"&redirect_uri={quote(self._slack_redirect_uri, safe='')}"
            f"&state={quote(internal_state, safe='')}"
        )

        logger.info("Redirecting to Slack OAuth for client %s", client.client_id)
        return slack_auth_url

    def _error_redirect(self, pending: dict, error: str, description: str) -> HTMLResponse:
        """Redirect back to the MCP client's redirect_uri with error params.

        Uses the same HTML+JS redirect pattern as the success path so that
        custom-scheme URIs (cursor://) work in all browsers.
        """
        redirect_url = construct_redirect_uri(
            pending["redirect_uri"],
            error=error,
            error_description=description,
            state=pending["state"],
        )
        html_safe_url = html_escape(redirect_url, quote=True)
        js_safe_url = json.dumps(redirect_url).replace("</", r"<\/")
        return HTMLResponse(
            content=(
                "<!DOCTYPE html><html><head>"
                f"<meta http-equiv='refresh' content='0;url={html_safe_url}'>"
                "</head><body>"
                f"<p>Redirecting... <a href='{html_safe_url}'>Click here</a>.</p>"
                f"<script>window.location.href = {js_safe_url};</script>"
                "</body></html>"
            ),
        )

    async def _handle_slack_callback(self, request: Request):
        """
        Route handler for /oauth2callback. Receives Slack's authorization code,
        exchanges it for a xoxp-* token, stores it server-side, generates an
        MCP authorization code, and redirects to the MCP client's redirect_uri.
        """
        error = request.query_params.get("error")
        if error:
            logger.error("Slack OAuth error: %s", error)
            # Redirect back to the MCP client with error params (RFC 6749 §4.1.2.1)
            # so the client doesn't hang waiting for a callback that never arrives.
            internal_state = request.query_params.get("state")
            if internal_state:
                pending = self._pending_authorizations.pop(internal_state, None)
                if pending:
                    return self._error_redirect(
                        pending, "access_denied", f"Slack OAuth error: {error}"
                    )
            # Fallback if state is missing or pending auth not found
            return HTMLResponse(
                content=f"<h1>Slack OAuth Error</h1><p>{html_escape(error)}</p>",
                status_code=400,
            )

        slack_code = request.query_params.get("code")
        internal_state = request.query_params.get("state")

        if not slack_code or not internal_state:
            return HTMLResponse(
                content="<h1>Error</h1><p>Missing code or state parameter.</p>",
                status_code=400,
            )

        self._cleanup_expired()
        pending = self._pending_authorizations.pop(internal_state, None)
        if not pending:
            logger.error("Unknown or expired internal state: %s", internal_state)
            return HTMLResponse(
                content="<h1>Error</h1><p>Invalid or expired authorization state.</p>",
                status_code=400,
            )

        # Exchange Slack code for xoxp-* token
        try:
            slack_client = WebClient()
            response = await asyncio.to_thread(
                slack_client.oauth_v2_access,
                client_id=self._slack_client_id,
                client_secret=self._slack_client_secret,
                code=slack_code,
                redirect_uri=self._slack_redirect_uri,
            )

            if not response.get("ok"):
                slack_error = response.get("error", "unknown")
                logger.error("Slack oauth.v2.access failed: %s", slack_error)
                return self._error_redirect(
                    pending, "server_error", f"Slack token exchange failed: {slack_error}"
                )

            authed_user = response.get("authed_user", {})
            slack_token = authed_user.get("access_token")
            slack_user_id = authed_user.get("id")

            if not slack_token or not slack_user_id:
                logger.error("Missing user access_token or user ID in Slack response")
                return self._error_redirect(
                    pending, "server_error", "Slack did not return a valid user token or user ID"
                )

            logger.info("Got Slack token for user %s", slack_user_id)

        except Exception as e:
            logger.error("Error exchanging Slack code: %s", e, exc_info=True)
            return self._error_redirect(
                pending, "server_error", "Failed to exchange Slack authorization code"
            )

        # Generate an MCP authorization code
        client_id = pending["client_id"]
        client = await self.get_client(client_id)
        if not client:
            logger.error("Client %s not found during callback", client_id)
            return self._error_redirect(pending, "server_error", "Client not found")

        mcp_code_value = f"mcp_auth_{secrets.token_hex(20)}"

        auth_code = AuthorizationCode(
            code=mcp_code_value,
            client_id=client_id,
            redirect_uri=pending["redirect_uri"],
            redirect_uri_provided_explicitly=pending["redirect_uri_provided_explicitly"],
            scopes=pending["scopes"],
            expires_at=time.time() + 300,  # 5 minutes
            code_challenge=pending["code_challenge"],
            resource=pending.get("resource"),
        )
        self.auth_codes[mcp_code_value] = auth_code

        # Store the Slack token keyed by the MCP auth code temporarily;
        # it will be moved to the access token key during exchange
        self._slack_tokens[f"code:{mcp_code_value}"] = {
            "token": slack_token,
            "user_id": slack_user_id,
            "created_at": time.time(),
        }

        # Redirect to the MCP client's redirect_uri with the MCP auth code.
        # Use an HTML page with JavaScript redirect instead of a 302 redirect
        # because some browsers strip query parameters from custom protocol
        # scheme URLs (like cursor://) when handling server-side redirects.
        redirect_url = construct_redirect_uri(
            pending["redirect_uri"],
            code=mcp_code_value,
            state=pending["state"],
        )

        logger.info(
            "Redirecting to MCP client with auth code for user %s",
            slack_user_id,
        )

        # Use html_escape for HTML attributes, json.dumps for JS string.
        # html.escape() turns & → &amp; which is correct in HTML attributes
        # but inside <script>, JS doesn't decode HTML entities, so we need
        # json.dumps() to produce a safe JS string literal.
        html_safe_url = html_escape(redirect_url, quote=True)
        js_safe_url = json.dumps(redirect_url).replace("</", r"<\/")  # produces "..." with escaping

        html_content = (
            "<!DOCTYPE html><html><head>"
            f"<meta http-equiv='refresh' content='0;url={html_safe_url}'>"
            "</head><body>"
            "<p>Completing authentication... "
            f"<a href='{html_safe_url}'>Click here</a> if not redirected.</p>"
            f"<script>window.location.href = {js_safe_url};</script>"
            "</body></html>"
        )

        return HTMLResponse(content=html_content)

    def _extend_token_ttl(self, oauth_token: OAuthToken, context: str) -> OAuthToken:
        """Override the MCP access token TTL to ACCESS_TOKEN_EXPIRY_SECONDS.

        The parent issues tokens with a 1-hour default. This replaces the
        stored AccessToken with an updated expires_at and returns a new
        OAuthToken with the corresponding expires_in.
        """
        existing = self.access_tokens.get(oauth_token.access_token)
        if existing is None:
            logger.warning(
                "Parent did not store access token in self.access_tokens "
                "(%s) — skipping TTL override",
                context,
            )
            return oauth_token

        self.access_tokens[oauth_token.access_token] = existing.model_copy(
            update={"expires_at": int(time.time()) + self.ACCESS_TOKEN_EXPIRY_SECONDS}
        )
        oauth_token = OAuthToken(
            access_token=oauth_token.access_token,
            token_type=oauth_token.token_type,
            expires_in=self.ACCESS_TOKEN_EXPIRY_SECONDS,
            scope=oauth_token.scope,
            refresh_token=oauth_token.refresh_token,
        )
        logger.info(
            "Issued MCP access token (%s) with %d-day TTL",
            context,
            self.ACCESS_TOKEN_EXPIRY_SECONDS // 86400,
        )
        return oauth_token

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        """
        Exchange MCP auth code for MCP tokens, then associate the stored
        Slack token with the new MCP access token.
        """
        # Retrieve the Slack token stored during callback (don't remove yet —
        # if super() fails, we'd lose the token permanently)
        code_key = f"code:{authorization_code.code}"
        slack_info = self._slack_tokens.get(code_key)

        # Call parent to generate MCP tokens
        oauth_token = await super().exchange_authorization_code(client, authorization_code)
        oauth_token = self._extend_token_ttl(oauth_token, "auth code exchange")

        # Only now remove the code-keyed entry and associate with access token
        if slack_info:
            self._slack_tokens.pop(code_key, None)
            self._slack_tokens[oauth_token.access_token] = slack_info
            logger.debug(
                "Associated Slack token for user %s with MCP access token",
                slack_info.get("user_id"),
            )
        else:
            logger.warning(
                "No Slack token found for auth code %s — MCP token will lack Slack access",
                authorization_code.code[:8] + "...",
            )

        return oauth_token

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token,
        scopes: list[str],
    ) -> OAuthToken:
        """
        Exchange refresh token for new MCP tokens, then transfer the Slack
        token association to the new MCP access token.
        """
        # Find the old access token associated with this refresh token
        # (don't remove yet — if super() fails, we'd lose the token permanently)
        old_access_token = self._refresh_to_access_map.get(refresh_token.token)
        old_slack_info = None
        if old_access_token:
            old_slack_info = self._slack_tokens.get(old_access_token)

        # Call parent to generate new MCP tokens
        oauth_token = await super().exchange_refresh_token(client, refresh_token, scopes)
        oauth_token = self._extend_token_ttl(oauth_token, "refresh")

        # Only now remove old entry and transfer the Slack token to the new access token
        if old_slack_info:
            if old_access_token:
                self._slack_tokens.pop(old_access_token, None)
            self._slack_tokens[oauth_token.access_token] = old_slack_info
            logger.debug(
                "Transferred Slack token for user %s to new MCP access token",
                old_slack_info.get("user_id"),
            )
        else:
            logger.warning(
                "No Slack token found during refresh — new MCP token will lack Slack access"
            )

        return oauth_token

    async def verify_token(self, token: str) -> FastMCPAccessToken | None:
        """
        Verify a bearer token presented by an MCP client.

        In addition to MCP-issued tokens (the standard OAuth 2.1 proxy flow),
        a caller may supply a raw Slack token directly as the bearer credential
        (BYOK). A valid Slack token is validated via auth.test and surfaced to
        the tools through the same ``slack_token``/``slack_user_id`` claims used
        by the OAuth flow, so trusted callers can skip interactive OAuth.

        Any non-Slack bearer is delegated to the normal MCP token verification,
        so interactive OAuth clients are unaffected.
        """
        if is_slack_token(token):
            slack_user_id = await resolve_slack_identity(token)
            if not slack_user_id:
                return None
            return FastMCPAccessToken(
                token=token,
                client_id=slack_user_id,
                scopes=list(self._slack_scopes),
                expires_at=None,
                claims={
                    "slack_token": token,
                    "slack_user_id": slack_user_id,
                    "is_byok": True,
                },
            )
        return await super().verify_token(token)

    async def load_access_token(self, token: str):
        """
        Load access token and attach Slack token info to claims.

        The SDK's AccessToken (from mcp.server.auth.provider) has no `claims`
        field. We reconstruct using FastMCP's AccessToken which adds `claims: dict`.
        """
        access_token = await super().load_access_token(token)
        if access_token is None:
            return None

        # Reconstruct as FastMCP's AccessToken which has a `claims` dict.
        # super() may return the SDK's AccessToken (no claims field).
        claims = {}
        slack_info = self._slack_tokens.get(token)
        if slack_info:
            claims["slack_token"] = slack_info["token"]
            claims["slack_user_id"] = slack_info["user_id"]

        return FastMCPAccessToken(
            token=access_token.token,
            client_id=access_token.client_id,
            scopes=access_token.scopes,
            expires_at=access_token.expires_at,
            claims=claims,
        )

    def get_routes(self, **kwargs) -> list[Route]:
        """
        Get standard OAuth routes plus the Slack callback route.

        Also wraps the ``/authorize`` endpoint so that a genuinely invalid
        client_id surfaces the OAuth ``invalid_client`` error code instead
        of the generic ``invalid_request`` returned by the upstream handler.
        """
        routes = super().get_routes(**kwargs)

        # Wrap the /authorize route to fix the error code for invalid clients.
        wrapped: list[Route] = []
        for route in routes:
            if isinstance(route, Route) and route.path == "/authorize":
                original_endpoint = route.endpoint

                async def _authorize_wrapper(
                    request: Request,
                    _original=original_endpoint,
                ) -> HTMLResponse:
                    await self._preregister_client_for_authorize(request)
                    response = await _original(request)
                    # The upstream handler returns a 400 JSON body with
                    # ``"error": "invalid_request"`` when get_client() yields
                    # None.  Since our get_client() only returns None for
                    # structurally invalid client IDs, rewrite that to the
                    # more appropriate ``invalid_client``.
                    if getattr(response, "status_code", None) == 400:
                        try:
                            body = json.loads(response.body)
                            if (
                                body.get("error") == "invalid_request"
                                and "not found" in (body.get("error_description") or "").lower()
                            ):
                                body["error"] = "invalid_client"
                                return HTMLResponse(
                                    content=json.dumps(body),
                                    status_code=400,
                                    media_type="application/json",
                                    headers={"Cache-Control": "no-store"},
                                )
                        except (json.JSONDecodeError, AttributeError):
                            pass
                    return response

                wrapped.append(
                    Route(
                        route.path,
                        endpoint=_authorize_wrapper,
                        methods=route.methods,
                    )
                )
            else:
                wrapped.append(route)

        # Add the Slack OAuth callback route
        wrapped.append(
            Route(
                "/oauth2callback",
                endpoint=self._handle_slack_callback,
                methods=["GET"],
            )
        )

        return wrapped
