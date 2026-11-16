"""Who is calling. Two modes, both standards-only:

- **none**: no credential is read; every caller is the one owner `""`. For a server bound to
  loopback, or behind a port only a trusted proxy can reach (a NetworkPolicy).
- **jwt**: a bearer token (RFC 6750) checked as a JWT (RFC 7519) against an issuer's keys (a JWKS
  URL, or the issuer's OpenID discovery document): signature, `iss`, `aud`, `exp`, and `sub`
  present. The owner is `sub`. `scope` (RFC 8693 §4.2, RFC 9068 §2.2.3: space-separated) may be
  required to hold given values. No other claim is read, and no IdP is assumed.

The agent card stays public (A2A §8.2); the JSON-RPC endpoint refuses a caller without a valid
token with 401 and a `WWW-Authenticate: Bearer` challenge.
"""

import asyncio
import json
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field

import jwt
from starlette.authentication import (
    AuthCredentials,
    AuthenticationBackend,
    AuthenticationError,
    SimpleUser,
)
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse

ALGORITHMS = [
    "RS256",
    "RS384",
    "RS512",
    "PS256",
    "PS384",
    "PS512",
    "ES256",
    "ES384",
    "ES512",
    "EdDSA",
]


@dataclass
class JwtSettings:
    issuer: str
    audience: str
    jwks_url: str | None = None  # None: from the issuer's /.well-known/openid-configuration
    scopes: Sequence[str] = ()
    leeway: float = 30.0
    _jwks: jwt.PyJWKClient | None = field(default=None, init=False, repr=False)

    @property
    def discovery_url(self) -> str:
        return self.issuer.rstrip("/") + "/.well-known/openid-configuration"

    def keys(self) -> jwt.PyJWKClient:
        if self._jwks is None:
            url = self.jwks_url
            if url is None:
                with urllib.request.urlopen(self.discovery_url, timeout=10) as f:
                    url = json.load(f)["jwks_uri"]
            self._jwks = jwt.PyJWKClient(url, cache_keys=True, lifespan=300)
        return self._jwks

    def check(self, token: str) -> dict:
        """The token's claims if it is valid; `jwt.PyJWTError` (or `OSError` for unreachable keys)
        otherwise."""
        key = self.keys().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=ALGORITHMS,
            audience=self.audience,
            issuer=self.issuer,
            leeway=self.leeway,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
        granted = set(str(claims.get("scope", "")).split())
        if missing := [s for s in self.scopes if s not in granted]:
            raise jwt.InvalidTokenError(f"missing scope: {' '.join(missing)}")
        return claims


class JwtBackend(AuthenticationBackend):
    """Starlette authentication: no `Authorization` header is an anonymous caller (the card is
    public; `require_caller` refuses it on the RPC path); a bad token is an error."""

    def __init__(self, settings: JwtSettings) -> None:
        self.settings = settings

    async def authenticate(self, conn: HTTPConnection):
        header = conn.headers.get("authorization")
        if header is None:
            return None
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise AuthenticationError("expected a bearer token")
        try:
            claims = await asyncio.to_thread(self.settings.check, token.strip())
        except (jwt.PyJWTError, OSError, KeyError, ValueError) as e:
            raise AuthenticationError(f"invalid token: {e}") from None
        scopes = str(claims.get("scope", "")).split()
        return AuthCredentials(["authenticated", *scopes]), SimpleUser(str(claims["sub"]))


def unauthorized(conn: HTTPConnection, exc: Exception) -> JSONResponse:
    return JSONResponse(
        {"error": "unauthorized", "message": str(exc)},
        status_code=401,
        headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
    )


class RequireCaller:
    """ASGI: on `path`, a request without an authenticated user gets 401."""

    def __init__(self, app, path: str) -> None:
        self.app, self.path = app, path

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope["path"] == self.path:
            user = scope.get("user")
            if user is None or not user.is_authenticated:
                response = unauthorized(
                    HTTPConnection(scope), Exception("a bearer token is required")
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
