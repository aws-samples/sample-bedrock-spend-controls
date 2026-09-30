import json
import os

import aws_cdk as cdk
import pytest
from aws_cdk import aws_lambda as lambda_
from aws_cdk.assertions import Match, Template

import cdk.stacks.spend_controls_stack as stack_module
from cdk.stacks.spend_controls_stack import SpendControlsStack


@pytest.fixture(autouse=True)
def no_asset_bundling(monkeypatch):
    inline = lambda_.Code.from_inline("def handler(event, context): return {}")
    original_from_asset = stack_module.lambda_.Code.from_asset

    def code_from_asset(path, *args, **kwargs):
        if str(path).endswith("quota_periods_layer"):
            return original_from_asset(path)
        return inline

    monkeypatch.setattr(
        stack_module.lambda_.Code,
        "from_asset",
        code_from_asset,
    )
    monkeypatch.setattr(
        stack_module.s3deploy.Source,
        "asset",
        lambda *args, **kwargs: stack_module.s3deploy.Source.data(
            "index.html", "<!doctype html>"
        ),
    )
    dist_dir = os.path.join(
        os.path.dirname(stack_module.__file__),
        "..",
        "..",
        "admin-ui",
        "dist",
    )
    os.makedirs(dist_dir, exist_ok=True)


def _template(context: dict) -> Template:
    app = cdk.App(context=context)
    stack = SpendControlsStack(
        app,
        "TestStack",
        env=cdk.Environment(
            account="111122223333", region="us-east-1"
        ),
    )
    return Template.from_stack(stack)


def _environment_with(template: Template, key: str) -> dict:
    for resource in template.find_resources("AWS::Lambda::Function").values():
        variables = (
            resource.get("Properties", {})
            .get("Environment", {})
            .get("Variables", {})
        )
        if key in variables:
            return variables
    raise AssertionError(f"No Lambda environment contains {key}")


def test_invocation_logging_requires_explicit_ownership_choice():
    with pytest.raises(ValueError, match="explicit consent"):
        _template({})
    with pytest.raises(ValueError, match="incompatible"):
        _template(
            {
                "manage_invocation_logging": True,
                "invocation_log_group_name": "/existing/group",
            }
        )
    with pytest.raises(ValueError, match="requires invocation_log_group_name"):
        _template({"manage_invocation_logging": False})


def test_stack_is_event_driven_with_iam_authenticated_function_url():
    template = _template({"manage_invocation_logging": True})

    # Emergency reconciliation (1 minute), the daily model price refresh,
    # the always-on revocation repair schedule, and the nightly auto-block
    # sweep.
    template.resource_count_is("AWS::Events::Rule", 4)
    template.resource_count_is("AWS::Logs::SubscriptionFilter", 1)
    # Exactly two users-table stream consumers: the emergency processor and
    # the enforcement dispatcher (DynamoDB Streams supports at most two).
    template.resource_count_is("AWS::Lambda::EventSourceMapping", 2)
    template.resource_count_is("AWS::SQS::Queue", 2)
    template.has_resource_properties(
        "AWS::Lambda::Url",
        {"AuthType": "AWS_IAM", "InvokeMode": "BUFFERED"},
    )
    rendered = json.dumps(template.to_json())
    assert "assumed-role/" in rendered
    assert "BedrockUserRole" in rendered
    assert "RevocationProcessorFn" in rendered
    assert "EnforcementDispatcherFn" in rendered
    assert any(
        logical_id.startswith("BrokerApiFn")
        for logical_id in template.find_resources(
            "AWS::Lambda::Function"
        )
    )
    outputs = template.to_json()["Outputs"]
    assert "BrokerApiUrl" in outputs
    assert "BrokerApiRoleArn" in outputs
    assert "GatewayUrl" not in outputs
    assert "GatewayRoleArn" not in outputs


def test_price_snapshot_is_injected_only_into_usage_processor():
    template = _template({"manage_invocation_logging": True})
    template.has_resource_properties(
        "Custom::BedrockModelPriceSnapshot",
        {
            "RegionCode": "us-east-1",
            "CatalogModels": Match.object_like(
                {"gpt-oss-120b": Match.any_value()}
            ),
            "PinnedPrices": Match.object_like(
                {
                    "anthropic.claude-opus-4-7": {
                        "input_per_mtok": 5.0,
                        "output_per_mtok": 25.0,
                    },
                    "global.anthropic.claude-opus-4-7": {
                        "input_per_mtok": 5.0,
                        "output_per_mtok": 25.0,
                    },
                    "us.anthropic.claude-opus-4-7": {
                        "input_per_mtok": 5.5,
                        "output_per_mtok": 27.5,
                    },
                }
            ),
        },
    )
    processor_env = _environment_with(template, "BEDROCK_USER_ROLE_NAME")
    broker_env = _environment_with(template, "BEDROCK_USER_ROLE_ARN")

    # The full snapshot lives in the SSM parameter (Lambda env is capped at
    # 4 KB); only the small fallback price rides along in the environment.
    assert "MODEL_PRICES_JSON" not in processor_env
    assert processor_env["MODEL_FALLBACK_PRICE_JSON"]["Fn::GetAtt"][1] == (
        "FallbackPriceJson"
    )
    assert processor_env["PRICES_PARAMETER_NAME"]
    assert "MODEL_PRICES_JSON" not in broker_env
    assert processor_env["BEDROCK_USER_ROLE_NAME"]


def test_defaults_are_injected_and_tables_are_destroyable_for_demo():
    template = _template({"manage_invocation_logging": True})
    broker_env = _environment_with(template, "BEDROCK_USER_ROLE_ARN")
    processor_env = _environment_with(template, "BEDROCK_USER_ROLE_NAME")

    assert broker_env["AUTO_PROVISION_USERS"] == "true"
    assert json.loads(broker_env["DEFAULT_LIMITS_JSON"]) == {
        "daily": {
            "usd": 1.0,
            "input_tokens": 1_000_000,
            "output_tokens": 200_000,
        },
        "weekly": None,
        "monthly": None,
    }
    assert broker_env["USAGE_RETENTION_DAYS"] == "35"
    assert broker_env["VENDED_CREDENTIAL_TTL_SECONDS"] == "900"
    assert "CREDENTIAL_ENFORCEMENT_MODE" not in broker_env
    assert broker_env["PERMISSION_LEASE_SECONDS"] == "300"
    assert broker_env["REFRESH_OVERLAP_SECONDS"] == "10"
    assert broker_env["REFRESH_JITTER_SECONDS"] == "5"
    assert broker_env["VEND_RATE_LIMIT_PER_MINUTE"] == "6"
    assert broker_env["REVOCATION_POLICY_SHARDS"] == "19"
    assert broker_env["REVOCATION_RECONCILE_MINUTES"] == "5"
    assert broker_env["REVOCATION_POLICY_MAX_CHARACTERS"] == "6144"
    assert "QUALIFICATION_STATUS_JSON" not in broker_env
    assert "OPERATIONS_ALARM_NAMES_JSON" in broker_env
    assert processor_env["WARN_THRESHOLD"] == "0.8"
    assert processor_env["USAGE_RETENTION_DAYS"] == "35"
    dashboard = json.dumps(
        next(iter(template.find_resources("AWS::CloudWatch::Dashboard").values()))
    )
    assert "DetectionLagMilliseconds" in dashboard
    assert "LeaseStarted" in dashboard
    assert "LeaseRefreshed" in dashboard
    assert "LeaseRetried" in dashboard

    template.resource_properties_count_is(
        "AWS::DynamoDB::Table",
        {
            "TimeToLiveSpecification": {
                "AttributeName": "expires_at",
                "Enabled": True,
            }
        },
        3,
    )
    for table in template.find_resources("AWS::DynamoDB::Table").values():
        assert table["DeletionPolicy"] == "Delete"


def test_production_values_and_table_retention():
    template = _template(
        {
            "manage_invocation_logging": True,
            "auto_provision_users": False,
            "default_limits": {
                "daily": {
                    "usd": 25,
                    "input_tokens": 10_000_000,
                    "output_tokens": 2_000_000,
                },
                "weekly": None,
                "monthly": None,
            },
            "warn_threshold": 0.75,
            "usage_retention_days": 90,
            "retain_tables_on_delete": True,
            "vended_ttl_seconds": 1800,
            "permission_lease_seconds": 300,
            "refresh_overlap_seconds": 20,
            "refresh_jitter_seconds": 7,
            "vend_rate_limit_per_minute": 8,
        }
    )
    broker_env = _environment_with(template, "BEDROCK_USER_ROLE_ARN")
    processor_env = _environment_with(template, "BEDROCK_USER_ROLE_NAME")
    assert broker_env["AUTO_PROVISION_USERS"] == "false"
    assert json.loads(broker_env["DEFAULT_LIMITS_JSON"])["daily"]["usd"] == 25.0
    assert broker_env["VENDED_CREDENTIAL_TTL_SECONDS"] == "1800"
    assert "CREDENTIAL_ENFORCEMENT_MODE" not in broker_env
    assert broker_env["PERMISSION_LEASE_SECONDS"] == "300"
    assert broker_env["REFRESH_OVERLAP_SECONDS"] == "20"
    assert broker_env["REFRESH_JITTER_SECONDS"] == "7"
    assert broker_env["VEND_RATE_LIMIT_PER_MINUTE"] == "8"
    assert processor_env["WARN_THRESHOLD"] == "0.75"
    assert processor_env["USAGE_RETENTION_DAYS"] == "90"
    for table in template.find_resources("AWS::DynamoDB::Table").values():
        assert table["DeletionPolicy"] == "Retain"
        assert table["UpdateReplacePolicy"] == "Retain"


