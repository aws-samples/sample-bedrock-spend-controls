"""Execution context for the preflight checks.

The :class:`Context` carries the boto3 session, the target account/Region/
partition, the deployment values (raw mapping plus the validated
``DeploymentConfig`` when ``cdk/stacks/configuration.py`` accepts them) and
the operator's options. Checks obtain AWS clients through
:meth:`Context.clients`, which memoises per service so tests can inject
stubbed or fake clients before running a check.

Configuration handling deliberately reuses the CDK app's own validator
(``DeploymentConfig.from_mapping`` / ``validate_mapping``) so the preflight,
the wizard, and ``cdk synth`` can never disagree about what a valid
deployment looks like.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping

import boto3
from botocore.exceptions import BotoCoreError, ClientError, ProfileNotFound

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "cdk" / "config" / "demo.json"

# Values the checks fall back to when cdk/stacks/configuration.py cannot be
# imported (for example before the CDK virtualenv exists). The authoritative
# defaults live in that module; this subset only covers keys a check reads.
_FALLBACK_DEFAULTS: dict[str, Any] = {
    "adapter_layer_arn": "",
    "admin_jwt_claim": "",
    "admin_ui": False,
    "allowed_model_arns": ["*"],
    "invocation_log_group_name": "",
    "invoker_principal_arns": [],
    "jwt_audience": "",
    "jwt_issuer": "",
    "jwt_jwks_url": "",
    "jwt_user_claim": "sub",
    "permission_lease_seconds": 300,
    "reconciliation_enabled": False,
    "reserve_enforcement_concurrency": True,
    "revocation_reconcile_minutes": 5,
    "usage_retention_days": 35,
}

# Deployment keys whose value is a list; ``--set key=a,b`` splits on commas
# for these when the configuration module is unavailable.
_LIST_KEYS = frozenset(
    {
        "admin_ui_connect_origins",
        "allowed_model_arns",
        "invoker_principal_arns",
        "reconciliation_service_names",
    }
)

_PARTITION_PREFIXES = (
    ("cn-", "aws-cn"),
    ("us-gov-", "aws-us-gov"),
    ("us-isob-", "aws-iso-b"),
    ("us-isof-", "aws-iso-f"),
    ("us-iso-", "aws-iso"),
    ("eu-isoe-", "aws-iso-e"),
    ("eusc-", "aws-eusc"),
)


class PreflightError(Exception):
    """A usage or configuration problem; the CLI reports it and exits 2."""


@dataclass
class Options:
    """Operator-supplied switches that change how checks judge a finding."""

    acknowledge_logging_overwrite: bool = False
    sample_jwt: str | None = None
    app_role_arn: str | None = None
    admin_ui_build_required: bool = False


@dataclass
class Context:
    """Everything a check needs to know about the target deployment."""

    session: boto3.session.Session
    region: str
    account: str | None = None
    partition: str = "aws"
    profile: str | None = None
    config: Any | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    options: Options = field(default_factory=Options)
    config_dir: Path = DEFAULT_CONFIG_PATH.parent
    root: Path = REPO_ROOT
    # Account the operator asked for (``--account-id``); the credentials
    # check fails when the session belongs to a different account.
    expected_account: str | None = None
    # ``service`` -> client, or ``"service@region"`` for another Region.
    # Tests pre-populate this with botocore Stubber-wrapped or fake clients.
    client_cache: dict[str, Any] = field(default_factory=dict)
    _identity: dict[str, str] | None = field(default=None, repr=False)

    def clients(self, service: str, *, region: str | None = None) -> Any:
        """Return a memoised boto3 client for ``service``.

        ``region`` selects another Region (used when an inference profile
        routes to foundation models elsewhere); the default is the target
        Region.
        """
        key = service if region in (None, self.region) else f"{service}@{region}"
        client = self.client_cache.get(key)
        if client is None:
            client = self.session.client(service, region_name=region or self.region)
            self.client_cache[key] = client
        return client

    def identity(self) -> dict[str, str]:
        """Memoised ``sts:GetCallerIdentity`` (Account, Arn, UserId)."""
        if self._identity is None:
            response = self.clients("sts").get_caller_identity()
            self._identity = {
                "Account": response["Account"],
                "Arn": response["Arn"],
                "UserId": response.get("UserId", ""),
            }
            if not self.account:
                self.account = self._identity["Account"]
            arn_partition = self._identity["Arn"].split(":")[1:2]
            if arn_partition and arn_partition[0]:
                self.partition = arn_partition[0]
        return self._identity

    def setting(self, name: str) -> Any:
        """Read a deployment value: validated config first, then the raw
        mapping, then the documented default."""
        if self.config is not None and hasattr(self.config, name):
            return getattr(self.config, name)
        if name in self.raw:
            return self.raw[name]
        return defaults().get(name)

    def workloads(self) -> tuple[Any, ...]:
        """Workload roster entries (``name``/``model``/``role_arn``) when the
        validated configuration is available; empty otherwise."""
        if self.config is None:
            return ()
        return tuple(getattr(self.config, "workloads", ()) or ())


# --- configuration module access ------------------------------------------

_configuration: ModuleType | None | bool = False  # False = not tried yet
_configuration_error: str = ""


def configuration_module() -> ModuleType | None:
    """Import ``cdk.stacks.configuration`` lazily.

    Returns ``None`` (and remembers why in :func:`configuration_error`) when
    the module or its dependencies are missing, so the checks can still run
    against the raw values.
    """
    global _configuration, _configuration_error
    if _configuration is False:
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        try:
            module = importlib.import_module("cdk.stacks.configuration")
        except Exception as exc:  # ImportError, or jsii failing to start
            _configuration = None
            _configuration_error = f"{type(exc).__name__}: {exc}"
            return None
        missing = [
            name
            for name in ("KNOWN_KEYS", "DEFAULTS", "validate_mapping")
            if not hasattr(module, name)
        ]
        if not callable(getattr(getattr(module, "DeploymentConfig", None), "from_mapping", None)):
            missing.append("DeploymentConfig.from_mapping")
        if missing:
            _configuration = None
            _configuration_error = (
                "cdk/stacks/configuration.py does not expose " + ", ".join(missing)
            )
        else:
            _configuration = module
    return _configuration or None


def configuration_error() -> str:
    configuration_module()
    return _configuration_error


def known_keys() -> frozenset[str] | None:
    module = configuration_module()
    keys = getattr(module, "KNOWN_KEYS", None) if module else None
    return frozenset(keys) if keys is not None else None


def defaults() -> Mapping[str, Any]:
    module = configuration_module()
    values = getattr(module, "DEFAULTS", None) if module else None
    return values if values is not None else _FALLBACK_DEFAULTS


# --- values and overrides ---------------------------------------------------


def load_values(path: Path) -> dict[str, Any]:
    """Read a deployment JSON file (``cdk/config/*.json``)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PreflightError(f"config file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise PreflightError(f"config file {path} is not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise PreflightError(f"config file {path} must contain a JSON object")
    return data


def parse_override(key: str, text: str) -> Any:
    """Turn a ``--set key=value`` string into the JSON type the key expects.

    Booleans, integers and floats are recognised from the text; keys whose
    default is a list split on commas; a value starting with ``[`` or ``{``
    is parsed as JSON (``default_limits``, inline ``workloads``); ``null``
    clears a key. Keys the configuration module does not know are rejected.
    """
    keys = known_keys()
    if keys is not None and key not in keys:
        raise PreflightError(
            f"--set: unknown deployment key {key!r} "
            f"(known keys: {', '.join(sorted(keys))})"
        )
    stripped = text.strip()
    default = defaults().get(key)
    if stripped[:1] in ("[", "{"):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise PreflightError(f"--set {key}: invalid JSON value: {exc}") from None
    lowered = stripped.lower()
    if lowered in ("null", "none"):
        return None
    if isinstance(default, list) or key in _LIST_KEYS:
        return [item.strip() for item in stripped.split(",") if item.strip()]
    if isinstance(default, bool):  # before int: bool is an int subclass
        if lowered in ("true", "yes", "on", "1"):
            return True
        if lowered in ("false", "no", "off", "0"):
            return False
        raise PreflightError(f"--set {key}: expected true or false, got {text!r}")
    if isinstance(default, str):
        return stripped
    if isinstance(default, int):
        try:
            return int(stripped)
        except ValueError:
            raise PreflightError(f"--set {key}: expected an integer, got {text!r}") from None
    if isinstance(default, float):
        try:
            return float(stripped)
        except ValueError:
            raise PreflightError(f"--set {key}: expected a number, got {text!r}") from None
    # No typed default (e.g. manage_invocation_logging): best-effort literal.
    if lowered in ("true", "false"):
        return lowered == "true"
    for convert in (int, float):
        try:
            return convert(stripped)
        except ValueError:
            continue
    return stripped


def parse_overrides(assignments: list[str] | None) -> dict[str, Any]:
    """Parse ``["key=value", ...]`` into a mapping of typed overrides."""
    overrides: dict[str, Any] = {}
    for assignment in assignments or ():
        key, separator, value = assignment.partition("=")
        key = key.strip()
        if not separator or not key:
            raise PreflightError(f"--set expects key=value, got {assignment!r}")
        overrides[key] = parse_override(key, value)
    return overrides


def apply_overrides(values: Mapping[str, Any], overrides: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(values)
    for key, value in overrides.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


def validate_values(
    values: Mapping[str, Any], *, base_dir: Path, account: str | None
) -> tuple[Any | None, list[str]]:
    """Validate ``values`` with the CDK app's validator.

    Returns ``(config, [])`` on success, ``(None, messages)`` when the values
    are rejected, and ``(None, [])`` when the configuration module is not
    importable (the caller decides whether that is acceptable).
    """
    module = configuration_module()
    if module is None:
        return None, []
    validate = getattr(module, "validate_mapping", None)
    if callable(validate):
        messages = list(validate(values, base_dir=base_dir, account=account))
        if messages:
            return None, messages
    try:
        config = module.DeploymentConfig.from_mapping(
            values, base_dir=base_dir, account=account
        )
    except (ValueError, TypeError, FileNotFoundError) as exc:
        return None, [str(exc)]
    return config, []


# --- session, Region, partition -------------------------------------------


def make_session(profile: str | None, region: str | None) -> boto3.session.Session:
    try:
        return boto3.session.Session(profile_name=profile, region_name=region)
    except ProfileNotFound as exc:
        raise PreflightError(str(exc)) from None


def resolve_region(
    explicit: str | None,
    session: boto3.session.Session,
    environ: Mapping[str, str] | None = None,
) -> str:
    """``--region`` > ``AWS_REGION`` > ``AWS_DEFAULT_REGION`` > profile default."""
    env = os.environ if environ is None else environ
    region = (
        explicit
        or env.get("AWS_REGION")
        or env.get("AWS_DEFAULT_REGION")
        or session.region_name
    )
    if not region:
        raise PreflightError(
            "no AWS Region configured: pass --region, set AWS_REGION, or add "
            "a region to the AWS profile"
        )
    return region


def partition_for_region(region: str, session: boto3.session.Session | None = None) -> str:
    if session is not None:
        try:
            partition = session.get_partition_for_region(region)
            if partition:
                return partition
        except Exception:  # unknown Region or old boto3: fall through
            pass
    for prefix, partition in _PARTITION_PREFIXES:
        if region.startswith(prefix):
            return partition
    return "aws"


def build_context(
    config_path: Path | str,
    *,
    profile: str | None = None,
    region: str | None = None,
    account_id: str | None = None,
    overrides: Mapping[str, Any] | None = None,
    options: Options | None = None,
    session: boto3.session.Session | None = None,
) -> Context:
    """Resolve session, Region, account and configuration into a Context.

    Raises :class:`PreflightError` for usage problems (missing Region,
    unknown profile, unreadable config) and for configuration values the CDK
    validator rejects; the messages are the validator's own.
    """
    path = Path(config_path)
    session = session or make_session(profile, region)
    region = resolve_region(region, session)
    if session.region_name != region:
        session = make_session(profile, region)
    values = apply_overrides(load_values(path), overrides or {})
    context = Context(
        session=session,
        region=region,
        account=account_id,
        partition=partition_for_region(region, session),
        profile=profile,
        raw=values,
        options=options or Options(),
        config_dir=path.resolve().parent,
        expected_account=account_id,
    )
    if context.account is None:
        # Best effort: the credentials check reports the failure properly.
        try:
            context.identity()
        except (ClientError, BotoCoreError, KeyError):
            pass
    config, messages = validate_values(
        values, base_dir=context.config_dir, account=context.account
    )
    if messages:
        raise PreflightError(
            "configuration rejected by cdk/stacks/configuration.py:\n  - "
            + "\n  - ".join(messages)
        )
    context.config = config
    return context
