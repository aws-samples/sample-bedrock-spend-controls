"""``DeploymentConfig.from_mapping`` is the validation the installer tooling
shares with ``cdk synth``.

These tests pin that the two entry points agree (``from_node`` through a
real CDK app versus ``from_mapping`` on the same keys), that
``validate_mapping`` reports what synth would, and that the key
documentation (``KEY_DOCS``) stays in step with ``KNOWN_KEYS``, ``DEFAULTS``,
the shipped profile files, and docs/configuration.md.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import aws_cdk as cdk
import pytest

from cdk.stacks import configuration
from cdk.stacks.configuration import (
    DEFAULTS,
    KEY_DOCS,
    KEY_SECTIONS,
    KEY_TYPES,
    KNOWN_KEYS,
    PROFILES,
    DeploymentConfig,
    KeyDoc,
    describe_key,
    validate_mapping,
)

ROOT = Path(__file__).resolve().parents[1]
CDK_DIR = ROOT / "cdk"
CONFIG_DIR = CDK_DIR / "config"
DOCS_PATH = ROOT / "docs" / "configuration.md"
ACCOUNT = "111122223333"

# Keys the wizard asks only under "advanced" (part of the installer contract).
ADVANCED_KEYS = {
    "adapter_layer_arn",
    "admin_ui_connect_origins",
    "log_retention_days",
    "model_config",
    "reconcile_lag_days",
    "reconciliation_alarm_percent",
    "reconciliation_service_names",
    "refresh_jitter_seconds",
    "refresh_overlap_seconds",
    "reserve_enforcement_concurrency",
    "revocation_policy_shards",
    "revocation_reconcile_minutes",
    "snapstart",
    "vend_rate_limit_per_minute",
    "vended_ttl_seconds",
}


def _profile(name: str) -> dict:
    return json.loads((CONFIG_DIR / f"{name}.json").read_text(encoding="utf-8"))


def _from_node(context: dict, *, account: str | None = ACCOUNT) -> DeploymentConfig:
    """What the stack sees: context on a real app, read through a stack node."""
    app = cdk.App(context=context)
    stack = cdk.Stack(
        app, "Test", env=cdk.Environment(account=ACCOUNT, region="us-east-1")
    )
    return DeploymentConfig.from_node(stack.node, account=account)


def _minimal(**overrides) -> dict:
    return {"manage_invocation_logging": True, **overrides}


# --- from_mapping versus from_node ------------------------------------------


@pytest.mark.parametrize("profile", PROFILES)
def test_from_mapping_equals_from_node_for_shipped_profiles(profile):
    expected = _from_node({"deployment_config": f"config/{profile}.json"})
    actual = DeploymentConfig.from_mapping(
        _profile(profile), base_dir=CONFIG_DIR, account=ACCOUNT
    )
    assert actual == expected
    assert actual.default_limits_json == expected.default_limits_json
    assert actual.model_pricing.catalog_models  # the file really resolved


def test_from_node_overlays_context_on_the_file_before_validating():
    demo = _profile("demo")
    model_arn = "arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-5"
    expected = DeploymentConfig.from_mapping(
        {**demo, "warn_threshold": "0.5", "allowed_model_arns": model_arn},
        base_dir=CONFIG_DIR,
        account=ACCOUNT,
    )
    actual = _from_node(
        {
            "deployment_config": "config/demo.json",
            "warn_threshold": "0.5",
            "allowed_model_arns": model_arn,
            # Context paths resolve from cdk/, file paths from the file's
            # directory; both must land on the same catalog.
            "model_config": "config/model-pricing.json",
        }
    )
    assert actual == expected
    assert actual.warn_threshold == 0.5
    assert actual.allowed_model_arns == (model_arn,)


def test_context_default_limits_arrives_as_a_string_and_is_rejected():
    with pytest.raises(ValueError, match="default_limits must be an object"):
        _from_node(_minimal(default_limits=json.dumps(DEFAULTS["default_limits"])))


def test_context_relative_path_must_exist_under_cdk_or_cwd():
    with pytest.raises(ValueError, match="model_config file not found: nope.json"):
        _from_node(_minimal(model_config="nope.json"))


def test_unknown_keys_are_listed_in_sorted_order():
    with pytest.raises(
        ValueError,
        match="Unknown deployment_config keys: colour, enforcement_mode$",
    ):
        DeploymentConfig.from_mapping(
            _minimal(enforcement_mode="lease", colour="red"), base_dir=CDK_DIR
        )


def test_missing_keys_take_defaults_and_null_means_unset():
    config = DeploymentConfig.from_mapping(_minimal(), base_dir=CDK_DIR)
    assert config.vended_ttl_seconds == DEFAULTS["vended_ttl_seconds"]
    assert config.jwt_user_claim == DEFAULTS["jwt_user_claim"]
    assert config.allowed_model_arns == tuple(DEFAULTS["allowed_model_arns"])
    assert config.default_daily_limits.usd == 1.0
    assert config.default_weekly_limits is None
    assert config.workloads == ()
    assert config.alert_email == ""
    # An explicit null is "present", exactly as the file loader treats it.
    with pytest.raises(ValueError, match="explicit consent"):
        DeploymentConfig.from_mapping(
            {"manage_invocation_logging": None}, base_dir=CDK_DIR
        )


def test_relative_paths_resolve_from_base_dir_then_cwd(tmp_path, monkeypatch):
    (tmp_path / "models.json").write_text(
        json.dumps(
            {
                "catalog_models": {"catalog": ["provider.model"]},
                "price_overrides": {},
                "fallback_price": {"input_per_mtok": 20, "output_per_mtok": 80},
            }
        )
    )
    values = _minimal(model_config="models.json")
    config = DeploymentConfig.from_mapping(values, base_dir=tmp_path)
    assert config.model_pricing.catalog_models == {"catalog": ["provider.model"]}

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    with pytest.raises(ValueError, match="model_config file not found"):
        DeploymentConfig.from_mapping(values, base_dir=elsewhere)
    monkeypatch.chdir(tmp_path)
    assert DeploymentConfig.from_mapping(values, base_dir=elsewhere) == config


def test_account_is_only_enforced_when_known():
    values = _minimal(
        workloads={
            "workloads": [
                {
                    "name": "batch",
                    "model": "anthropic.claude-sonnet-5",
                    "role_arn": "arn:aws:iam::999988887777:role/Batch",
                }
            ]
        }
    )
    assert DeploymentConfig.from_mapping(values, base_dir=CDK_DIR).workloads[
        0
    ].workload_id == "workload:batch"
    assert validate_mapping(values, base_dir=CDK_DIR, account=ACCOUNT) == [
        "workloads[0].role_arn belongs to account 999988887777 but this stack "
        f"deploys to {ACCOUNT}; the enforcer can only manage roles in its own "
        "account"
    ]


# --- validate_mapping -------------------------------------------------------


@pytest.mark.parametrize(
    ("values", "message"),
    [
        (_minimal(vended_ttl_seconds=899), "vended_ttl_seconds must be between 900"),
        (
            _minimal(jwt_issuer="https://idp.example.com"),
            "jwt_audience is required with a bring-your-own jwt_issuer",
        ),
        (_minimal(mystery=1), "Unknown deployment_config keys: mystery"),
        ({}, "Bedrock model-invocation logging is an account + region-wide"),
    ],
)
def test_validate_mapping_reports_the_synth_error(values, message):
    errors = validate_mapping(values, base_dir=CDK_DIR, account=ACCOUNT)
    assert len(errors) == 1
    assert errors[0].startswith(message)
    with pytest.raises(ValueError) as raised:
        DeploymentConfig.from_mapping(values, base_dir=CDK_DIR, account=ACCOUNT)
    assert errors == [str(raised.value)]


@pytest.mark.parametrize("profile", PROFILES)
def test_validate_mapping_accepts_the_shipped_profiles(profile):
    assert (
        validate_mapping(_profile(profile), base_dir=CONFIG_DIR, account=ACCOUNT)
        == []
    )


# --- key documentation ------------------------------------------------------


def test_public_aliases_and_read_only_defaults():
    assert isinstance(KNOWN_KEYS, frozenset)
    assert configuration._DEPLOYMENT_KEYS is KNOWN_KEYS
    assert configuration._DEFAULTS is DEFAULTS
    assert set(DEFAULTS) == KNOWN_KEYS - {"manage_invocation_logging"}
    with pytest.raises(TypeError):
        DEFAULTS["snapstart"] = True  # type: ignore[index]


def test_every_known_key_has_exactly_one_key_doc_in_wizard_order():
    names = [doc.name for doc in KEY_DOCS]
    assert len(names) == len(set(names)) == len(KNOWN_KEYS)
    assert set(names) == KNOWN_KEYS
    section_order = [KEY_SECTIONS.index(doc.section) for doc in KEY_DOCS]
    assert section_order == sorted(section_order)
    for doc in KEY_DOCS:
        assert isinstance(doc, KeyDoc)
        assert doc.type in KEY_TYPES, doc.name
        assert doc.section in KEY_SECTIONS, doc.name
        assert doc.help.strip() == doc.help and doc.help, doc.name
        assert "\n" not in doc.help and not doc.help.endswith("."), doc.name
        assert doc.default == DEFAULTS.get(doc.name), doc.name
        assert describe_key(doc.name) is doc
    assert {doc.name for doc in KEY_DOCS if doc.advanced} == ADVANCED_KEYS
    with pytest.raises(KeyError):
        describe_key("admin_password")


_PYTHON_TYPES = {
    "bool": bool,
    "int": int,
    "float": (int, float),
    "string": str,
    "path": str,
    "string_list": list,
    "object": dict,
}


def test_key_doc_types_match_the_defaults():
    for doc in KEY_DOCS:
        if doc.default is None:
            assert doc.name == "manage_invocation_logging"
            continue
        assert isinstance(doc.default, _PYTHON_TYPES[doc.type]), doc.name
        if doc.type in {"int", "float"}:
            assert not isinstance(doc.default, bool), doc.name
        if doc.type == "string_list":
            assert all(isinstance(item, str) for item in doc.default), doc.name


def test_profile_defaults_are_read_from_the_shipped_files():
    profiles = {name: _profile(name) for name in PROFILES}
    for doc in KEY_DOCS:
        expected = {name: values.get(doc.name) for name, values in profiles.items()}
        assert dict(doc.profile_defaults) == expected, doc.name
        assert doc.profile_defaults == expected
    demo_ttl = describe_key("vended_ttl_seconds").profile_defaults
    assert demo_ttl["demo"] == 3600 and demo_ttl["production"] == 900
    assert describe_key("jwt_issuer").profile_defaults["demo"] is None
    with pytest.raises(KeyError):
        demo_ttl["staging"]


def _documented_keys() -> dict[str, str]:
    """``{key: default cell}`` from the Keys table in docs/configuration.md."""
    text = DOCS_PATH.read_text(encoding="utf-8")
    table = text.split("## Keys", 1)[1].split("\n## ", 1)[0]
    rows: dict[str, str] = {}
    for line in table.splitlines():
        match = re.match(r"^\| `([a-z_]+)` \| (.*?) \| .* \|$", line)
        if match:
            rows[match.group(1)] = match.group(2)
    return rows


def test_configuration_doc_lists_exactly_the_known_keys_with_their_defaults():
    rows = _documented_keys()
    assert set(rows) == KNOWN_KEYS
    literal_types = {"bool", "int", "float", "string_list"}
    for doc in KEY_DOCS:
        cell = rows[doc.name]
        if doc.type in literal_types and doc.default is not None:
            assert f"`{json.dumps(doc.default)}`" in cell, (doc.name, cell)
        elif doc.type in {"string", "path"} and doc.default:
            assert f"`{doc.default}`" in cell, (doc.name, cell)