@pytest.mark.parametrize("lease_seconds", [60, 300, 900])
def test_supported_permission_lease_durations_synthesize(lease_seconds):
    template = _template(
        {
            "manage_invocation_logging": True,
            "vended_ttl_seconds": 900,
            "permission_lease_seconds": lease_seconds,
            "refresh_overlap_seconds": 10,
            "refresh_jitter_seconds": 5,
        }
    )
    broker_env = _environment_with(template, "BEDROCK_USER_ROLE_ARN")
    assert broker_env["PERMISSION_LEASE_SECONDS"] == str(lease_seconds)


def test_revocation_layer_is_always_deployed():
    template = _template(
        {
            "manage_invocation_logging": True,
            "revocation_reconcile_minutes": 3,
        }
    )
    broker_env = _environment_with(template, "BEDROCK_USER_ROLE_ARN")
    assert "CREDENTIAL_ENFORCEMENT_MODE" not in broker_env
    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {"StreamSpecification": {"StreamViewType": "NEW_AND_OLD_IMAGES"}},
    )
    # Emergency processor + enforcement dispatcher: never a third stream
    # consumer (DynamoDB Streams supports at most two per shard).
    template.resource_count_is("AWS::Lambda::EventSourceMapping", 2)
    # Emergency reconcile, revocation reconcile, daily price refresh, and
    # the nightly auto-block sweep.
    template.resource_count_is("AWS::Events::Rule", 4)
    template.resource_count_is("AWS::SQS::Queue", 2)

    template.has_resource_properties(
        "AWS::Lambda::Function",
        Match.object_like(
            {
                "ReservedConcurrentExecutions": 1,
                "Environment": {
                    "Variables": Match.object_like(
                        {
                            "REVOCATION_POLICY_ARNS_JSON": Match.any_value(),
                            "REVOCATION_POLICY_MAX_CHARACTERS": "6144",
                        }
                    )
                },
            }
        ),
    )
    # The dispatcher is the stream consumer and fans out asynchronously.
    template.has_resource_properties(
        "AWS::Lambda::Function",
        Match.object_like(
            {
                "Environment": {
                    "Variables": Match.object_like(
                        {
                            "REVOCATION_FUNCTION_NAME": Match.any_value(),
                            "WORKLOAD_ENFORCER_FUNCTION_NAME": "",
                        }
                    )
                },
            }
        ),
    )
    rendered = json.dumps(template.to_json())
    assert rendered.count("__no_blocked_quota_identity__") == 19
    assert "iam:CreatePolicyVersion" in rendered
    assert "iam:DeletePolicyVersion" in rendered
    assert "RevocationSyncFailure" in rendered
    assert "RevocationPolicyOverflow" in rendered
    assert "revocation_failure" in rendered
    assert "revocation_overflow" in rendered
    assert "enforcement_dispatch_dlq" in rendered
    assert "enforcement_dispatch_iterator_age" in rendered
    role = next(
        value
        for logical_id, value in template.find_resources("AWS::IAM::Role").items()
        if logical_id.startswith("BedrockUserRole")
    )
    assert "PermissionsBoundary" in role["Properties"]

    with pytest.raises(ValueError, match="immutable 19-shard layout"):
        _template(
            {
                "manage_invocation_logging": True,
                "revocation_policy_shards": 18,
            }
        )


def test_enforcement_mode_key_is_rejected_everywhere():
    """The mode concept is gone: the key must fail loudly, not be ignored."""
    with pytest.raises(ValueError, match="Unknown deployment_config keys"):
        _template(
            {
                "deployment_config": {
                    "manage_invocation_logging": True,
                    "credential_enforcement_mode": "lease",
                }
            }
        )


def test_eight_hour_role_chained_session_is_rejected():
    with pytest.raises(ValueError, match="role-chaining maximum is 3600"):
        _template(
            {
                "manage_invocation_logging": True,
                "vended_ttl_seconds": 28_800,
            }
        )


def test_refresh_timing_must_fit_inside_permission_lease():
    with pytest.raises(ValueError, match="less than permission_lease_seconds"):
        _template(
            {
                "manage_invocation_logging": True,
                "permission_lease_seconds": 60,
                "refresh_overlap_seconds": 60,
            }
        )
    with pytest.raises(ValueError, match="less than refresh_overlap_seconds"):
        _template(
            {
                "manage_invocation_logging": True,
                "refresh_overlap_seconds": 10,
                "refresh_jitter_seconds": 10,
            }
        )

def test_vended_role_uses_runtime_iam_allowlist_without_bearer_permission():
    arns = [
        "arn:aws:bedrock:us-east-1::foundation-model/openai.gpt-oss-120b-1:0",
        "arn:aws:bedrock:us-east-1:111122223333:inference-profile/example",
    ]
    template = _template(
        {
            "manage_invocation_logging": True,
            "allowed_model_arns": arns,
        }
    )
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Action": [
                                    "bedrock:CountTokens",
                                    "bedrock:InvokeModel",
                                    "bedrock:InvokeModelWithResponseStream",
                                ],
                                "Effect": "Allow",
                                "Resource": arns,
                            }
                        )
                    ]
                )
            }
        },
    )
    assert "bedrock:CallWithBearerToken" not in json.dumps(
        [
            policy
            for policy in template.find_resources(
                "AWS::IAM::Policy"
            ).values()
            if "Allow" in json.dumps(policy)
        ]
    )


def test_existing_log_group_does_not_change_account_wide_configuration():
    template = _template(
        {
            "manage_invocation_logging": False,
            "invocation_log_group_name": "/existing/bedrock/invocations",
        }
    )
    template.resource_count_is("Custom::AWS", 0)
    template.resource_count_is("AWS::Logs::SubscriptionFilter", 1)


def test_managed_logging_configuration_is_retained():
    template = _template({"manage_invocation_logging": True})
    template.has_resource(
        "Custom::AWS",
        {
            "DeletionPolicy": "Retain",
            "UpdateReplacePolicy": "Retain",
            "Properties": Match.object_like({}),
        },
    )
    template.has_resource(
        "AWS::IAM::Role",
        {
            "DeletionPolicy": "Retain",
            "UpdateReplacePolicy": "Retain",
            "Properties": Match.object_like(
                {
                    "AssumeRolePolicyDocument": Match.object_like(
                        {
                            "Statement": Match.array_with(
                                [
                                    Match.object_like(
                                        {
                                            "Principal": {
                                                "Service": "bedrock.amazonaws.com"
                                            }
                                        }
                                    )
                                ]
                            )
                        }
                    )
                }
            ),
        },
    )


def test_invoker_principals_and_deny_policy_are_preserved():
    principal = "arn:aws:iam::111122223333:role/BrokerInvoker"
    template = _template(
        {
            "manage_invocation_logging": True,
            "invoker_principal_arns": [principal],
        }
    )
    template.has_resource_properties(
        "AWS::Lambda::Permission",
        {
            "Action": "lambda:InvokeFunctionUrl",
            "FunctionUrlAuthType": "AWS_IAM",
            "Principal": principal,
        },
    )
    rendered = json.dumps(template.to_json())
    assert "deny-direct-bedrock-invocation" in rendered
    assert "bedrock:CallWithBearerToken" in rendered
    assert "__emergency_stop_inactive__" in rendered
    assert "EmergencyStopFailure" in rendered
    assert "dynamodb:LeadingKeys" in rendered
    assert "CONFIG#EMERGENCY_STOP" in rendered
    assert "cloudwatch:GetMetricData" in rendered
    assert "cloudwatch:DescribeAlarms" in rendered
    assert "cloudwatch:*" not in rendered
    assert "emergency_failure" in rendered
    assert "emergency_dlq" in rendered
    assert "EmergencyDenyPolicyArn" in template.to_json()["Outputs"]
    assert "EmergencyKeySecretArn" in template.to_json()["Outputs"]


def test_admin_ui_remains_opt_in():
    disabled = _template({"manage_invocation_logging": True})
    disabled.resource_count_is("AWS::CloudFront::Distribution", 0)
    disabled.has_resource_properties(
        "AWS::Lambda::Url",
        Match.not_(Match.object_like({"Cors": Match.any_value()})),
    )
    enabled = _template(
        {
            "manage_invocation_logging": True,
            "admin_ui": True,
            "admin_jwt_claim": "cognito:groups",
            "admin_jwt_value": "quota-admins",
        }
    )
    enabled.resource_count_is("AWS::CloudFront::Distribution", 1)
    enabled.resource_count_is("AWS::Cognito::IdentityPool", 1)
    rendered = json.dumps(enabled.to_json())
    assert "createGroup" in rendered
    assert "GroupExistsException" in rendered
    assert "quota-admins" in rendered
    assert '"Exclude": ["config.js"]' in rendered
    function_url = next(
        iter(enabled.find_resources("AWS::Lambda::Url").values())
    )
    cors = function_url["Properties"]["Cors"]
    assert cors["AllowMethods"] == ["DELETE", "GET", "POST", "PUT"]
    assert "authorization" in cors["AllowHeaders"]
    assert cors["AllowOrigins"] != ["*"]
    assert "AdminUiDistribution" in json.dumps(cors["AllowOrigins"])


