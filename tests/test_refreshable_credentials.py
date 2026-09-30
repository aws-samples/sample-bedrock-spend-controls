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
