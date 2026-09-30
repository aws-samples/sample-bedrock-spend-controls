"""JWT authentication: the user identity comes from the customer's own IdP.

Instead of gateway-issued API keys, clients send the JWT their application
already uses (Cognito, Okta, Auth0, Entra ID, ... any OIDC issuer). The
gateway verifies it and takes the quota identity from a configurable claim
(default ``sub``).

Two verification modes:

- **JWKS (production)** — RS256/ES256 signatures verified against the
  issuer's published JWKS (``JWT_JWKS_URL``, discovered from ``JWT_ISSUER`` if
  not set). Keys are fetched once and cached by PyJWKClient.
- **Shared secret (dev/test)** — set ``JWT_SHARED_SECRET`` to verify HS256
  tokens without an IdP. Never use in production.

``iss`` and ``aud`` are enforced when configured; ``exp`` always is.
"""

import json
from dataclasses import dataclass
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import urlopen

import jwt as pyjwt

from .config import settings

USER_TOKEN_HEADER = "x-quota-user-token"  # nosec B105  # header name, not a credential


class JwtError(Exception):
    """Verification failed; .reason is safe to return to the caller."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Identity:
    user_id: str
    claims: dict


@dataclass(frozen=True)
class AdminPrincipal:
    """Verified routine-admin identity safe to persist in audit records."""

    actor: str
    auth_method: str


def extract_bearer(authorization_header: str | None) -> str | None:
    """Pull a bearer token out of an Authorization or x-api-key style value."""
    if not authorization_header:
        return None
    value = authorization_header.strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    return value or None


def extract_user_token(headers) -> str | None:
    """Read the end-user token without conflicting with SigV4 Authorization.

    ``X-Quota-User-Token`` is the canonical header for IAM-authenticated
    Function URLs. ``x-api-key`` and Bearer Authorization remain available
    for local development and deployments whose edge does not use SigV4.
    """
    dedicated = extract_bearer(headers.get(USER_TOKEN_HEADER))
    if dedicated:
        return dedicated
    api_key = extract_bearer(headers.get("x-api-key"))
    if api_key:
        return api_key
    authorization = headers.get("authorization")
    if authorization and authorization.lower().startswith("bearer "):
        return extract_bearer(authorization)
    return None


def _require_https(url: str, what: str) -> str:
    """Reject anything but an ``https://`` URL with a host.

    ``urlopen`` also understands ``file://`` and ``ftp://``; the issuer and
    JWKS locations are operator configuration, so an https-only check keeps
    a misconfigured or tampered setting from turning key discovery into a
    local file read or a plaintext fetch.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise JwtError(f"{what} must be an https:// URL")
    return url


def discover_jwks_url(issuer: str) -> str:
    """Resolve ``jwks_uri`` from the issuer's OIDC discovery document."""
    discovery_url = _require_https(
        issuer.rstrip("/") + "/.well-known/openid-configuration", "JWT issuer"
    )
    try:
        with urlopen(discovery_url, timeout=5) as response:  # nosec B310  # nosemgrep
            document = json.load(response)
    except (OSError, URLError, ValueError, TypeError) as exc:
        raise JwtError(f"could not load OIDC discovery document: {exc}") from exc
    jwks_uri = document.get("jwks_uri") if isinstance(document, dict) else None
    if not isinstance(jwks_uri, str) or not jwks_uri:
        raise JwtError("OIDC discovery document does not contain a valid jwks_uri")
    return _require_https(jwks_uri, "OIDC discovery jwks_uri")


class JwtVerifier:
    def __init__(self, jwks_client: "pyjwt.PyJWKClient | None" = None):
        self._jwks_client = jwks_client

    def _signing_key(self, token: str):
        if self._jwks_client is None:
            jwks_url = settings.jwt_jwks_url
            if not jwks_url and settings.jwt_issuer:
                jwks_url = discover_jwks_url(settings.jwt_issuer)
            if not jwks_url:
                raise JwtError(
                    "gateway is not configured with a JWT issuer/JWKS URL or shared secret"
                )
            _require_https(jwks_url, "JWT JWKS URL")
            self._jwks_client = pyjwt.PyJWKClient(jwks_url, cache_keys=True)
        return self._jwks_client.get_signing_key_from_jwt(token).key

    def verify(self, token: str) -> Identity:
        # Comma-separated audiences: the token must match any one of them.
        # A deployment may accept both the data-plane audience and the
        # admin UI's public client id from the same corporate IdP.
        audiences = [
            audience.strip()
            for audience in settings.jwt_audience.split(",")
            if audience.strip()
        ]
        options = {"require": ["exp"], "verify_aud": bool(audiences)}
        try:
            if settings.jwt_shared_secret:
                claims = pyjwt.decode(
                    token,
                    settings.jwt_shared_secret,
                    algorithms=["HS256"],
                    audience=audiences or None,
                    issuer=settings.jwt_issuer or None,
                    options=options,
                )
            else:
                claims = pyjwt.decode(
                    token,
                    self._signing_key(token),
                    algorithms=["RS256", "ES256", "RS384", "ES384", "RS512"],
                    audience=audiences or None,
                    issuer=settings.jwt_issuer or None,
                    options=options,
                )
        except pyjwt.ExpiredSignatureError:
            raise JwtError("token has expired")
        except pyjwt.InvalidAudienceError:
            raise JwtError("token audience does not match this gateway")
        except pyjwt.InvalidIssuerError:
            raise JwtError("token issuer does not match this gateway")
        except pyjwt.PyJWTError as e:
            raise JwtError(f"invalid token: {e}")

        user_id = claims.get(settings.jwt_user_claim)
        if not user_id or not isinstance(user_id, str):
            raise JwtError(
                f"token is missing the '{settings.jwt_user_claim}' claim used as the user id"
            )
        return Identity(user_id=user_id, claims=claims)