@pytest.mark.parametrize(
    "context",
    [
        {
            "admin_jwt_claim": "groups",
            "admin_jwt_value": "",
        },
        {
            "admin_jwt_claim": "",
            "admin_jwt_value": "quota-admins",
        },
    ],
)
def test_admin_jwt_claim_and_value_must_be_configured_together(context):
    with pytest.raises(ValueError, match="configured together"):
        _template({"manage_invocation_logging": True, **context})


def test_admin_ui_rejects_unsupported_or_unauthorized_identity_setup():
    # BYO issuer needs the SPA's public client id from the corporate IdP.
    with pytest.raises(ValueError, match="public OAuth client id"):
        _template(
            {
                "manage_invocation_logging": True,
                "admin_ui": True,
                "jwt_issuer": "https://idp.example.com",
                "admin_jwt_claim": "groups",
                "admin_jwt_value": "quota-admins",
            }
        )
    # ...and that client id is meaningless without the BYO UI.
    with pytest.raises(ValueError, match="applies only when"):
        _template(
            {
                "manage_invocation_logging": True,
                "admin_ui_client_id": "spa-client",
            }
        )
    with pytest.raises(ValueError, match="HTTPS origins"):
        _template(
            {
                "manage_invocation_logging": True,
                "admin_ui_connect_origins": ["http://plain.example.com"],
            }
        )
    with pytest.raises(ValueError, match="browser never receives"):
        _template(
            {
                "manage_invocation_logging": True,
                "admin_ui": True,
            }
        )


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("auto_provision_users", "yes", "must be true or false"),
        ("manage_invocation_logging", "yes", "must be true or false"),
        ("retain_tables_on_delete", "sometimes", "must be true or false"),
        ("snapstart", "1", "must be true or false"),
        ("usage_retention_days", 0, "positive integer"),
        ("usage_retention_days", 30, "at least 31"),
        ("warn_threshold", 0, "positive number"),
        ("warn_threshold", 1, "less than 1"),
        ("vended_ttl_seconds", 899, "between 900"),
        ("vended_ttl_seconds", 3_601, "role-chaining maximum is 3600"),
        ("permission_lease_seconds", 120, "one of 60, 300, 900"),
        ("refresh_overlap_seconds", 0, "positive integer"),
        ("refresh_jitter_seconds", -1, "non-negative integer"),
        ("vend_rate_limit_per_minute", 0, "positive integer"),
        ("revocation_policy_shards", 0, "positive integer"),
        ("revocation_policy_shards", 20, "emergency policy uses the twentieth"),
        ("revocation_reconcile_minutes", 0, "positive integer"),
        ("allowed_model_arns", [], "must not be empty"),
        ("allowed_model_arns", ["openai.model"], "resource ARN"),
        ("invoker_principal_arns", ["not-an-arn"], "principal ARNs"),
        ("reconciliation_enabled", "yes", "must be true or false"),
        ("reconcile_lag_days", 0, "positive integer"),
        ("reconcile_lag_days", 15, "at most 14"),
        ("reconciliation_alarm_percent", 0, "positive number"),
        ("reconciliation_alarm_percent", 101, "at most 100"),
    ],
)
def test_invalid_deployment_values_fail_synth(key, value, message):
    with pytest.raises(ValueError, match=message):
        _template({"manage_invocation_logging": True, key: value})

@pytest.mark.parametrize(
    ("limits", "message"),
    [
        (
            {"daily": None, "weekly": None, "monthly": None},
            "missing daily",
        ),
        (
            {
                "daily": {"usd": -1, "input_tokens": 1, "output_tokens": 1},
                "weekly": None,
                "monthly": None,
            },
            "non-negative number",
        ),
        (
            {
                "daily": {"usd": 1, "input_tokens": 1.5, "output_tokens": 1},
                "weekly": None,
                "monthly": None,
            },
            "non-negative integer",
        ),
    ],
)
def test_invalid_default_limits_fail_synth(limits, message):
    with pytest.raises(ValueError, match=message):
        _template(
            {
                "manage_invocation_logging": True,
                "default_limits": limits,
            }
        )


def _daily(**extra) -> dict:
    return {
        "daily": {
            "usd": 1,
            "input_tokens": 1_000,
            "output_tokens": 100,
            **extra,
        },
        "weekly": None,
        "monthly": None,
    }


def test_default_limits_thresholds_and_rate_synthesize_into_env():
    template = _template(
        {
            "manage_invocation_logging": True,
            "default_limits": {
                **_daily(
                    thresholds=[
                        {"at": 0.5, "action": "warn"},
                        {"at": 0.8, "action": "warn"},
                        {"at": 1.2, "action": "block"},
                    ]
                ),
                "weekly": {
                    "usd": 5,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    # Alert-only: no block entry.
                    "thresholds": [{"at": 1.0, "action": "warn"}],
                },
                "rate": {"rpm": 30, "tpm": 60_000},
            },
        }
    )
    broker_env = _environment_with(template, "BEDROCK_USER_ROLE_ARN")
    defaults = json.loads(broker_env["DEFAULT_LIMITS_JSON"])
    # Storage form (basis points) so the broker, processor, and enforcer
    # all materialize identical rows.
    assert defaults["daily"]["thresholds"] == [
        {"at_bps": 5000, "action": "warn"},
        {"at_bps": 8000, "action": "warn"},
        {"at_bps": 12000, "action": "block"},
    ]
    assert defaults["weekly"]["thresholds"] == [
        {"at_bps": 10000, "action": "warn"}
    ]
    assert defaults["rate"] == {"rpm": 30, "tpm": 60_000}
    # Both the broker and the processor receive the same deployment warn
    # ratio for rows that predate thresholds.
    processor_env = _environment_with(template, "BEDROCK_USER_ROLE_NAME")
    assert broker_env["WARN_THRESHOLD"] == processor_env["WARN_THRESHOLD"] == "0.8"


def test_default_limits_without_thresholds_or_rate_are_unchanged():
    template = _template({"manage_invocation_logging": True})
    defaults = json.loads(
        _environment_with(template, "BEDROCK_USER_ROLE_ARN")["DEFAULT_LIMITS_JSON"]
    )
    assert "thresholds" not in defaults["daily"]
    assert "rate" not in defaults


@pytest.mark.parametrize(
    ("thresholds", "message"),
    [
        (
            [{"at": 0.8, "action": "block"}, {"at": 1.0, "action": "block"}],
            "'block' entry must be the last",
        ),
        (
            [{"at": 1.0, "action": "block"}, {"at": 1.5, "action": "warn"}],
            "'block' entry must be the last",
        ),
        (
            [{"at": 0.8, "action": "warn"}, {"at": 0.8, "action": "block"}],
            "strictly increasing",
        ),
        (
            [{"at": 0.9, "action": "warn"}, {"at": 0.5, "action": "block"}],
            "strictly increasing",
        ),
        ([{"at": 0, "action": "warn"}], "positive number"),
        ([{"at": 10.5, "action": "block"}], "at most 10"),
        ([{"at": 0.5, "action": "alert"}], "action must be one of"),
        ([{"at": 0.5}], "must contain at and action"),
        ([{"at": 0.5, "action": "warn", "note": "x"}], "unknown keys"),
        ([], "non-empty list"),
        ("0.8", "non-empty list"),
    ],
)
def test_invalid_default_thresholds_fail_synth(thresholds, message):
    with pytest.raises(ValueError, match=message):
        _template(
            {
                "manage_invocation_logging": True,
                "default_limits": _daily(thresholds=thresholds),
            }
        )


@pytest.mark.parametrize(
    ("rate", "message"),
    [
        ({"rpm": -1}, "non-negative integer"),
        ({"tpm": 1.5}, "non-negative integer"),
        ({"rps": 10}, "unknown rps"),
        ("fast", "must be an object or null"),
    ],
)
def test_invalid_default_rate_limits_fail_synth(rate, message):
    with pytest.raises(ValueError, match=message):
        _template(
            {
                "manage_invocation_logging": True,
                "default_limits": {**_daily(), "rate": rate},
            }
        )


def test_model_pricing_accepts_validated_json_and_conservative_fallback():
    model_config = {
        "catalog_models": {"catalog-name": ["provider.dynamic-model"]},
        "price_overrides": {
            "provider.pinned-model": {
                "input_per_mtok": 7,
                "output_per_mtok": 21,
                "reason": "test model absent from the Pricing API catalog",
            }
        },
        "fallback_price": {
            "input_per_mtok": 50,
            "output_per_mtok": 100,
        },
    }
    template = _template(
        {
            "manage_invocation_logging": True,
            "model_config": model_config,
        }
    )
    template.has_resource_properties(
        "Custom::BedrockModelPriceSnapshot",
        {
            "CatalogModels": {
                "catalog-name": ["provider.dynamic-model"]
            },
            "PinnedPrices": {
                "provider.pinned-model": {
                    "input_per_mtok": 7.0,
                    "output_per_mtok": 21.0,
                }
            },
            "FallbackPrice": {
                "input_per_mtok": 50.0,
                "output_per_mtok": 100.0,
            },
        },
    )


