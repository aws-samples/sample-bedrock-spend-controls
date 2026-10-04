from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from examples import refreshable_bedrock as refreshable_module
from examples.refreshable_bedrock import (
    BrokerCredentialError,
    QuotaBrokerCredentialProvider,
)


class Clock:
    def __init__(self):
        self.now = datetime(2030, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


def _response(clock: Clock, key: str, lease_id: str) -> dict:
    return {
        "aws_access_key_id": key,
        "aws_secret_access_key": "secret",  # nosec B105  # fake vend response
        "aws_session_token": "token",  # nosec B105
        "expiration": (clock.now + timedelta(seconds=60)).isoformat(),
        "sts_expiration": (clock.now + timedelta(minutes=15)).isoformat(),
        "refresh_after": (clock.now + timedelta(seconds=50)).isoformat(),
        "lease_id": lease_id,
    }


def test_provider_is_lazy_and_concurrent_first_use_is_single_flight():
    clock = Clock()
    calls: list[str] = []

    def vend(lease_id: str) -> dict:
        calls.append(lease_id)
        return _response(clock, "ASIA1", lease_id)

    provider = QuotaBrokerCredentialProvider(
        "https://broker.example",
        "jwt",
        vend_callable=vend,
        time_fetcher=clock,
    )
    assert calls == []

    with ThreadPoolExecutor(max_workers=8) as executor:
        frozen = list(
            executor.map(
                lambda _: provider.credentials.get_frozen_credentials(),
                range(8),
            )
        )

    assert len(calls) == 1
    assert {credentials.access_key for credentials in frozen} == {"ASIA1"}


def test_many_uses_reuse_credentials_then_refresh_with_new_logical_lease():
    clock = Clock()
    calls: list[str] = []

    def vend(lease_id: str) -> dict:
        calls.append(lease_id)
        return _response(clock, f"ASIA{len(calls)}", lease_id)

    provider = QuotaBrokerCredentialProvider(
        "https://broker.example",
        "jwt",
        vend_callable=vend,
        time_fetcher=clock,
    )

    first = provider.credentials.get_frozen_credentials()
    for _ in range(20):
        assert provider.credentials.get_frozen_credentials().access_key == "ASIA1"
    assert len(calls) == 1

    clock.now += timedelta(seconds=51)
    refreshed = provider.credentials.get_frozen_credentials()

    assert refreshed.access_key == "ASIA2"
    assert len(calls) == 2
    assert calls[0] != calls[1]
    assert first.access_key != refreshed.access_key


def test_transport_retry_reuses_same_lease_id_without_extension():
    clock = Clock()
    attempts: list[str] = []
    sleeps: list[float] = []

    def vend(lease_id: str) -> dict:
        attempts.append(lease_id)
        if len(attempts) == 1:
            raise BrokerCredentialError("temporary", terminal=False)
        return _response(clock, "ASIA2", lease_id)

    provider = QuotaBrokerCredentialProvider(
        "https://broker.example",
        "jwt",
        vend_callable=vend,
        time_fetcher=clock,
        sleep_fn=sleeps.append,
    )

    credentials = provider.credentials.get_frozen_credentials()

    assert credentials.access_key == "ASIA2"
    assert attempts[0] == attempts[1]
    assert sleeps == [0.25]


def test_terminal_block_does_not_retry_or_return_credentials():
    attempts = 0

    def vend(_lease_id: str) -> dict:
        nonlocal attempts
        attempts += 1
        raise BrokerCredentialError("quota_blocked", terminal=True)

    provider = QuotaBrokerCredentialProvider(
        "https://broker.example",
        "jwt",
        vend_callable=vend,
        sleep_fn=lambda _: None,
    )

    with pytest.raises(BrokerCredentialError, match="quota_blocked"):
        provider.credentials.get_frozen_credentials()
    assert attempts == 1


def test_provider_rejects_expired_or_malformed_broker_response():
    clock = Clock()

    for response in (
        {},
        {
            "aws_access_key_id": "ASIA",
            "aws_secret_access_key": "secret",  # nosec B105  # fake vend response
            "aws_session_token": "token",  # nosec B105
            "expiration": (clock.now - timedelta(seconds=1)).isoformat(),
        },
    ):
        provider = QuotaBrokerCredentialProvider(
            "https://broker.example",
            "jwt",
            vend_callable=lambda _lease_id, response=response: response,
            time_fetcher=clock,
        )
        with pytest.raises(BrokerCredentialError):
            provider.credentials.get_frozen_credentials()


def test_live_refresh_asks_token_supplier_for_each_logical_lease(monkeypatch):
    clock = Clock()
    supplied = iter(["jwt-1", "jwt-2"])
    seen_tokens: list[str] = []
    calls = 0

    class Response:
        status_code = 200

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

        def raise_for_status(self):
            return None

    def signed_request(*args, **kwargs):
        nonlocal calls
        calls += 1
        seen_tokens.append(kwargs["user_token"])
        return Response(_response(clock, f"ASIA{calls}", kwargs["headers"]["X-Quota-Lease-Id"]))

    monkeypatch.setattr(refreshable_module, "signed_request", signed_request)
    provider = QuotaBrokerCredentialProvider(
        "https://broker.example",
        lambda: next(supplied),
        time_fetcher=clock,
    )

    assert provider.credentials.get_frozen_credentials().access_key == "ASIA1"
    clock.now += timedelta(seconds=51)
    assert provider.credentials.get_frozen_credentials().access_key == "ASIA2"
    assert seen_tokens == ["jwt-1", "jwt-2"]


def test_premature_refresh_does_not_consume_retry_budget(monkeypatch):
    calls = 0

    class Response:
        status_code = 429

        def json(self):
            return {
                "error": {
                    "type": "lease_not_refreshable",
                    "message": "wait for refresh window",
                }
            }

        def raise_for_status(self):
            return None

    def signed_request(*args, **kwargs):
        nonlocal calls
        calls += 1
        return Response()

    monkeypatch.setattr(refreshable_module, "signed_request", signed_request)
    provider = QuotaBrokerCredentialProvider(
        "https://broker.example",
        "jwt",
        sleep_fn=lambda _: pytest.fail("must not immediately retry 429"),
        max_attempts=3,
    )

    with pytest.raises(BrokerCredentialError, match="lease_not_refreshable"):
        provider.credentials.get_frozen_credentials()
    assert calls == 1


def test_expired_lease_id_rotates_once_and_recovers(monkeypatch):
    clock = Clock()
    lease_ids: list[str] = []

    class Response:
        def __init__(self, status_code, body):
            self.status_code = status_code
            self._body = body

        def json(self):
            return self._body

        def raise_for_status(self):
            return None

    def signed_request(*args, **kwargs):
        lease_id = kwargs["headers"]["X-Quota-Lease-Id"]
        lease_ids.append(lease_id)
        if len(lease_ids) == 1:
            return Response(
                409,
                {
                    "error": {
                        "type": "lease_expired",
                        "message": "use a new lease ID",
                    }
                },
            )
        return Response(200, _response(clock, "ASIA2", lease_id))

    monkeypatch.setattr(refreshable_module, "signed_request", signed_request)
    provider = QuotaBrokerCredentialProvider(
        "https://broker.example",
        "jwt",
        time_fetcher=clock,
        sleep_fn=lambda _: pytest.fail("409 rotation should not back off"),
    )

    credentials = provider.credentials.get_frozen_credentials()

    assert credentials.access_key == "ASIA2"
    assert len(lease_ids) == 2
    assert lease_ids[0] != lease_ids[1]


# ---------------------------------------------------------------------------
# BedrockSpendControls: the per-process factory an application backend uses.
# ---------------------------------------------------------------------------

import base64  # noqa: E402
import json  # noqa: E402

from examples.refreshable_bedrock import (  # noqa: E402
    BedrockSpendControls,
    QuotaExceededError,
    UserBlockedError,
    VendRateLimitedError,
    jwt_claim,
)


def _jwt(**claims) -> str:
    def segment(obj) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{segment({'alg': 'none'})}.{segment(claims)}.sig"


def test_jwt_claim_reads_the_payload_without_verifying():
    assert jwt_claim(_jwt(sub="alice", tenant="acme"), "sub") == "alice"
    assert jwt_claim(_jwt(sub="alice", tenant="acme"), "tenant") == "acme"
    with pytest.raises(ValueError, match="no string claim"):
        jwt_claim(_jwt(sub="alice"), "tenant")
    with pytest.raises(ValueError, match="not a JWT"):
        jwt_claim("not-a-token", "sub")


def test_factory_keeps_one_client_per_identity_and_separates_users():
    clock = Clock()
    calls: list[str] = []

    def vend(lease_id: str) -> dict:
        calls.append(lease_id)
        return _response(clock, f"ASIA{len(calls)}", lease_id)

    factory = BedrockSpendControls(
        "https://broker.example/",
        provider_kwargs={"vend_callable": vend, "time_fetcher": clock},
    )
    alice_first = factory.client_for(_jwt(sub="alice", sid="s1"))
    alice_second = factory.client_for(_jwt(sub="alice", sid="s2"))
    bob = factory.client_for(_jwt(sub="bob"))

    assert alice_first is alice_second
    assert bob is not alice_first
    assert factory.active_identities() == ["alice", "bob"]

    alice_creds = factory.provider_for(_jwt(sub="alice")).credentials
    bob_creds = factory.provider_for(_jwt(sub="bob")).credentials
    assert alice_creds.get_frozen_credentials().access_key == "ASIA1"
    assert bob_creds.get_frozen_credentials().access_key == "ASIA2"
    assert alice_creds.get_frozen_credentials().access_key == "ASIA1"
    assert len(calls) == 2  # one lease per identity, not per call

    assert factory.forget(_jwt(sub="bob")) is True
    assert factory.forget("bob") is False
    assert factory.active_identities() == ["alice"]


def test_factory_keys_by_the_deployment_identity_claim():
    clock = Clock()
    factory = BedrockSpendControls(
        "https://broker.example",
        identity_claim="tenant",
        provider_kwargs={"vend_callable": lambda lease_id: _response(clock, "ASIA", lease_id),
                         "time_fetcher": clock},
    )
    shared = factory.client_for(_jwt(sub="alice", tenant="acme"))
    assert factory.client_for(_jwt(sub="bob", tenant="acme")) is shared
    assert factory.active_identities() == ["acme"]


def test_factory_renews_with_the_latest_jwt_seen_for_the_identity(monkeypatch):
    clock = Clock()
    seen_tokens: list[str] = []
    calls = 0

    class Response:
        status_code = 200
        headers: dict = {}

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

        def raise_for_status(self):
            return None

    def signed_request(*args, **kwargs):
        nonlocal calls
        calls += 1
        seen_tokens.append(kwargs["user_token"])
        return Response(_response(clock, f"ASIA{calls}", kwargs["headers"]["X-Quota-Lease-Id"]))

    monkeypatch.setattr(refreshable_module, "signed_request", signed_request)
    factory = BedrockSpendControls(
        "https://broker.example", provider_kwargs={"time_fetcher": clock}
    )

    first_jwt = _jwt(sub="alice", exp=1)
    second_jwt = _jwt(sub="alice", exp=2)
    factory.client_for(first_jwt)
    provider = factory.provider_for(first_jwt)
    assert provider.credentials.get_frozen_credentials().access_key == "ASIA1"

    factory.client_for(second_jwt)  # the user's session refreshed its token
    clock.now += timedelta(seconds=51)
    assert provider.credentials.get_frozen_credentials().access_key == "ASIA2"
    assert seen_tokens == [first_jwt, second_jwt]


def test_factory_evicts_idle_identities_and_bounds_the_cache():
    clock = Clock()
    ticks = {"now": 1000.0}
    factory = BedrockSpendControls(
        "https://broker.example",
        idle_ttl_seconds=100,
        max_users=2,
        time_fetcher=lambda: ticks["now"],
        provider_kwargs={"vend_callable": lambda lease_id: _response(clock, "ASIA", lease_id),
                         "time_fetcher": clock},
    )
    factory.client_for(_jwt(sub="a"))
    ticks["now"] += 10
    factory.client_for(_jwt(sub="b"))
    ticks["now"] += 10
    factory.client_for(_jwt(sub="c"))  # over max_users: least recently used "a" goes
    assert factory.active_identities() == ["b", "c"]

    ticks["now"] += 200  # everyone idle past the TTL
    factory.client_for(_jwt(sub="d"))
    assert factory.active_identities() == ["d"]


def test_factory_reads_the_current_user_from_the_context():
    clock = Clock()
    factory = BedrockSpendControls(
        "https://broker.example",
        provider_kwargs={"vend_callable": lambda lease_id: _response(clock, "ASIA", lease_id),
                         "time_fetcher": clock},
    )
    with pytest.raises(ValueError, match="no user JWT"):
        factory.client_for()

    token = BedrockSpendControls.set_current_user(_jwt(sub="alice"))
    try:
        assert factory.client_for() is factory.client_for(_jwt(sub="alice"))
        assert BedrockSpendControls.current_user() == _jwt(sub="alice")
    finally:
        BedrockSpendControls.reset_current_user(token)
    assert BedrockSpendControls.current_user() is None


@pytest.mark.parametrize(
    ("status", "error_type", "headers", "expected", "checks"),
    [
        (
            429,
            "quota_exceeded",
            {
                "Retry-After": "1",
                "X-Quota-Breached-Period": "daily",
                "X-Quota-Breached-Dimension": "usd",
                "X-Quota-Resets-At": "2030-01-02T00:00:00+00:00",
            },
            QuotaExceededError,
            {
                "breached_period": "daily",
                "breached_dimension": "usd",
                "resets_at": datetime(2030, 1, 2, tzinfo=timezone.utc),
                "retry_after": 1,
                "status_code": 429,
            },
        ),
        (403, "quota_blocked", {}, UserBlockedError, {"status_code": 403}),
        (
            429,
            "lease_rate_limited",
            {"Retry-After": "37"},
            VendRateLimitedError,
            {"retry_after": 37},
        ),
        (429, "lease_not_refreshable", {}, VendRateLimitedError, {}),
        (409, "invalid_lease", {}, BrokerCredentialError, {"error_type": "invalid_lease"}),
    ],
)
def test_broker_refusals_surface_as_typed_errors(monkeypatch, status, error_type, headers, expected, checks):
    class Response:
        def __init__(self):
            self.status_code = status
            self.headers = headers

        def json(self):
            return {"error": {"type": error_type, "message": "nope"}}

        def raise_for_status(self):
            raise AssertionError("not reached")

    monkeypatch.setattr(refreshable_module, "signed_request", lambda *a, **k: Response())
    provider = QuotaBrokerCredentialProvider(
        "https://broker.example", _jwt(sub="alice"), sleep_fn=lambda _: None
    )

    with pytest.raises(expected) as caught:
        provider.credentials.get_frozen_credentials()
    assert type(caught.value) is expected
    assert caught.value.terminal is True
    for attribute, value in checks.items():
        assert getattr(caught.value, attribute) == value


# ---------------------------------------------------------------------------
# Pre-publication review fixes (H-8, H-9, M-9, M-10, M-11, client lows).
# ---------------------------------------------------------------------------

import subprocess  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
from email.utils import format_datetime  # noqa: E402
from pathlib import Path  # noqa: E402

import httpx  # noqa: E402
from botocore.credentials import Credentials  # noqa: E402

from examples.refreshable_bedrock import (  # noqa: E402
    BROKER_TIMEOUT,
    AuthenticationError,
    EmergencyStopError,
    signed_request,
)


class _Response:
    def __init__(self, status_code, body, headers=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    def json(self):
        return self._body


# -- H-8: shared sessions, build outside the lock, defensible default bound --


def test_factory_shares_one_signing_session_loader_and_http_client():
    clock = Clock()
    factory = BedrockSpendControls(
        "https://broker.example",
        provider_kwargs={"vend_callable": lambda lease_id: _response(clock, "ASIA", lease_id),
                         "time_fetcher": clock},
    )
    assert factory.max_users == 1_000
    alice = factory.provider_for(_jwt(sub="alice"))
    bob = factory.provider_for(_jwt(sub="bob"))
    assert alice.aws_session is bob.aws_session is factory.aws_session
    assert alice.http_client is bob.http_client is factory.http_client
    assert factory.http_client.timeout == BROKER_TIMEOUT
    # Every client is built over one process-wide botocore model loader.
    loader = refreshable_module._shared_data_loader()
    assert loader is refreshable_module._shared_data_loader()
    for identity in ("alice", "bob"):
        client = factory.client_for(_jwt(sub=identity))
        assert client.meta.region_name == "us-east-1"
        assert client.meta.service_model.service_name == "bedrock-runtime"
    factory.close()
    assert factory.active_identities() == []


def test_factory_builds_new_identities_outside_the_lock_and_keeps_one_entry():
    clock = Clock()
    built: list[str] = []
    factory = BedrockSpendControls(
        "https://broker.example",
        provider_kwargs={"vend_callable": lambda lease_id: _response(clock, "ASIA", lease_id),
                         "time_fetcher": clock},
    )
    original = factory._create_entry
    gate = threading.Barrier(16)

    def slow_create(key, user_jwt, now):
        assert not factory._lock.locked(), "entry built while holding the factory lock"
        gate.wait(timeout=5)
        built.append(key)
        return original(key, user_jwt, now)

    factory._create_entry = slow_create
    with ThreadPoolExecutor(max_workers=16) as executor:
        clients = list(executor.map(lambda _: factory.client_for(_jwt(sub="carol")), range(16)))

    assert len(built) == 16  # every racer built a candidate ...
    assert len({id(client) for client in clients}) == 1  # ... but one entry won
    assert factory.active_identities() == ["carol"]


# -- H-9: unverified tokens must not poison the identity's renewal token ----


def test_rejected_token_never_replaces_the_accepted_renewal_token(monkeypatch):
    clock = Clock()
    seen_tokens: list[str] = []
    real = _jwt(sub="alice", exp=1)
    forged = _jwt(sub="alice", forged=True)

    def fake_signed_request(*args, **kwargs):
        token = kwargs["user_token"]
        seen_tokens.append(token)
        if token == forged:
            return _Response(401, {"error": {"type": "authentication_error", "message": "bad signature"}})
        return _Response(200, _response(clock, f"ASIA{len(seen_tokens)}", kwargs["headers"]["X-Quota-Lease-Id"]))

    monkeypatch.setattr(refreshable_module, "signed_request", fake_signed_request)
    factory = BedrockSpendControls(
        "https://broker.example", provider_kwargs={"time_fetcher": clock}
    )
    client = factory.client_for(real)
    provider = factory.provider_for(real)
    assert provider.credentials.get_frozen_credentials().access_key == "ASIA1"

    # An attacker presents a forged token naming alice: the cached client is
    # returned (the cache key is client-side), but the stored token is not
    # replaced until the broker accepts the new one.
    assert factory.client_for(forged) is client
    entry = factory._entries["alice"]
    assert entry.jwt == real
    assert entry.pending_jwt == forged

    clock.now += timedelta(seconds=51)  # advisory refresh: broker rejects the forged token
    assert provider.credentials.get_frozen_credentials().access_key == "ASIA1"
    assert entry.jwt == real
    assert entry.pending_jwt is None

    assert provider.credentials.get_frozen_credentials().access_key == "ASIA3"
    assert seen_tokens == [real, forged, real]


def test_verified_identity_must_match_the_token_claim():
    clock = Clock()
    factory = BedrockSpendControls(
        "https://broker.example",
        provider_kwargs={"vend_callable": lambda lease_id: _response(clock, "ASIA", lease_id),
                         "time_fetcher": clock},
    )
    alice = _jwt(sub="alice")
    assert factory.client_for_identity("alice", alice) is factory.client_for(alice, verified_identity="alice")
    with pytest.raises(ValueError, match="verified identity is 'bob'"):
        factory.client_for(alice, verified_identity="bob")
    with pytest.raises(ValueError, match="verified identity is 'bob'"):
        factory.client_for_identity("bob", alice)
    with pytest.raises(ValueError, match="must not be empty"):
        factory.client_for_identity("", alice)
    assert factory.active_identities() == ["alice"]
    assert factory.provider_for(alice, verified_identity="alice") is factory.provider_for(alice)


# -- M-9: typed 401 / 503, headers optional ----------------------------------


def test_401_surfaces_as_authentication_error(monkeypatch):
    monkeypatch.setattr(
        refreshable_module,
        "signed_request",
        lambda *a, **k: _Response(401, {"error": {"type": "authentication_error", "message": "Token expired."}}),
    )
    provider = QuotaBrokerCredentialProvider("https://broker.example", _jwt(sub="alice"))
    with pytest.raises(AuthenticationError) as caught:
        provider.credentials.get_frozen_credentials()
    assert caught.value.status_code == 401
    assert caught.value.error_type == "authentication_error"
    assert caught.value.terminal is True
    assert "Token expired." in str(caught.value)

    # A 401 without a JSON body is still typed by status.
    monkeypatch.setattr(refreshable_module, "signed_request", lambda *a, **k: _Response(401, None))
    with pytest.raises(AuthenticationError):
        QuotaBrokerCredentialProvider("https://broker.example", "jwt").credentials.get_frozen_credentials()


def test_emergency_stop_is_typed_retryable_and_not_hammered(monkeypatch):
    calls = 0

    def fake(*args, **kwargs):
        nonlocal calls
        calls += 1
        return _Response(
            503,
            {"error": {"type": "emergency_stop", "message": "Vending is disabled."}},
            {"Retry-After": "60"},
        )

    monkeypatch.setattr(refreshable_module, "signed_request", fake)
    provider = QuotaBrokerCredentialProvider(
        "https://broker.example", "jwt", sleep_fn=lambda _: pytest.fail("must not sleep 60 s in-call")
    )
    with pytest.raises(EmergencyStopError) as caught:
        provider.credentials.get_frozen_credentials()
    assert caught.value.terminal is False
    assert caught.value.error_type == "emergency_stop"
    assert caught.value.status_code == 503
    assert caught.value.retry_after == 60
    assert calls == 1


def test_other_5xx_carry_the_body_error_type_and_honour_short_retry_after(monkeypatch):
    sleeps: list[float] = []
    calls = 0
    clock = Clock()

    def fake(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _Response(
                503,
                {"error": {"type": "quota_state_conflict", "message": "retry"}},
                {"Retry-After": "1"},
            )
        return _Response(200, _response(clock, "ASIA2", kwargs["headers"]["X-Quota-Lease-Id"]))

    monkeypatch.setattr(refreshable_module, "signed_request", fake)
    provider = QuotaBrokerCredentialProvider(
        "https://broker.example", "jwt", time_fetcher=clock, sleep_fn=sleeps.append
    )
    assert provider.credentials.get_frozen_credentials().access_key == "ASIA2"
    assert sleeps == [1.0]

    monkeypatch.setattr(refreshable_module, "signed_request", lambda *a, **k: _Response(502, "<html>"))
    provider = QuotaBrokerCredentialProvider(
        "https://broker.example", "jwt", sleep_fn=lambda _: None, max_attempts=2
    )
    with pytest.raises(BrokerCredentialError) as caught:
        provider.credentials.get_frozen_credentials()
    assert type(caught.value) is BrokerCredentialError
    assert caught.value.error_type == "http_502"
    assert caught.value.terminal is False


def test_blocked_and_exceeded_tolerate_missing_headers(monkeypatch):
    monkeypatch.setattr(
        refreshable_module,
        "signed_request",
        lambda *a, **k: _Response(403, {"error": {"type": "quota_blocked", "message": "User 'alice' is blocked."}}),
    )
    provider = QuotaBrokerCredentialProvider("https://broker.example", "jwt")
    with pytest.raises(UserBlockedError) as caught:
        provider.credentials.get_frozen_credentials()
    assert caught.value.resets_at is None
    assert caught.value.retry_after is None
    assert caught.value.breached_period == ""
    assert "quota_blocked: User 'alice' is blocked." in str(caught.value)
    assert "resets at" not in str(caught.value)

    monkeypatch.setattr(
        refreshable_module,
        "signed_request",
        lambda *a, **k: _Response(
            429,
            {"error": {"type": "quota_exceeded", "message": "exhausted"}},
            {"X-Quota-Resets-At": "2030-01-02T00:00:00+00:00"},
        ),
    )
    provider = QuotaBrokerCredentialProvider("https://broker.example", "jwt")
    with pytest.raises(QuotaExceededError) as caught:
        provider.credentials.get_frozen_credentials()
    assert caught.value.resets_at == datetime(2030, 1, 2, tzinfo=timezone.utc)
    assert "resets at 2030-01-02T00:00:00+00:00" in str(caught.value)


# -- M-10: clock skew --------------------------------------------------------


class FakeBroker:
    """Server-side lease semantics (quota.py reserve_lease) on its own clock.

    ``client_ahead_seconds`` > 0 means the client's clock runs ahead of the
    broker's, so the client believes refresh windows open before they do.
    """

    def __init__(self, clock: Clock, *, client_ahead_seconds: float, vends_per_minute: int = 3,
                 lease_seconds: int = 60, overlap_seconds: int = 10):
        self.clock = clock
        self.skew = timedelta(seconds=client_ahead_seconds)
        self.limit = vends_per_minute
        self.lease_seconds = lease_seconds
        self.overlap = overlap_seconds
        self.lease: tuple[str, datetime, datetime] | None = None
        self.vend_times: list[datetime] = []
        self.calls = 0
        self.joins = 0
        self.generations = 0

    def now(self) -> datetime:
        return self.clock.now - self.skew

    def __call__(self, lease_id: str):
        now = self.now()
        self.calls += 1
        recent = [t for t in self.vend_times if now - t < timedelta(minutes=1)]
        if len(recent) >= self.limit:
            retry_at = recent[0] + timedelta(minutes=1)
            raise VendRateLimitedError(
                "lease_rate_limited", terminal=True, error_type="lease_rate_limited",
                status_code=429, retry_after=int((retry_at - now).total_seconds()) + 1,
                refresh_after=self.lease[2] if self.lease else None,
            )
        self.vend_times.append(now)
        if self.lease is not None and self.lease[0] == lease_id:
            current = self.lease
        elif self.lease is not None and now < self.lease[2]:
            self.joins += 1
            current = self.lease
        else:
            self.generations += 1
            expires = now + timedelta(seconds=self.lease_seconds)
            current = (lease_id, expires, expires - timedelta(seconds=self.overlap))
            self.lease = current
        body = {
            "aws_access_key_id": f"ASIA{self.generations}",
            "aws_secret_access_key": "secret",  # nosec B105  # fake vend response
            "aws_session_token": "token",  # nosec B105
            "expiration": current[1].isoformat(),
            "refresh_after": current[2].isoformat(),
            "lease_id": current[0],
        }
        return body, {"Date": format_datetime(now, usegmt=True)}


@pytest.mark.parametrize("client_ahead_seconds", [6, -6])
def test_clock_skew_is_corrected_from_the_date_header(client_ahead_seconds):
    clock = Clock()
    broker = FakeBroker(clock, client_ahead_seconds=client_ahead_seconds)
    provider = QuotaBrokerCredentialProvider(
        "https://broker.example", "jwt", vend_callable=broker, time_fetcher=clock,
        sleep_fn=lambda _: None,
    )
    rate_limited = 0
    calls = 240
    for _ in range(calls):
        clock.now += timedelta(seconds=1)
        try:
            frozen = provider.credentials.get_frozen_credentials()
        except VendRateLimitedError:
            rate_limited += 1
            continue
        # Credentials handed out are never past the broker's deadline.
        assert broker.now() < broker.lease[1]
        assert frozen.access_key == f"ASIA{broker.generations}"

    assert rate_limited == 0
    assert provider.clock_offset == timedelta(seconds=-client_ahead_seconds)
    assert broker.generations == pytest.approx(calls / 60, abs=1)
    assert broker.joins <= broker.generations  # at most one premature join per lease
    assert broker.calls <= broker.generations * 2


def test_unskewed_clock_keeps_old_behaviour_and_ignores_date_noise():
    clock = Clock()
    broker = FakeBroker(clock, client_ahead_seconds=0.4)  # sub-second: measurement noise
    provider = QuotaBrokerCredentialProvider(
        "https://broker.example", "jwt", vend_callable=broker, time_fetcher=clock
    )
    provider.credentials.get_frozen_credentials()
    assert provider.clock_offset == timedelta(0)
    for _ in range(120):
        clock.now += timedelta(seconds=1)
        provider.credentials.get_frozen_credentials()
    assert broker.calls == broker.generations == 3


def test_premature_refresh_honours_refresh_after_and_stops_asking(monkeypatch):
    clock = Clock()
    start = clock.now
    calls: list[datetime] = []

    def fake(*args, **kwargs):
        calls.append(clock.now)
        if len(calls) == 1:
            return _Response(200, _response(clock, "ASIA1", kwargs["headers"]["X-Quota-Lease-Id"]))
        if clock.now < start + timedelta(seconds=54):
            return _Response(
                429,
                {"error": {"type": "lease_not_refreshable", "message": "not yet"}},
                {"Retry-After": "3", "X-Quota-Refresh-After": (start + timedelta(seconds=54)).isoformat()},
            )
        return _Response(200, _response(clock, "ASIA2", kwargs["headers"]["X-Quota-Lease-Id"]))

    monkeypatch.setattr(refreshable_module, "signed_request", fake)
    provider = QuotaBrokerCredentialProvider(
        "https://broker.example", "jwt", time_fetcher=clock, sleep_fn=lambda _: None
    )
    assert provider.credentials.get_frozen_credentials().access_key == "ASIA1"

    clock.now = start + timedelta(seconds=51)  # inside the advisory window, server disagrees
    assert provider.credentials.get_frozen_credentials().access_key == "ASIA1"
    assert len(calls) == 2
    for seconds in (52, 53):
        clock.now = start + timedelta(seconds=seconds)
        assert provider.credentials.get_frozen_credentials().access_key == "ASIA1"
    assert len(calls) == 2  # no broker traffic until the server's refresh time

    clock.now = start + timedelta(seconds=55)
    assert provider.credentials.get_frozen_credentials().access_key == "ASIA2"
    assert len(calls) == 3


# -- M-11: cached refusals ---------------------------------------------------


def test_thirty_two_threads_against_a_blocked_user_hit_the_broker_once():
    calls = 0
    lock = threading.Lock()

    def vend(_lease_id):
        nonlocal calls
        with lock:
            calls += 1
        raise UserBlockedError(
            "quota_blocked", terminal=True, error_type="quota_blocked", status_code=403
        )

    provider = QuotaBrokerCredentialProvider(
        "https://broker.example", "jwt", vend_callable=vend, sleep_fn=lambda _: None
    )
    results = []

    def attempt():
        try:
            provider.credentials.get_frozen_credentials()
        except UserBlockedError as exc:
            results.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(32)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 32
    assert calls == 1


def test_refusals_are_cached_until_the_earliest_hint_then_retried():
    clock = Clock()
    outcomes: list = []

    def vend(lease_id):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return _response(clock, outcome, lease_id)

    provider = QuotaBrokerCredentialProvider(
        "https://broker.example", "jwt", vend_callable=vend, time_fetcher=clock,
        sleep_fn=lambda _: None,
    )
    # Emergency stop: Retry-After 60 is capped at 30 s.
    outcomes.append(EmergencyStopError("emergency_stop", terminal=False, error_type="emergency_stop",
                                       status_code=503, retry_after=60))
    for _ in range(5):
        with pytest.raises(EmergencyStopError):
            provider.credentials.get_frozen_credentials()
    assert outcomes == []
    clock.now += timedelta(seconds=29)
    with pytest.raises(EmergencyStopError):
        provider.credentials.get_frozen_credentials()
    clock.now += timedelta(seconds=2)
    # Quota exceeded with resets_at 8 s away wins over the 30 s default.
    outcomes.append(QuotaExceededError("quota_exceeded", terminal=True, error_type="quota_exceeded",
                                       status_code=429, resets_at=clock.now + timedelta(seconds=8)))
    with pytest.raises(QuotaExceededError):
        provider.credentials.get_frozen_credentials()
    clock.now += timedelta(seconds=7)
    with pytest.raises(QuotaExceededError):
        provider.credentials.get_frozen_credentials()
    assert outcomes == []
    clock.now += timedelta(seconds=2)
    outcomes.append("ASIA9")
    assert provider.credentials.get_frozen_credentials().access_key == "ASIA9"
    assert outcomes == []


def test_generic_terminal_errors_are_not_cached():
    attempts = 0

    def vend(_lease_id):
        nonlocal attempts
        attempts += 1
        raise BrokerCredentialError("invalid_lease", terminal=True, error_type="invalid_lease")

    provider = QuotaBrokerCredentialProvider("https://broker.example", "jwt", vend_callable=vend)
    for _ in range(3):
        with pytest.raises(BrokerCredentialError):
            provider.credentials.get_frozen_credentials()
    assert attempts == 3


# -- client lows: timeout, client reuse, malformed body, eviction race -------


def test_inline_signer_signs_broker_requests_with_bounded_timeout():
    class AwsSession:
        def get_credentials(self):
            return Credentials("AKID", "SECRET", "SESSION")

    captured = {}

    def handler(request):
        captured["request"] = request
        return httpx.Response(200, json={}, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        response = signed_request(
            "POST",
            "https://example.lambda-url.us-east-1.on.aws/v1/credentials",
            region="us-east-1",
            user_token="user-jwt",
            aws_session=AwsSession(),
            http_client=client,
            headers={"X-Quota-Lease-Id": "lease-1"},
        )
    assert response.status_code == 200
    request = captured["request"]
    assert request.headers["X-Quota-User-Token"] == "user-jwt"
    assert request.headers["X-Quota-Lease-Id"] == "lease-1"
    assert request.headers["Authorization"].startswith("AWS4-HMAC-SHA256 ")
    assert request.headers["X-Amz-Security-Token"] == "SESSION"
    assert request.extensions["timeout"] == {"connect": 2.0, "read": 5.0, "write": 5.0, "pool": 2.0}
    assert BROKER_TIMEOUT.connect == 2.0 and BROKER_TIMEOUT.read == 5.0


def test_provider_reuses_one_http_client_across_vends():
    clock = Clock()
    clients: list[int] = []

    class AwsSession:
        def get_credentials(self):
            return Credentials("AKID", "SECRET", "SESSION")

    def handler(request):
        clients.append(id(request))
        return httpx.Response(
            200,
            json=_response(clock, f"ASIA{len(clients)}", request.headers["X-Quota-Lease-Id"]),
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        provider = QuotaBrokerCredentialProvider(
            "https://broker.example", "jwt", aws_session=AwsSession(),
            http_client=http_client, time_fetcher=clock,
        )
        assert provider.credentials.get_frozen_credentials().access_key == "ASIA1"
        clock.now += timedelta(seconds=51)
        assert provider.credentials.get_frozen_credentials().access_key == "ASIA2"
        assert provider.http_client is http_client
        assert len(clients) == 2

    provider = QuotaBrokerCredentialProvider("https://broker.example", "jwt", aws_session=AwsSession())
    assert provider._http() is provider._http()
    assert provider.http_client.timeout == BROKER_TIMEOUT
    provider.close()
    assert provider.http_client is None


@pytest.mark.parametrize(
    "body",
    [
        {"expiration": "2030-01-01T00:01:00+00:00"},
        {"aws_access_key_id": "ASIA", "aws_secret_access_key": "", "aws_session_token": "t",  # nosec B105
         "expiration": "2030-01-01T00:01:00+00:00"},
        [],
    ],
)
def test_malformed_200_body_is_a_broker_error_not_a_key_error(monkeypatch, body):
    monkeypatch.setattr(refreshable_module, "signed_request", lambda *a, **k: _Response(200, body))
    provider = QuotaBrokerCredentialProvider("https://broker.example", "jwt", sleep_fn=lambda _: None)
    with pytest.raises(BrokerCredentialError) as caught:
        provider.credentials.get_frozen_credentials()
    assert not isinstance(caught.value, KeyError)
    assert "broker response" in str(caught.value)


def test_provider_for_survives_eviction_between_lookup_and_return():
    clock = Clock()
    factory = BedrockSpendControls(
        "https://broker.example",
        max_users=1,
        provider_kwargs={"vend_callable": lambda lease_id: _response(clock, "ASIA", lease_id),
                         "time_fetcher": clock},
    )
    alice = factory.provider_for(_jwt(sub="alice"))
    bob = factory.provider_for(_jwt(sub="bob"))  # evicts alice
    assert alice is not bob
    assert factory.active_identities() == ["bob"]
    assert factory.provider_for(_jwt(sub="alice")) is not alice  # rebuilt, no KeyError


def test_refreshable_bedrock_imports_standalone(tmp_path):
    source = Path(refreshable_module.__file__)
    (tmp_path / "refreshable_bedrock.py").write_text(source.read_text())
    script = (
        "import refreshable_bedrock as m\n"
        "assert 'examples' not in __import__('sys').modules\n"
        "print(m.signed_request.__name__, m.BedrockSpendControls.__name__)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=60
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "signed_request BedrockSpendControls"
