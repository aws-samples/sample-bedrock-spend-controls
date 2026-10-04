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

``iss`` and ``aud`` are enforced when configured; ``exp`` always is. The
first configured audience is the data plane's; any further entries are
honoured only when verifying for the admin routes (see ``AudienceScope``).
"""

import json
from dataclasses import dataclass
from typing import Literal
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import urlopen

import jwt as pyjwt

from .config import settings

USER_TOKEN_HEADER = "x-quota-user-token"  # nosec B105  # header name, not a credential

# Which route family a token is being verified for. ``JWT_AUDIENCE`` is a
# comma-separated list whose FIRST entry is the data-plane audience; any
# further entries (the admin console's public client id, appended by the
# stack) are accepted only on ``/admin/*``. A console login must never be
# enough to vend credentials on ``/v1/credentials``.
AudienceScope = Literal["data-plane", "admin"]

# Signature algorithms accepted per JWK key type when the JWKS entry does
# not advertise an ``alg`` of its own.
_ALGORITHMS_BY_KEY_TYPE = {
    "RSA": ["RS256", "RS384", "RS512"],
    "EC": ["ES256", "ES384", "ES512"],
}


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
        try:
            return self._jwks_client.get_signing_key_from_jwt(token)
        except pyjwt.PyJWTError as exc:
            raise JwtError(f"invalid token: {exc}") from exc

    @staticmethod
    def _allowed_algorithms(signing_key) -> list[str]:
        """Pin ``algorithms`` to what the resolved JWKS entry can verify.

        PyJWT raises a bare ``TypeError`` ("Expecting a PEM-formatted key")
        when a token's ``alg`` header names a family the key does not belong
        to (ES256 header against an RSA key). Pinning to the JWK's own
        ``alg``, or the key type's family, turns that into a clean
        ``InvalidAlgorithmError`` -> 401 and also rules out the other
        families for this key.
        """
        advertised = getattr(signing_key, "algorithm_name", None)
        if isinstance(advertised, str) and advertised:
            return [advertised]
        key_type = getattr(signing_key, "key_type", None)
        if key_type in _ALGORITHMS_BY_KEY_TYPE:
            return list(_ALGORITHMS_BY_KEY_TYPE[key_type])
        key = getattr(signing_key, "key", signing_key)
        from cryptography.hazmat.primitives.asymmetric import ec, rsa

        if isinstance(key, rsa.RSAPublicKey):
            return list(_ALGORITHMS_BY_KEY_TYPE["RSA"])
        if isinstance(key, ec.EllipticCurvePublicKey):
            return list(_ALGORITHMS_BY_KEY_TYPE["EC"])
        return [*_ALGORITHMS_BY_KEY_TYPE["RSA"], *_ALGORITHMS_BY_KEY_TYPE["EC"]]

    @staticmethod
    def accepted_audiences(scope: AudienceScope) -> list[str]:
        """Audiences a token may carry for the given route family.

        ``JWT_AUDIENCE`` is comma-separated: the first entry is the
        data-plane audience and the only one honoured on
        ``/v1/credentials``; the remaining entries (the admin console's
        public client id, when deployed) are additionally accepted on
        ``/admin/*``. With a single configured audience both scopes are
        identical. Empty = ``aud`` not enforced.
        """
        audiences = [
            audience.strip()
            for audience in settings.jwt_audience.split(",")
            if audience.strip()
        ]
        if scope == "admin" or not audiences:
            return audiences
        return audiences[:1]

    def verify(self, token: str, scope: AudienceScope = "data-plane") -> Identity:
        audiences = self.accepted_audiences(scope)
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
                signing_key = self._signing_key(token)
                claims = pyjwt.decode(
                    token,
                    getattr(signing_key, "key", signing_key),
                    algorithms=self._allowed_algorithms(signing_key),
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
        except (TypeError, ValueError) as e:
            # Key/algorithm family mismatches and malformed key material
            # surface from the crypto backend as plain Python errors; they
            # are a bad token, not a gateway fault.
            raise JwtError(f"invalid token: {e}")

        user_id = claims.get(settings.jwt_user_claim)
        if not user_id or not isinstance(user_id, str):
            raise JwtError(
                f"token is missing the '{settings.jwt_user_claim}' claim used as the user id"
            )
        return Identity(user_id=user_id, claims=claims)