def test_deployment_file_loads_and_context_overrides_it(tmp_path):
    model_path = tmp_path / "models.json"
    model_path.write_text(
        json.dumps(
            {
                "catalog_models": {"catalog": ["provider.model"]},
                "price_overrides": {},
                "fallback_price": {
                    "input_per_mtok": 20,
                    "output_per_mtok": 80,
                },
            }
        )
    )
    deployment_path = tmp_path / "deployment.json"
    deployment_path.write_text(
        json.dumps(
            {
                "manage_invocation_logging": True,
                "default_limits": {
                    "daily": {
                        "usd": 5,
                        "input_tokens": 1_000_000,
                        "output_tokens": 200_000,
                    },
                    "weekly": None,
                    "monthly": None,
                },
                "model_config": "models.json",
            }
        )
    )
    template = _template(
        {
            "deployment_config": str(deployment_path),
            "default_limits": {
                "daily": {
                    "usd": 9,
                    "input_tokens": 1_000_000,
                    "output_tokens": 200_000,
                },
                "weekly": None,
                "monthly": None,
            },
        }
    )
    assert (
        json.loads(
            _environment_with(template, "BEDROCK_USER_ROLE_ARN")[
                "DEFAULT_LIMITS_JSON"
            ]
        )["daily"]["usd"]
        == 9.0
    )


def test_model_config_rejects_duplicate_and_non_positive_prices():
    duplicate = {
        "catalog_models": {"catalog": ["provider.model"]},
        "price_overrides": {
            "provider.model": {
                "input_per_mtok": 1,
                "output_per_mtok": 2,
            }
        },
        "fallback_price": {
            "input_per_mtok": 20,
            "output_per_mtok": 80,
        },
    }
    with pytest.raises(ValueError, match="Duplicate model price mapping"):
        _template(
            {
                "manage_invocation_logging": True,
                "model_config": duplicate,
            }
        )
    duplicate["price_overrides"] = {}
    duplicate["fallback_price"]["input_per_mtok"] = 0
    with pytest.raises(ValueError, match="positive number"):
        _template(
            {
                "manage_invocation_logging": True,
                "model_config": duplicate,
            }
        )


def _pricing_context(overrides: dict, fallback: dict | None = None) -> dict:
    return {
        "manage_invocation_logging": True,
        "model_config": {
            "catalog_models": {"catalog": ["provider.model"]},
            "price_overrides": overrides,
            "fallback_price": fallback
            or {"input_per_mtok": 20, "output_per_mtok": 80},
        },
    }


def test_model_config_accepts_optional_cache_and_image_dimensions():
    template = _template(
        _pricing_context(
            {
                "provider.cached": {
                    "input_per_mtok": 1.0,
                    "output_per_mtok": 5.0,
                    "cache_read_per_mtok": 0.1,
                    # $0 cache write is legitimate (observed for Nova).
                    "cache_write_per_mtok": 0,
                    "reason": "test",
                },
                "provider.image": {
                    "input_per_mtok": 0,
                    "output_per_mtok": 0,
                    "per_image": 0.04,
                    "reason": "image model has no token rows",
                },
            },
            {
                "input_per_mtok": 20,
                "output_per_mtok": 80,
                "cache_read_per_mtok": 2,
                "cache_write_per_mtok": 25,
                "per_image": 0.1,
            },
        )
    )
    template.has_resource_properties(
        "Custom::BedrockModelPriceSnapshot",
        {
            "PinnedPrices": {
                "provider.cached": {
                    "cache_read_per_mtok": 0.1,
                    "cache_write_per_mtok": 0.0,
                    "input_per_mtok": 1.0,
                    "output_per_mtok": 5.0,
                },
                "provider.image": {
                    "input_per_mtok": 0.0,
                    "output_per_mtok": 0.0,
                    "per_image": 0.04,
                },
            },
            "FallbackPrice": Match.object_like({"per_image": 0.1}),
        },
    )


@pytest.mark.parametrize(
    ("price", "message"),
    [
        (
            {"input_per_mtok": 1, "output_per_mtok": 2, "video_per_second": 1},
            "unknown video_per_second",
        ),
        ({"input_per_mtok": 1}, "missing output_per_mtok"),
        (
            {"input_per_mtok": 1, "output_per_mtok": 2, "cache_read_per_mtok": -1},
            "non-negative number",
        ),
        # Zero token rates are only allowed for an image model (per_image > 0).
        ({"input_per_mtok": 0, "output_per_mtok": 0}, "positive number"),
        (
            {"input_per_mtok": 0, "output_per_mtok": 0, "per_image": 0},
            "positive number",
        ),
    ],
)
def test_model_config_rejects_invalid_price_dimensions(price, message):
    with pytest.raises(ValueError, match=message):
        _template(
            _pricing_context({"provider.bad": {**price, "reason": "test"}})
        )


def test_fallback_price_may_not_be_an_image_only_model():
    with pytest.raises(ValueError, match="fallback_price.input_per_mtok"):
        _template(
            _pricing_context(
                {},
                {"input_per_mtok": 0, "output_per_mtok": 0, "per_image": 0.5},
            )
        )


def test_reference_catalog_pins_cache_and_image_dimensions():
    """The shipped model-pricing.json exercises the new schema."""
    template = _template({"manage_invocation_logging": True})
    template.has_resource_properties(
        "Custom::BedrockModelPriceSnapshot",
        {
            "CatalogModels": Match.object_like(
                {"Nova Canvas": ["amazon.nova-canvas-v1:0"]}
            ),
            "PinnedPrices": Match.object_like(
                {
                    "anthropic.claude-opus-4-7": Match.object_like(
                        {"cache_read_per_mtok": 0.5, "cache_write_per_mtok": 6.25}
                    )
                }
            ),
        },
    )


def test_admin_audit_table_gateway_grant_and_safe_ui_cors():
    template = _template(
        {
            "manage_invocation_logging": True,
            "admin_ui": True,
            "admin_jwt_claim": "cognito:groups",
            "admin_jwt_value": "quota-admins",
        }
    )
    tables = template.find_resources("AWS::DynamoDB::Table")
    audit_logical_id, audit_table = next(
        (logical_id, resource)
        for logical_id, resource in tables.items()
        if resource["Properties"]["KeySchema"][0]["AttributeName"]
        == "subject_id"
    )
    properties = audit_table["Properties"]
    assert properties["KeySchema"] == [
        {"AttributeName": "subject_id", "KeyType": "HASH"},
        {"AttributeName": "event_key", "KeyType": "RANGE"},
    ]
    assert properties["TimeToLiveSpecification"] == {
        "AttributeName": "expires_at",
        "Enabled": True,
    }
    assert properties["GlobalSecondaryIndexes"][0]["IndexName"] == (
        "scope-event-key-index"
    )
    assert properties["GlobalSecondaryIndexes"][0]["KeySchema"] == [
        {"AttributeName": "scope", "KeyType": "HASH"},
        {"AttributeName": "event_key", "KeyType": "RANGE"},
    ]

    broker_env = _environment_with(template, "BEDROCK_USER_ROLE_ARN")
    processor_env = _environment_with(template, "BEDROCK_USER_ROLE_NAME")
    assert broker_env["ADMIN_AUDIT_TABLE"] == {"Ref": audit_logical_id}
    assert broker_env["ADMIN_AUDIT_RETENTION_DAYS"] == "365"
    assert "ADMIN_AUDIT_TABLE" not in processor_env

    policies = json.dumps(template.find_resources("AWS::IAM::Policy"))
    assert audit_logical_id in policies
    # Transactions are authorized by the item-level actions from
    # grant_read_write_data; the API-level name is not a valid IAM action.
    assert "dynamodb:TransactWriteItems" not in policies
    assert "dynamodb:PutItem" in policies
    assert "dynamodb:ConditionCheckItem" in policies

    function_url = next(
        iter(template.find_resources("AWS::Lambda::Url").values())
    )
    cors = function_url["Properties"]["Cors"]
    assert "if-match" in cors["AllowHeaders"]
    assert "idempotency-key" in cors["AllowHeaders"]
    # The browser sends the break-glass key with emergency-stop requests.
    assert "x-quota-emergency-key" in cors["AllowHeaders"]
    assert "etag" in cors["ExposeHeaders"]
    assert "x-quota-breached-period" in cors["ExposeHeaders"]
    assert "x-quota-breached-dimension" in cors["ExposeHeaders"]
    assert "x-quota-enabled-periods" in cors["ExposeHeaders"]
    assert "x-quota-resets-at" in cors["ExposeHeaders"]
    assert "x-request-id" in cors["ExposeHeaders"]
    assert cors["AllowOrigins"] != ["*"]
    assert function_url["Properties"]["AuthType"] == "AWS_IAM"


def test_period_helper_layer_and_usage_transaction_permission_are_wired():
    template = _template({"manage_invocation_logging": True})
    layers = template.find_resources("AWS::Lambda::LayerVersion")
    assert len(layers) == 1
    layer_id = next(iter(layers))

    functions = template.find_resources("AWS::Lambda::Function")
    broker = next(
        value
        for value in functions.values()
        if "BEDROCK_USER_ROLE_ARN"
        in value.get("Properties", {}).get("Environment", {}).get("Variables", {})
    )
    # WARN_THRESHOLD is shared with the scheduled enforcers; only the broker
    # and the usage processor auto-provision rows from DEFAULT_LIMITS_JSON.
    processor = next(
        value
        for value in functions.values()
        if "DEFAULT_LIMITS_JSON"
        in value.get("Properties", {}).get("Environment", {}).get("Variables", {})
        and "BEDROCK_USER_ROLE_ARN"
        not in value.get("Properties", {}).get("Environment", {}).get("Variables", {})
    )
    for function in (broker, processor):
        assert {"Ref": layer_id} in function["Properties"]["Layers"]

    processor_role = processor["Properties"]["Role"]["Fn::GetAtt"][0]
    processor_policies = [
        value
        for value in template.find_resources("AWS::IAM::Policy").values()
        if {"Ref": processor_role} in value["Properties"].get("Roles", [])
    ]
    rendered = json.dumps(processor_policies)
    # TransactWriteItems is not an IAM action; transactional writes are
    # authorized by the item-level actions on both tables.
    assert "dynamodb:TransactWriteItems" not in rendered
    for action in (
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:ConditionCheckItem",
    ):
        assert action in rendered
    assert "UsageTable" in rendered
    assert "UsersTable" in rendered


def test_admin_managed_login_reuses_demo_client_with_oauth():
    template = _template(
        {
            "manage_invocation_logging": True,
            "admin_ui": True,
            "admin_jwt_claim": "cognito:groups",
            "admin_jwt_value": "quota-admins",
        }
    )
    resources = template.to_json()["Resources"]

    # The client must stay scoped under the User Pool: its ID is the broker's
    # JWT_AUDIENCE, so moving it would replace the client and invalidate every
    # token issued for the old one.
    assert "DemoUserPool1AB98549" in resources
    assert "DemoUserPoolDemoAppClientB5870BCA" in resources
    assert "AdminUiDistribution580370A5" in resources
    client = resources["DemoUserPoolDemoAppClientB5870BCA"]["Properties"]
    assert client["GenerateSecret"] is False
    assert set(client["ExplicitAuthFlows"]) == {
        "ALLOW_USER_PASSWORD_AUTH",
        "ALLOW_USER_SRP_AUTH",
        "ALLOW_REFRESH_TOKEN_AUTH",
    }
    assert client["AllowedOAuthFlows"] == ["code"]
    assert client["AllowedOAuthFlowsUserPoolClient"] is True
    assert set(client["AllowedOAuthScopes"]) == {"openid", "email", "profile"}
    assert "implicit" not in json.dumps(client)
    assert "/auth/callback" in json.dumps(client["CallbackURLs"])
    assert '"/"' in json.dumps(client["LogoutURLs"])
    assert "AdminUiDistribution580370A5" in json.dumps(client["CallbackURLs"])

    template.has_resource_properties(
        "AWS::Cognito::UserPoolDomain",
        {
            "ManagedLoginVersion": 2,
            "UserPoolId": {"Ref": "DemoUserPool1AB98549"},
        },
    )
    template.has_resource_properties(
        "AWS::Cognito::ManagedLoginBranding",
        {
            "ClientId": {"Ref": "DemoUserPoolDemoAppClientB5870BCA"},
            "UserPoolId": {"Ref": "DemoUserPool1AB98549"},
            "UseCognitoProvidedValues": True,
        },
    )

    broker_env = _environment_with(template, "JWT_AUDIENCE")
    assert broker_env["JWT_AUDIENCE"] == {
        "Ref": "DemoUserPoolDemoAppClientB5870BCA"
    }
    identity_pool = next(
        iter(template.find_resources("AWS::Cognito::IdentityPool").values())
    )
    providers = identity_pool["Properties"]["CognitoIdentityProviders"]
    assert len(providers) == 1
    assert providers[0]["ClientId"] == {
        "Ref": "DemoUserPoolDemoAppClientB5870BCA"
    }
    assert providers[0]["ServerSideTokenCheck"] is True
    assert "DemoUserPool1AB98549" in json.dumps(providers[0]["ProviderName"])


def test_admin_runtime_config_security_headers_and_cors_have_no_secrets_or_cycle():
    template = _template(
        {
            "manage_invocation_logging": True,
            "admin_ui": True,
            "admin_jwt_claim": "cognito:groups",
            "admin_jwt_value": "quota-admins",
        }
    )
    resources = template.to_json()["Resources"]
    runtime_config = resources["AdminUiRuntimeConfigFB2880AD"]
    runtime_config_json = json.dumps(runtime_config["Properties"])
    assert "issuer" in runtime_config_json
    assert "clientId" in runtime_config_json
    assert "identityPoolId" in runtime_config_json
    # Obsolete keys from the Cognito-coupled UI must not come back.
    assert "cognitoDomain" not in runtime_config_json
    assert "userPoolId" not in runtime_config_json
    assert "AdminKey" not in runtime_config_json
    assert "EmergencyKey" not in runtime_config_json
    assert "SecretArn" not in runtime_config_json
    assert "clientSecret" not in runtime_config_json

    policies = template.find_resources("AWS::CloudFront::ResponseHeadersPolicy")
    assert len(policies) == 1
    policy_id, policy = next(iter(policies.items()))
    security = policy["Properties"]["ResponseHeadersPolicyConfig"][
        "SecurityHeadersConfig"
    ]
    assert security["ContentTypeOptions"]["Override"] is True
    assert security["FrameOptions"] == {
        "FrameOption": "DENY",
        "Override": True,
    }
    assert security["StrictTransportSecurity"] == {
        "AccessControlMaxAgeSec": 31_536_000,
        "IncludeSubdomains": True,
        "Override": True,
        "Preload": True,
    }
    csp = json.dumps(security["ContentSecurityPolicy"])
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "https://*.lambda-url.us-east-1.on.aws" in csp
    assert "cognito-identity.us-east-1." in csp
    # The SPA fetches the OIDC discovery document from the pool issuer.
    assert "cognito-idp.us-east-1." in csp
    assert "amazoncognito.com" in csp
    assert "BrokerApiFnFunctionUrl" not in csp
    csp_parts = security["ContentSecurityPolicy"]["ContentSecurityPolicy"][
        "Fn::Join"
    ][1]
    identity_origin = next(
        index
        for index, part in enumerate(csp_parts)
        if isinstance(part, str)
        and "https://cognito-identity.us-east-1." in part
    )
    assert csp_parts[identity_origin + 1] == {"Ref": "AWS::URLSuffix"}

    distribution = resources["AdminUiDistribution580370A5"]["Properties"]
    assert distribution["DistributionConfig"]["DefaultCacheBehavior"][
        "ResponseHeadersPolicyId"
    ] == {"Ref": policy_id}
    function_url = next(
        iter(template.find_resources("AWS::Lambda::Url").values())
    )
    assert function_url["Properties"]["AuthType"] == "AWS_IAM"
    cors = function_url["Properties"]["Cors"]
    assert cors["AllowOrigins"] != ["*"]
    assert cors["AllowOrigins"] == [
        {
            "Fn::Join": [
                "",
                [
                    "https://",
                    {
                        "Fn::GetAtt": [
                            "AdminUiDistribution580370A5",
                            "DomainName",
                        ]
                    },
                ],
            ]
        }
    ]


def test_demo_client_without_admin_ui_keeps_explicit_auth_and_disables_oauth():
    template = _template({"manage_invocation_logging": True})
    client = next(
        iter(template.find_resources("AWS::Cognito::UserPoolClient").values())
    )["Properties"]
    assert set(client["ExplicitAuthFlows"]) == {
        "ALLOW_USER_PASSWORD_AUTH",
        "ALLOW_USER_SRP_AUTH",
        "ALLOW_REFRESH_TOKEN_AUTH",
    }
    assert client["GenerateSecret"] is False
    assert "AllowedOAuthFlows" not in client
    assert "CallbackURLs" not in client
    template.resource_count_is("AWS::Cognito::UserPoolDomain", 0)
    template.resource_count_is("AWS::Cognito::ManagedLoginBranding", 0)


def test_price_refresh_schedule_parameter_and_fallback_alarm():
    template = _template({"manage_invocation_logging": True})

    # The deployment snapshot seeds a runtime SSM parameter combining the
    # resolved model prices and the conservative fallback (the other
    # parameter is the workload roster the admin API reads).
    parameters = template.find_resources("AWS::SSM::Parameter")
    assert len(parameters) == 2
    parameter = next(
        resource["Properties"]
        for logical_id, resource in parameters.items()
        if logical_id.startswith("ModelPricesParameter")
    )
    joined = parameter["Value"]["Fn::Join"][1]
    assert joined[0] == '{"models":'
    assert joined[2] == ',"fallback":'

    # A daily EventBridge rule targets the scheduled resolver entrypoint and
    # carries the pricing config in the event, mirroring the custom
    # resource's properties shape.
    schedule_rule = next(
        resource["Properties"]
        for resource in template.find_resources("AWS::Events::Rule").values()
        if resource["Properties"].get("ScheduleExpression") == "rate(1 day)"
    )
    rule_input = json.dumps(schedule_rule["Targets"][0]["Input"])
    assert "CatalogModels" in rule_input
    assert "PinnedPrices" in rule_input
    # Pinned prices flow without the deploy-time-only reason metadata.
    assert "reason" not in rule_input
    refresher = next(
        resource["Properties"]
        for name, resource in template.find_resources(
            "AWS::Lambda::Function"
        ).items()
        if name.startswith("PriceRefreshFn")
    )
    assert refresher["Handler"] == "handler.scheduled_handler"
    assert list(refresher["Environment"]["Variables"]) == [
        "PRICES_PARAMETER_NAME"
    ]

    # Metering reads the parameter; the snapshot is not duplicated into the
    # (4 KB-capped) environment.
    processor_env = _environment_with(template, "BEDROCK_USER_ROLE_NAME")
    assert "PRICES_PARAMETER_NAME" in processor_env
    assert "MODEL_PRICES_JSON" not in processor_env

    # Fallback-priced requests are an alarmed operational event.
    template.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {
            "MetricName": "FallbackPricedRequests",
            "Statistic": "Sum",
            "Threshold": 1,
            "TreatMissingData": "notBreaching",
        },
    )
    broker_env = _environment_with(template, "BEDROCK_USER_ROLE_ARN")
    alarm_names = broker_env["OPERATIONS_ALARM_NAMES_JSON"]
    assert "pricing_fallback" in json.dumps(alarm_names)


def test_price_overrides_require_a_documented_reason():
    from cdk.stacks.configuration import _model_pricing
    from pathlib import Path

    valid = {
        "catalog_models": {"gpt-oss-20b": ["openai.gpt-oss-20b"]},
        "price_overrides": {
            "x.model": {
                "input_per_mtok": 1.0,
                "output_per_mtok": 2.0,
                "reason": "catalog gap documented here",
            }
        },
        "fallback_price": {"input_per_mtok": 15.0, "output_per_mtok": 75.0},
    }
    pricing = _model_pricing(valid, Path("."))
    # The reason is deploy metadata; resolved overrides carry only rates.
    assert pricing.price_overrides["x.model"] == {
        "input_per_mtok": 1.0,
        "output_per_mtok": 2.0,
    }

    missing_reason = json.loads(json.dumps(valid))
    del missing_reason["price_overrides"]["x.model"]["reason"]
    with pytest.raises(ValueError, match="reason"):
        _model_pricing(missing_reason, Path("."))

    blank_reason = json.loads(json.dumps(valid))
    blank_reason["price_overrides"]["x.model"]["reason"] = "  "
    with pytest.raises(ValueError, match="reason"):
        _model_pricing(blank_reason, Path("."))


# ---------------------------------------------------------------------------
# Workload mode: inference profiles, direct-attach policies, enforcer
# ---------------------------------------------------------------------------

_WORKLOADS_CONTEXT = {
    "manage_invocation_logging": True,
    "workloads": json.dumps(
        {
            "workloads": [
                {
                    "name": "payments",
                    "model": "us.anthropic.claude-opus-4-7",
                    "role_arn": (
                        "arn:aws:iam::111122223333:role/payments-app"
                    ),
                },
                {
                    "name": "reports",
                    "model": "anthropic.claude-haiku-4-5-20251001-v1:0",
                },
            ]
        }
    ),
}


def _workload_roster_parameter(template: Template) -> dict:
    parameters = template.find_resources("AWS::SSM::Parameter")
    matches = [
        resource
        for logical_id, resource in parameters.items()
        if logical_id.startswith("WorkloadRosterParameter")
    ]
    assert len(matches) == 1, "exactly one workload roster parameter"
    return matches[0]["Properties"]


def _resolve_roster(value) -> dict:
    """Render the roster Fn::Join by substituting GetAtt tokens with their
    logical-id path, then parse the JSON the broker will read."""
    if isinstance(value, str):
        return json.loads(value)
    assert set(value) == {"Fn::Join"}
    delimiter, parts = value["Fn::Join"]
    rendered = delimiter.join(
        part if isinstance(part, str) else "/".join(part["Fn::GetAtt"])
        for part in parts
    )
    return json.loads(rendered)


def test_no_workloads_means_no_workload_resources():
    template = _template({"manage_invocation_logging": True})
    template.resource_count_is(
        "AWS::Bedrock::ApplicationInferenceProfile", 0
    )
    rendered = json.dumps(template.to_json())
    assert "WorkloadEnforcerFn" not in rendered
    # The roster parameter always exists (empty roster) so the broker has
    # one code path; the old inline env var is gone.
    assert "WORKLOAD_ENFORCEMENT_JSON" not in rendered
    assert _resolve_roster(_workload_roster_parameter(template)["Value"]) == {}
    env = _environment_with(template, "WORKLOAD_ROSTER_PARAMETER_NAME")
    assert env["WORKLOAD_TAG_KEY"] == "bedrock-spend-controls-workload"


def test_workload_roster_parameter_carries_identity_and_broker_reads_it():
    template = _template(_WORKLOADS_CONTEXT)
    parameter = _workload_roster_parameter(template)
    # Intelligent tiering: a large roster is promoted past the 4 KB
    # standard-parameter cap instead of failing the deploy.
    assert parameter["Tier"] == "Intelligent-Tiering"
    roster = _resolve_roster(parameter["Value"])
    assert set(roster) == {"workload:payments", "workload:reports"}
    payments = roster["workload:payments"]
    assert payments["name"] == "payments"
    assert payments["model"] == "us.anthropic.claude-opus-4-7"
    assert payments["role_arn"] == "arn:aws:iam::111122223333:role/payments-app"
    assert payments["enforcement_ready"] is True
    # The profile ARN is the deploy-time attribute of the profile resource.
    assert payments["profile_arn"] == (
        "WorkloadProfilePayments/InferenceProfileArn"
    )
    reports = roster["workload:reports"]
    assert reports["role_arn"] == ""
    assert reports["enforcement_ready"] is False
    # The broker is pointed at the parameter and may read it.
    broker_env = _environment_with(template, "WORKLOAD_ROSTER_PARAMETER_NAME")
    assert broker_env["WORKLOAD_ROSTER_PARAMETER_NAME"] == {
        "Ref": next(
            logical_id
            for logical_id in template.find_resources("AWS::SSM::Parameter")
            if logical_id.startswith("WorkloadRosterParameter")
        )
    }
    policies = json.dumps(template.find_resources("AWS::IAM::Policy"))
    assert "ssm:GetParameter" in policies


def test_workloads_create_profiles_with_tags_and_correct_model_sources():
    template = _template(_WORKLOADS_CONTEXT)
    template.resource_count_is(
        "AWS::Bedrock::ApplicationInferenceProfile", 2
    )
    template.has_resource_properties(
        "AWS::Bedrock::ApplicationInferenceProfile",
        {
            "InferenceProfileName": "spend-controls-payments",
            "Tags": [
                {"Key": "bedrock-spend-controls-workload", "Value": "payments"}
            ],
        },
    )
    template.has_resource_properties(
        "AWS::Bedrock::ApplicationInferenceProfile",
        {
            "InferenceProfileName": "spend-controls-reports",
            "Tags": [
                {"Key": "bedrock-spend-controls-workload", "Value": "reports"}
            ],
        },
    )
    # CopyFrom ARNs are Fn::Join over partition/region tokens; assert the
    # literal segments: CR profile IDs get account-scoped inference-profile
    # ARNs, plain model IDs get foundation-model ARNs (no account).
    rendered = json.dumps(template.to_json())
    assert ":inference-profile/us.anthropic.claude-opus-4-7" in rendered
    assert (
        "::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0"
        in rendered
    )


def test_direct_attach_policy_pins_the_role_to_its_profile():
    template = _template(_WORKLOADS_CONTEXT)
    policies = template.find_resources("AWS::IAM::Policy")
    attach = next(
        resource
        for logical_id, resource in policies.items()
        if logical_id.startswith("WorkloadInvokePolicyPayments")
    )
    assert attach["Properties"]["Roles"] == ["payments-app"]
    statements = attach["Properties"]["PolicyDocument"]["Statement"]
    by_sid = {statement["Sid"]: statement for statement in statements}
    assert set(by_sid) == {
        "InvokeOwnQuotaProfile",
        "InvokeRoutedModelsViaQuotaProfileOnly",
    }
    condition = by_sid["InvokeRoutedModelsViaQuotaProfileOnly"]["Condition"]
    assert "bedrock:InferenceProfileArn" in condition["StringEquals"]
    # The snippet-only workload must not synthesize an attachment.
    assert not any(
        logical_id.startswith("WorkloadInvokePolicyReports")
        for logical_id in policies
    )


def test_snippet_workload_emits_policy_output_and_not_ready_flag():
    template = _template(_WORKLOADS_CONTEXT)
    outputs = template.to_json()["Outputs"]
    assert any(
        key.startswith("WorkloadPolicySnippetReports") for key in outputs
    )
    assert not any(
        key.startswith("WorkloadPolicySnippetPayments") for key in outputs
    )
    assert any(
        key.startswith("WorkloadProfileArnPayments") for key in outputs
    )
    # enforcement_ready is derived from role_arn presence at synth time.
    roster = _resolve_roster(_workload_roster_parameter(template)["Value"])
    assert roster["workload:payments"]["enforcement_ready"] is True
    assert roster["workload:reports"]["enforcement_ready"] is False


def test_enforcer_wiring_least_privilege_and_schedules():
    template = _template(_WORKLOADS_CONTEXT)
    # Second subscription filter for profile-attributed traffic.
    template.resource_count_is("AWS::Logs::SubscriptionFilter", 2)
    # Emergency (1m), price refresh (24h), revocation repair (5m), nightly
    # auto-block sweep, and workload enforcement (5m).
    template.resource_count_is("AWS::Events::Rule", 5)
    # Still exactly two stream consumers: workload events arrive via the
    # enforcement dispatcher, never a third event source mapping.
    template.resource_count_is("AWS::Lambda::EventSourceMapping", 2)
    env = _environment_with(template, "WORKLOADS_JSON")
    assert env["DENY_POLICY_NAME"] == "bedrock-spend-controls-workload-deny"

    rendered = template.to_json()
    enforcer_policies = [
        statement
        for resource in rendered["Resources"].values()
        if resource["Type"] == "AWS::IAM::Policy"
        for statement in resource["Properties"]["PolicyDocument"][
            "Statement"
        ]
        if isinstance(statement.get("Action"), list)
        and "iam:PutRolePolicy" in statement["Action"]
    ]
    assert len(enforcer_policies) == 1
    statement = enforcer_policies[0]
    # Explicit ARN scoping: only the enrolled role, never a wildcard.
    assert statement["Resource"] == (
        "arn:aws:iam::111122223333:role/payments-app"
    )
    assert set(statement["Action"]) == {
        "iam:PutRolePolicy",
        "iam:DeleteRolePolicy",
        "iam:GetRolePolicy",
    }


def test_processor_receives_the_workload_profile_map():
    template = _template(_WORKLOADS_CONTEXT)
    env = _environment_with(template, "WORKLOAD_PROFILES_JSON")
    assert "DEFAULT_LIMITS_JSON" in env
    assert env["BEDROCK_USER_ROLE_NAME"]  # user path unchanged
    profiles = env["WORKLOAD_PROFILES_JSON"]
    rendered = json.dumps(profiles)
    assert "workload:payments" in rendered
    assert "workload:reports" in rendered


def test_workloads_config_validation_errors():
    with pytest.raises(ValueError, match="name must match"):
        _template(
            {
                "manage_invocation_logging": True,
                "workloads": json.dumps(
                    {"workloads": [{"name": "Bad_Name", "model": "m"}]}
                ),
            }
        )
    with pytest.raises(ValueError, match="must be unique"):
        _template(
            {
                "manage_invocation_logging": True,
                "workloads": json.dumps(
                    {
                        "workloads": [
                            {"name": "a", "model": "m"},
                            {"name": "a", "model": "m"},
                        ]
                    }
                ),
            }
        )
    with pytest.raises(ValueError, match="IAM role ARN"):
        _template(
            {
                "manage_invocation_logging": True,
                "workloads": json.dumps(
                    {
                        "workloads": [
                            {
                                "name": "a",
                                "model": "m",
                                "role_arn": "arn:aws:iam::1:user/x",
                            }
                        ]
                    }
                ),
            }
        )


def test_gateway_local_bundling_avoids_container_runtime(
    tmp_path, monkeypatch
):
    """Host pip bundling replaces Docker/Finch; the image is only a fallback."""
    source = tmp_path / "gateway"
    (source / "app").mkdir(parents=True)
    (source / "app" / "main.py").write_text("app = object()\n")
    (source / "requirements.txt").write_text("fastapi\n")
    (source / "run.sh").write_text("#!/bin/bash\n")
    output = tmp_path / "asset-output"
    output.mkdir()

    commands = []
    lookups = []

    def fake_run(command, check, shell, env):
        assert check is True
        assert shell is False  # argv list, never a shell string
        commands.append((command, env))

    def fake_which(name, path=None):
        lookups.append((name, path))
        return "/fake/bin/pip3"

    monkeypatch.setattr(stack_module.subprocess, "run", fake_run)
    monkeypatch.setattr(stack_module.shutil, "which", fake_which)
    bundler = stack_module.GatewayLocalBundling(str(source))

    assert bundler.try_bundle(str(output), image=None) is True
    # The program is a constant resolved through PATH, and that PATH puts
    # the CDK interpreter's own bin directory first so a project virtualenv
    # wins over a system pip.
    ((pip_command, env),) = commands
    assert pip_command[:2] == ["pip3", "install"]
    venv_bin = os.path.dirname(stack_module.sys.executable)
    assert env["PATH"].split(os.pathsep)[0] == venv_bin
    assert lookups == [("pip3", env["PATH"])]
    # Pinned to the Lambda target, not the host: x86_64 manylinux wheels
    # for CPython 3.12 into the asset output.
    for flag in (
        "manylinux2014_x86_64", "3.12", "--only-binary=:all:", str(output)
    ):
        assert flag in pip_command
    assert (output / "app" / "main.py").read_text() == "app = object()\n"
    assert os.access(output / "run.sh", os.X_OK)


def test_gateway_local_bundling_falls_back_when_host_pip_fails(
    tmp_path, monkeypatch
):
    source = tmp_path / "gateway"
    source.mkdir()

    def failing_run(command, check, shell, env):
        raise stack_module.subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(stack_module.subprocess, "run", failing_run)
    monkeypatch.setattr(
        stack_module.shutil, "which", lambda name, path=None: "/fake/bin/pip3"
    )
    bundler = stack_module.GatewayLocalBundling(str(source))

    assert bundler.try_bundle(str(tmp_path / "out"), image=None) is False


def test_gateway_local_bundling_falls_back_without_any_host_pip(
    tmp_path, monkeypatch
):
    """pip-less hosts (for example bare uv venvs) defer to the container."""
    monkeypatch.setattr(
        stack_module.shutil, "which", lambda name, path=None: None
    )
    bundler = stack_module.GatewayLocalBundling(str(tmp_path))

    assert bundler.try_bundle(str(tmp_path / "out"), image=None) is False


def test_admin_ui_with_byo_issuer_federates_through_an_oidc_provider():
    """admin_ui=true + jwt_issuer: UI hosting, Identity Pool, and config.js
    are wired to the corporate IdP instead of a demo Cognito pool."""
    template = _template(
        {
            "manage_invocation_logging": True,
            "admin_ui": True,
            "jwt_issuer": "https://idp.example.com",
            "jwt_audience": "data-plane-client",
            "admin_ui_client_id": "spa-client",
            "admin_ui_connect_origins": ["https://login.example.com"],
            "admin_jwt_claim": "groups",
            "admin_jwt_value": "quota-admins",
            "auto_provision_users": False,
        }
    )
    # No demo pool; the Identity Pool trusts the IAM OIDC provider instead.
    assert template.find_resources("AWS::Cognito::UserPool") == {}
    template.resource_count_is("Custom::AWSCDKOpenIdConnectProvider", 1)
    provider = next(
        iter(
            template.find_resources(
                "Custom::AWSCDKOpenIdConnectProvider"
            ).values()
        )
    )
    assert provider["Properties"]["Url"] == "https://idp.example.com"
    assert provider["Properties"]["ClientIDList"] == ["spa-client"]
    identity_pool = next(
        iter(template.find_resources("AWS::Cognito::IdentityPool").values())
    )
    assert identity_pool["Properties"].get("CognitoIdentityProviders") in (
        None,
        [],
    )
    assert identity_pool["Properties"]["OpenIdConnectProviderARNs"]

    resources = template.to_json()["Resources"]
    runtime_config_json = json.dumps(
        resources["AdminUiRuntimeConfigFB2880AD"]["Properties"]
    )
    assert "https://idp.example.com" in runtime_config_json
    assert "spa-client" in runtime_config_json
    assert "issuer" in runtime_config_json
    assert "cognitoDomain" not in runtime_config_json

    # CSP allows the issuer origin and the extra token-endpoint origin, and
    # keeps the demo-only Cognito hosts out.
    policy = next(
        iter(
            template.find_resources(
                "AWS::CloudFront::ResponseHeadersPolicy"
            ).values()
        )
    )
    csp = json.dumps(
        policy["Properties"]["ResponseHeadersPolicyConfig"][
            "SecurityHeadersConfig"
        ]["ContentSecurityPolicy"]
    )
    assert "https://idp.example.com" in csp
    assert "https://login.example.com" in csp
    assert "amazoncognito.com" not in csp
    assert "cognito-identity.us-east-1." in csp

    # The broker accepts both the data-plane and the SPA audiences.
    broker = next(
        env
        for resource in template.find_resources("AWS::Lambda::Function").values()
        if (env := resource["Properties"].get("Environment", {}).get("Variables", {}))
        and "JWT_AUDIENCE" in env
    )
    assert broker["JWT_AUDIENCE"] == "data-plane-client,spa-client"

    # The callback URL to register in the IdP is surfaced as an output.
    outputs = template.to_json()["Outputs"]
    assert "AdminUiCallbackUrl" in outputs


def test_admin_ui_byo_issuer_defaults_spa_client_to_the_shared_audience():
    template = _template(
        {
            "manage_invocation_logging": True,
            "admin_ui": True,
            "jwt_issuer": "https://idp.example.com",
            "jwt_audience": "shared-client",
            "admin_jwt_claim": "groups",
            "admin_jwt_value": "quota-admins",
            "auto_provision_users": False,
        }
    )
    provider = next(
        iter(
            template.find_resources(
                "Custom::AWSCDKOpenIdConnectProvider"
            ).values()
        )
    )
    assert provider["Properties"]["ClientIDList"] == ["shared-client"]
    broker = next(
        env
        for resource in template.find_resources("AWS::Lambda::Function").values()
        if (env := resource["Properties"].get("Environment", {}).get("Variables", {}))
        and "JWT_AUDIENCE" in env
    )
    # Same client for data plane and UI: no duplicate audience entry.
    assert broker["JWT_AUDIENCE"] == "shared-client"


def test_reconciliation_is_off_by_default_and_leaves_no_trace():
    template = _template({"manage_invocation_logging": True})
    rendered = json.dumps(template.to_json())
    assert "SpendReconciliationFn" not in rendered
    assert "SpendReconciliationDeltaAlarm" not in rendered
    assert "SpendReconciliationSchedule" not in rendered
    assert "ce:GetCostAndUsage" not in rendered
    assert "ReconciliationDeltaPercent" not in rendered  # no dashboard widget
    broker_env = _environment_with(template, "BEDROCK_USER_ROLE_ARN")
    assert broker_env["RECONCILIATION_ENABLED"] == "false"
    assert "RECONCILE_LAG_DAYS" not in broker_env
    assert "reconciliation_delta" not in json.dumps(
        broker_env["OPERATIONS_ALARM_NAMES_JSON"]
    )


def test_reconciliation_enabled_wires_lambda_schedule_alarm_and_least_privilege():
    template = _template(
        {
            **_WORKLOADS_CONTEXT,
            "reconciliation_enabled": True,
            "reconcile_lag_days": 3,
            "reconciliation_alarm_percent": 12.5,
        }
    )
    functions = template.find_resources("AWS::Lambda::Function")
    name, reconciler = next(
        (name, resource["Properties"])
        for name, resource in functions.items()
        if name.startswith("SpendReconciliationFn")
    )
    env = reconciler["Environment"]["Variables"]
    assert reconciler["Handler"] == "handler.handler"
    assert env["RECONCILE_LAG_DAYS"] == "3"
    assert env["USAGE_RETENTION_DAYS"] == "35"
    assert env["WORKLOAD_TAG_KEY"] == "bedrock-spend-controls-workload"
    assert env["METRICS_NAMESPACE"] == "BedrockSpendControls"
    assert "RECONCILE_REGION" in env and "USAGE_TABLE" in env
    # Same workload roster the processor and enforcer see, so per-workload
    # comparisons use the tag values the stack actually stamped.
    assert "payments" in json.dumps(env["WORKLOADS_JSON"])
    assert "reports" in json.dumps(env["WORKLOADS_JSON"])

    # Once a day, after Cost Explorer's refresh.
    schedule = next(
        resource["Properties"]
        for resource in template.find_resources("AWS::Events::Rule").values()
        if resource["Properties"].get("ScheduleExpression") == "cron(0 6 * * ? *)"
    )
    assert json.dumps(schedule["Targets"][0]["Arn"]).count(name) == 1

    # IAM: Cost Explorer read only (no other ce:* verb), usage table access,
    # SNS publish. Nothing on the users / audit tables.
    policies = template.find_resources("AWS::IAM::Policy")
    reconciler_role_ref = reconciler["Role"]["Fn::GetAtt"][0]
    reconciler_policy = next(
        resource["Properties"]["PolicyDocument"]
        for resource in policies.values()
        if any(
            isinstance(role, dict) and role.get("Ref") == reconciler_role_ref
            for role in resource["Properties"]["Roles"]
        )
    )
    ce_actions = [
        action
        for statement in reconciler_policy["Statement"]
        for action in (
            statement["Action"]
            if isinstance(statement["Action"], list)
            else [statement["Action"]]
        )
        if str(action).startswith("ce:")
    ]
    assert ce_actions == ["ce:GetCostAndUsage"]
    policy_text = json.dumps(reconciler_policy)
    assert "UsageTable" in policy_text
    assert "UsersTable" not in policy_text
    assert "AdminAuditTable" not in policy_text
    assert "sns:Publish" in policy_text

    # Alarm: two consecutive daily breaches over the configured percent.
    template.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {
            "MetricName": "ReconciliationDeltaPercent",
            "Namespace": "BedrockSpendControls",
            "Statistic": "Maximum",
            "Period": 86_400,
            "Threshold": 12.5,
            "EvaluationPeriods": 2,
            "ComparisonOperator": "GreaterThanThreshold",
            "TreatMissingData": "notBreaching",
        },
    )
    broker_env = _environment_with(template, "BEDROCK_USER_ROLE_ARN")
    assert broker_env["RECONCILIATION_ENABLED"] == "true"
    assert broker_env["RECONCILE_LAG_DAYS"] == "3"
    assert "reconciliation_delta" in json.dumps(
        broker_env["OPERATIONS_ALARM_NAMES_JSON"]
    )
    # Dashboard widget shows estimated vs billed with the delta on the right.
    dashboard = next(
        iter(template.find_resources("AWS::CloudWatch::Dashboard").values())
    )["Properties"]["DashboardBody"]
    dashboard_text = json.dumps(dashboard)
    assert "ReconciliationEstimatedUSD" in dashboard_text
    assert "ReconciliationBilledUSD" in dashboard_text
    assert "ReconciliationDeltaPercent" in dashboard_text



def test_lambda_environments_stay_under_the_4kb_service_limit():
    """Lambda rejects UpdateFunctionConfiguration when the environment
    exceeds 4 KB (seen in production as HTTP 413 once the resolved price
    snapshot grew cache/image dimensions). Resolve every token to a
    pessimistic literal (Ref -> 80-char name, GetAtt/Sub -> 128-char ARN)
    and keep 10% headroom. Real deployed sizes are lower (the broker is
    ~2.2 KB); the point is to fail synth, not deploy, when a JSON blob is
    added to an environment."""
    template = _template(
        {
            **_WORKLOADS_CONTEXT,
            "reconciliation_enabled": True,
        }
    )

    def literal_size(value) -> int:
        if isinstance(value, str):
            return len(value)
        if isinstance(value, dict):
            if "Fn::Join" in value:
                return sum(literal_size(part) for part in value["Fn::Join"][1])
            # Ref resolves to a generated name; GetAtt / Sub usually to an ARN.
            return 80 if "Ref" in value else 128
        if isinstance(value, list):
            return sum(literal_size(part) for part in value)
        return len(str(value))

    budget = int(4096 * 0.9)
    for name, resource in template.find_resources("AWS::Lambda::Function").items():
        env = resource["Properties"].get("Environment", {}).get("Variables", {})
        size = sum(len(key) + literal_size(value) for key, value in env.items())
        assert size <= budget, f"{name} environment ~{size} bytes exceeds {budget}-byte budget"


def test_auto_block_sweeper_is_always_deployed_with_nightly_cron_and_no_iam_actuator():
    """The sweep repairs the users-table side of the revocation layer.

    It is not opt-in: the revocation layer is always on and the sweep is what
    keeps its Deny shards from filling with users who never come back. It
    needs read/write on the users table (row flip + REVOCATION# sentinel in
    one transaction) and read on the usage table, but no IAM verbs: the
    revocation processor owns the policy documents.
    """
    template = _template({"manage_invocation_logging": True})
    functions = template.find_resources("AWS::Lambda::Function")
    name, sweeper = next(
        (name, resource["Properties"])
        for name, resource in functions.items()
        if name.startswith("AutoBlockSweeperFn")
    )
    env = sweeper["Environment"]["Variables"]
    assert sweeper["Handler"] == "handler.handler"
    assert sweeper["ReservedConcurrentExecutions"] == 1
    assert env["WARN_THRESHOLD"] == "0.8"
    assert env["USAGE_RETENTION_DAYS"] == "35"
    assert env["METRICS_NAMESPACE"] == "BedrockSpendControls"
    assert "USERS_TABLE" in env and "USAGE_TABLE" in env and "SNS_TOPIC_ARN" in env
    layer_id = next(iter(template.find_resources("AWS::Lambda::LayerVersion")))
    assert {"Ref": layer_id} in sweeper["Layers"]

    # Right after the UTC daily/weekly/monthly windows roll over.
    schedule = next(
        resource["Properties"]
        for resource in template.find_resources("AWS::Events::Rule").values()
        if resource["Properties"].get("ScheduleExpression") == "cron(5 0 * * ? *)"
    )
    assert json.dumps(schedule["Targets"][0]["Arn"]).count(name) == 1
    assert json.loads(schedule["Targets"][0]["Input"]) == {"source": "aws.events"}

    policies = template.find_resources("AWS::IAM::Policy")
    sweeper_role_ref = sweeper["Role"]["Fn::GetAtt"][0]
    sweeper_policy = next(
        resource["Properties"]["PolicyDocument"]
        for resource in policies.values()
        if any(
            isinstance(role, dict) and role.get("Ref") == sweeper_role_ref
            for role in resource["Properties"]["Roles"]
        )
    )
    policy_text = json.dumps(sweeper_policy)
    assert "iam:" not in policy_text
    assert "UsersTable" in policy_text
    assert "UsageTable" in policy_text
    assert "AdminAuditTable" not in policy_text
    assert "sns:Publish" in policy_text
    for action in ("dynamodb:Scan", "dynamodb:UpdateItem", "dynamodb:PutItem"):
        assert action in policy_text

    # A failed nightly pass stays in ALARM until the next pass can clear it.
    template.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {
            "MetricName": "AutoBlockSweepFailure",
            "Namespace": "BedrockSpendControls",
            "Statistic": "Sum",
            "Period": 86_400,
            "Threshold": 1,
            "EvaluationPeriods": 1,
            "TreatMissingData": "notBreaching",
        },
    )
    broker_env = _environment_with(template, "BEDROCK_USER_ROLE_ARN")
    assert "auto_block_sweep_failure" in json.dumps(
        broker_env["OPERATIONS_ALARM_NAMES_JSON"]
    )
