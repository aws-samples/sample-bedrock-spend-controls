"""Guarded non-production probe for permission leases and session revocation.

The default mode is a local dry run that prints the policies and planned AWS
operations. Live mode deliberately requires an exact account ID and role ARN
confirmation because it calls STS and temporarily changes one sandbox role's
permissions. It never creates or deletes the target role.

Live mode also refuses to run unless the sandbox is unmistakably set aside
for it: both the role and the managed deny policy must carry the tag
``bedrock-spend-controls:qualification=true``, the policy must not be
CloudFormation-managed (no ``aws:cloudformation:*`` tags), and its current
default document must be the probe placeholder (``probe_placeholder_policy``,
printed by the dry run). The probe deletes only policy versions it created.

Example dry run:

    python qualification/lease_revocation_probe.py \
      --role-arn arn:aws:iam::111122223333:role/quota-sandbox-role \
      --model-id anthropic.claude-3-haiku-20240307-v1:0

Live execution is intentionally not documented as a copy/paste command. Review
its dry-run JSON and obtain explicit approval for the named sandbox resources
before adding ``--execute`` and the confirmation arguments shown by the tool.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import unquote

import boto3
from botocore.exceptions import ClientError

BEDROCK_ACTIONS = (
    "bedrock:CountTokens",
    "bedrock:InvokeModel",
    "bedrock:InvokeModelWithResponseStream",
)
_ACCESS_DENIED_CODES = {
    "AccessDenied",
    "AccessDeniedException",
    "UnauthorizedOperation",
}
#: Required on both the sandbox role and the managed deny policy.
QUALIFICATION_TAG_KEY = "bedrock-spend-controls:qualification"
QUALIFICATION_TAG_VALUE = "true"
#: The managed policy's default version must be this do-nothing document.
PROBE_PLACEHOLDER_SID = "BedrockSpendControlsQualificationPlaceholder"
PROBE_PLACEHOLDER_IDENTITY = "bedrock-spend-controls-qualification-placeholder"
LEASE_CHOICES = (60, 300, 900)
# Vended STS keys must outlive the lease deadline so a post-deadline denial
# is a lease decision, not an expired token.
_LEASE_SESSION_MARGIN_SECONDS = 300
_MIN_SESSION_SECONDS = 900


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def lease_session_seconds(lease_seconds: int) -> int:
    """STS session duration for a lease probe: the lease plus a margin."""
    return max(_MIN_SESSION_SECONDS, lease_seconds + _LEASE_SESSION_MARGIN_SECONDS)


def probe_placeholder_policy() -> dict:
    """The document a dedicated probe policy must carry between runs.

    It denies Bedrock only to a source identity nobody uses, so attaching it
    changes nothing, and its presence proves the policy exists for this
    probe rather than for production revocation.
    """
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": PROBE_PLACEHOLDER_SID,
                "Effect": "Deny",
                "Action": list(BEDROCK_ACTIONS),
                "Resource": "*",
                "Condition": {
                    "StringEquals": {
                        "aws:SourceIdentity": [PROBE_PLACEHOLDER_IDENTITY]
                    }
                },
            }
        ],
    }


def _as_list(value) -> list:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def is_probe_placeholder(document) -> bool:
    """True when ``document`` is the placeholder (allowing IAM's normalisation)."""
    if not isinstance(document, dict):
        return False
    statements = _as_list(document.get("Statement"))
    if len(statements) != 1 or not isinstance(statements[0], dict):
        return False
    statement = statements[0]
    condition = statement.get("Condition")
    identities = (
        _as_list(condition.get("StringEquals", {}).get("aws:SourceIdentity"))
        if isinstance(condition, dict)
        and isinstance(condition.get("StringEquals"), dict)
        and len(condition) == 1
        else None
    )
    return (
        statement.get("Sid") == PROBE_PLACEHOLDER_SID
        and statement.get("Effect") == "Deny"
        and set(_as_list(statement.get("Action"))) == set(BEDROCK_ACTIONS)
        and _as_list(statement.get("Resource")) == ["*"]
        and identities == [PROBE_PLACEHOLDER_IDENTITY]
    )


def check_qualification_tags(tags: Iterable[dict], *, resource: str) -> None:
    """Refuse a role or policy that is not tagged for qualification."""
    for tag in tags:
        if (
            str(tag.get("Key", "")) == QUALIFICATION_TAG_KEY
            and str(tag.get("Value", "")).lower() == QUALIFICATION_TAG_VALUE
        ):
            return
    raise RuntimeError(
        f"sandbox {resource} is not tagged {QUALIFICATION_TAG_KEY}="
        f"{QUALIFICATION_TAG_VALUE}; refusing to touch a resource that was "
        "not set aside for qualification"
    )


def check_policy_tags(tags: Iterable[dict]) -> None:
    """The managed policy must be tagged and must not belong to a stack."""
    tags = list(tags)
    cfn = sorted(
        str(tag.get("Key", ""))
        for tag in tags
        if str(tag.get("Key", "")).startswith("aws:cloudformation:")
    )
    if cfn:
        raise RuntimeError(
            "managed policy is managed by CloudFormation (" + ", ".join(cfn)
            + "); the probe only versions a hand-made dedicated policy"
        )
    check_qualification_tags(tags, resource="policy")


def select_removable_probe_version(
    versions: Iterable[dict], probe_versions: Iterable[str]
) -> str | None:
    """Pick the oldest non-default version *this probe created*, or ``None``.

    Returns ``None`` when the policy still has room (fewer than five
    versions). Raises instead of touching a version the probe did not make.
    """
    versions = list(versions)
    if len(versions) < 5:
        return None
    owned = set(probe_versions)
    removable = sorted(
        (
            version
            for version in versions
            if not version.get("IsDefaultVersion")
            and version["VersionId"] in owned
        ),
        key=lambda version: version.get("CreateDate", _utc_now()),
    )
    if not removable:
        raise RuntimeError(
            "managed policy has five versions and none of them were created "
            "by this probe; remove old versions manually before re-running"
        )
    return removable[0]["VersionId"]


def _compact(document: dict) -> str:
    return json.dumps(document, separators=(",", ":"), sort_keys=True)


def _decode_policy_document(document) -> dict:
    if isinstance(document, dict):
        return document
    return json.loads(unquote(document))


def lease_policy(expires_at: datetime) -> dict:
    """Return a session policy that stops authorizing Bedrock at a fixed time."""
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise ValueError("lease expiration must be timezone-aware")
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "BedrockPermissionLease",
                "Effect": "Allow",
                "Action": list(BEDROCK_ACTIONS),
                "Resource": "*",
                "Condition": {
                    "DateLessThan": {
                        "aws:CurrentTime": expires_at.astimezone(
                            timezone.utc
                        ).isoformat().replace("+00:00", "Z")
                    }
                },
            }
        ],
    }


def source_identity_deny_policy(source_identities: Iterable[str]) -> dict:
    identities = sorted(set(source_identities))
    if not identities:
        raise ValueError("at least one source identity is required")
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "DenyBlockedQuotaIdentities",
                "Effect": "Deny",
                "Action": list(BEDROCK_ACTIONS),
                "Resource": "*",
                "Condition": {
                    "StringEquals": {"aws:SourceIdentity": identities}
                },
            }
        ],
    }


def role_account_and_name(role_arn: str) -> tuple[str, str]:
    parts = role_arn.split(":", 5)
    if len(parts) != 6 or parts[2] != "iam" or not parts[5].startswith("role/"):
        raise ValueError("role ARN must be an IAM role ARN")
    account_id = parts[4]
    role_name = parts[5][len("role/") :]
    if not account_id.isdigit() or len(account_id) != 12 or not role_name:
        raise ValueError("role ARN contains an invalid account or role name")
    return account_id, role_name


def _percentile(samples: list[float], percentile: float) -> float:
    if not samples:
        raise ValueError("at least one timing sample is required")
    ordered = sorted(samples)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def timing_summary(samples: Iterable[float]) -> dict[str, float | int]:
    values = list(samples)
    if not values:
        return {"samples": 0}
    return {
        "samples": len(values),
        "min_seconds": round(min(values), 3),
        "p50_seconds": round(statistics.median(values), 3),
        "p95_seconds": round(_percentile(values, 0.95), 3),
        "max_seconds": round(max(values), 3),
    }


@dataclass(frozen=True)
class ProbeConfig:
    role_arn: str
    model_id: str
    region: str
    profile: str = ""
    managed_policy_arn: str = ""
    confirm_managed_policy_arn: str = ""
    lease_seconds: tuple[int, ...] = (60, 300)
    revocation_samples: int = 5
    propagation_timeout_seconds: int = 300
    poll_seconds: float = 2.0
    execute: bool = False
    expected_account_id: str = ""
    confirm_role_arn: str = ""

    def validate(self) -> None:
        account_id, _ = role_account_and_name(self.role_arn)
        if not self.model_id:
            raise ValueError("model ID is required")
        if any(seconds not in LEASE_CHOICES for seconds in self.lease_seconds):
            raise ValueError("probe lease durations must be 60, 300, or 900 seconds")
        if self.revocation_samples < 1:
            raise ValueError("revocation sample count must be positive")
        if self.propagation_timeout_seconds < 1 or self.poll_seconds <= 0:
            raise ValueError("poll and timeout values must be positive")
        if self.execute:
            if self.expected_account_id != account_id:
                raise ValueError(
                    "live mode requires --expected-account-id matching the role ARN"
                )
            if self.confirm_role_arn != self.role_arn:
                raise ValueError(
                    "live mode requires --confirm-role-arn exactly matching --role-arn"
                )
            if not self.managed_policy_arn:
                raise ValueError(
                    "live mode requires a dedicated pre-attached managed policy ARN"
                )
            if self.confirm_managed_policy_arn != self.managed_policy_arn:
                raise ValueError(
                    "live mode requires --confirm-managed-policy-arn exactly "
                    "matching --managed-policy-arn"
                )
            policy_parts = self.managed_policy_arn.split(":", 5)
            if (
                len(policy_parts) != 6
                or policy_parts[2] != "iam"
                or policy_parts[4] != account_id
                or not policy_parts[5].startswith("policy/")
            ):
                raise ValueError(
                    "managed policy must be an IAM policy in the confirmed account"
                )


@dataclass
class ProbeResult:
    caller_arn: str = ""
    role_chaining_caller: bool = False
    lease_results: list[dict] | None = None
    deny_propagation: dict | None = None
    allow_propagation: dict | None = None
    isolation_preserved: bool | None = None
    one_hour_session_succeeded: bool | None = None
    above_one_hour_rejected: bool | None = None
    passed: bool = False

    def __post_init__(self) -> None:
        if self.lease_results is None:
            self.lease_results = []


def validate_probe_result(result: ProbeResult) -> None:
    failures: list[str] = []
    if not result.role_chaining_caller:
        failures.append("caller was not role chained")
    if result.one_hour_session_succeeded is not True:
        failures.append("one-hour role session did not succeed")
    if result.above_one_hour_rejected is not True:
        failures.append("3,601-second role session was not rejected")
    if not result.lease_results:
        failures.append("no permission-lease result was recorded")
    for lease in result.lease_results:
        if lease.get("allowed_before_deadline") is not True:
            failures.append(
                f"{lease.get('lease_seconds')}-second lease lacked pre-deadline access"
            )
        if lease.get("denied_after_deadline") is not True:
            failures.append(
                f"{lease.get('lease_seconds')}-second lease remained authorized"
            )
    if result.isolation_preserved is not True:
        failures.append("targeted deny affected the control identity")
    if failures:
        raise RuntimeError("probe qualification failed: " + "; ".join(failures))
    result.passed = True


class LiveProbe:
    """Runs live calls only after ``ProbeConfig.validate`` safety checks.

    Construction performs the read-only guards: the policy is attached to the
    role, both carry the qualification tag, the policy is not
    CloudFormation-managed, and its default document is the placeholder.
    ``iam_client`` / ``sts_client`` exist for tests.
    """

    def __init__(self, config: ProbeConfig, *, iam_client=None, sts_client=None):
        config.validate()
        if not config.execute:
            raise ValueError("LiveProbe requires execute=True")
        self.config = config
        self._session = boto3.Session(
            profile_name=config.profile or None,
            region_name=config.region,
        )
        self.sts = sts_client or self._session.client("sts")
        self.iam = iam_client or self._session.client("iam")
        _, self._role_name = role_account_and_name(config.role_arn)
        attached = self.iam.list_entities_for_policy(
            PolicyArn=config.managed_policy_arn,
            EntityFilter="Role",
        ).get("PolicyRoles", [])
        if self._role_name not in {
            str(entity.get("RoleName", "")) for entity in attached
        }:
            raise RuntimeError(
                "confirmed managed policy is not attached to the sandbox role"
            )
        check_qualification_tags(
            self.iam.list_role_tags(RoleName=self._role_name).get("Tags", []),
            resource="role",
        )
        check_policy_tags(
            self.iam.list_policy_tags(
                PolicyArn=config.managed_policy_arn
            ).get("Tags", [])
        )
        policy = self.iam.get_policy(
            PolicyArn=config.managed_policy_arn
        )["Policy"]
        self._original_version_id = policy["DefaultVersionId"]
        self._original_document = _decode_policy_document(
            self.iam.get_policy_version(
                PolicyArn=config.managed_policy_arn,
                VersionId=self._original_version_id,
            )["PolicyVersion"]["Document"]
        )
        if not is_probe_placeholder(self._original_document):
            raise RuntimeError(
                "managed policy's default version is not the probe placeholder "
                f"(statement Sid {PROBE_PLACEHOLDER_SID}); it may be carrying "
                "real revocations, so the probe will not version it"
            )
        self._probe_versions: list[str] = []

    @staticmethod
    def _session_kwargs(credentials: dict) -> dict:
        return {
            "aws_access_key_id": credentials["AccessKeyId"],
            "aws_secret_access_key": credentials["SecretAccessKey"],
            "aws_session_token": credentials["SessionToken"],
        }

    def _assume(
        self,
        source_identity: str,
        *,
        duration_seconds: int = 900,
        policy: dict | None = None,
    ) -> dict:
        kwargs = {
            "RoleArn": self.config.role_arn,
            "RoleSessionName": source_identity,
            "SourceIdentity": source_identity,
            "DurationSeconds": duration_seconds,
        }
        if policy is not None:
            encoded = _compact(policy)
            if len(encoded) > 2_048:
                raise ValueError("session policy exceeds the 2,048-character limit")
            kwargs["Policy"] = encoded
        return self.sts.assume_role(**kwargs)["Credentials"]

    def _runtime(self, credentials: dict):
        return self._session.client(
            "bedrock-runtime",
            region_name=self.config.region,
            **self._session_kwargs(credentials),
        )

    def _count_tokens_allowed(self, credentials: dict) -> bool:
        try:
            self._runtime(credentials).count_tokens(
                modelId=self.config.model_id,
                input={
                    "converse": {
                        "messages": [
                            {
                                "role": "user",
                                "content": [{"text": "quota probe"}],
                            }
                        ]
                    }
                },
            )
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in _ACCESS_DENIED_CODES:
                return False
            raise

    def _wait_for(self, check: Callable[[], bool], expected: bool) -> float:
        started = time.monotonic()
        deadline = started + self.config.propagation_timeout_seconds
        while time.monotonic() < deadline:
            if check() is expected:
                return time.monotonic() - started
            time.sleep(self.config.poll_seconds)
        raise TimeoutError(
            f"authorization did not become {expected} within "
            f"{self.config.propagation_timeout_seconds}s"
        )

    def _make_version_room(self) -> None:
        versions = self.iam.list_policy_versions(
            PolicyArn=self.config.managed_policy_arn
        ).get("Versions", [])
        version_id = select_removable_probe_version(versions, self._probe_versions)
        if version_id is None:
            return
        self.iam.delete_policy_version(
            PolicyArn=self.config.managed_policy_arn,
            VersionId=version_id,
        )
        self._probe_versions.remove(version_id)

    def _set_managed_policy_document(self, document: dict) -> None:
        self._make_version_room()
        response = self.iam.create_policy_version(
            PolicyArn=self.config.managed_policy_arn,
            PolicyDocument=_compact(document),
            SetAsDefault=True,
        )
        self._probe_versions.append(
            response["PolicyVersion"]["VersionId"]
        )

    def _put_targeted_deny(self, source_identity: str) -> None:
        self._set_managed_policy_document(
            source_identity_deny_policy([source_identity])
        )

    def _restore_permissions_for_sample(self) -> None:
        self._set_managed_policy_document(self._original_document)

    def _cleanup_policy_versions(self) -> None:
        self.iam.set_default_policy_version(
            PolicyArn=self.config.managed_policy_arn,
            VersionId=self._original_version_id,
        )
        for version_id in list(self._probe_versions):
            try:
                self.iam.delete_policy_version(
                    PolicyArn=self.config.managed_policy_arn,
                    VersionId=version_id,
                )
            except ClientError as exc:
                if (
                    exc.response.get("Error", {}).get("Code")
                    != "NoSuchEntity"
                ):
                    raise
            finally:
                if version_id in self._probe_versions:
                    self._probe_versions.remove(version_id)

    def _probe_role_chaining(self, result: ProbeResult) -> None:
        if not result.role_chaining_caller:
            raise RuntimeError(
                "probe caller is not an assumed-role session; role-chaining "
                "duration result would not qualify the Lambda broker"
            )
        source = "quota-probe-duration-" + uuid.uuid4().hex[:8]
        try:
            self._assume(source, duration_seconds=3_600)
            result.one_hour_session_succeeded = True
        except ClientError:
            result.one_hour_session_succeeded = False
            raise
        try:
            self._assume(source, duration_seconds=3_601)
        except ClientError:
            result.above_one_hour_rejected = True
        else:
            result.above_one_hour_rejected = False
            raise RuntimeError(
                "3,601-second AssumeRole unexpectedly succeeded for a "
                "role-chaining caller"
            )

    def _probe_leases(self, result: ProbeResult) -> None:
        for seconds in self.config.lease_seconds:
            expires_at = _utc_now() + timedelta(seconds=seconds)
            source = f"quota-probe-lease-{seconds}-{uuid.uuid4().hex[:6]}"
            credentials = self._assume(
                source,
                duration_seconds=lease_session_seconds(seconds),
                policy=lease_policy(expires_at),
            )
            allowed_before = self._count_tokens_allowed(credentials)
            if not allowed_before:
                raise RuntimeError(
                    f"{seconds}-second lease was denied before its deadline"
                )
            sleep_seconds = max(0.0, (expires_at - _utc_now()).total_seconds())
            time.sleep(sleep_seconds)
            denied_after_seconds = self._wait_for(
                lambda: self._count_tokens_allowed(credentials),
                False,
            )
            result.lease_results.append(
                {
                    "lease_seconds": seconds,
                    "allowed_before_deadline": allowed_before,
                    "denied_after_deadline": True,
                    "post_deadline_detection_seconds": round(
                        denied_after_seconds, 3
                    ),
                }
            )

    def _probe_targeted_revocation(self, result: ProbeResult) -> None:
        source_a = "quota-probe-a-" + uuid.uuid4().hex[:8]
        source_b = "quota-probe-b-" + uuid.uuid4().hex[:8]
        credentials_a = self._assume(source_a)
        credentials_b = self._assume(source_b)
        if not self._count_tokens_allowed(credentials_a):
            raise RuntimeError("probe identity A lacks baseline CountTokens access")
        if not self._count_tokens_allowed(credentials_b):
            raise RuntimeError("probe identity B lacks baseline CountTokens access")

        deny_samples: list[float] = []
        allow_samples: list[float] = []
        isolation = True
        for _ in range(self.config.revocation_samples):
            self._put_targeted_deny(source_a)
            deny_samples.append(
                self._wait_for(
                    lambda: self._count_tokens_allowed(credentials_a),
                    False,
                )
            )
            control_allowed = self._count_tokens_allowed(credentials_b)
            isolation = isolation and control_allowed
            self._restore_permissions_for_sample()
            allow_samples.append(
                self._wait_for(
                    lambda: self._count_tokens_allowed(credentials_a),
                    True,
                )
            )
            if not control_allowed:
                raise RuntimeError(
                    "targeted deny affected the control identity"
                )
        result.deny_propagation = timing_summary(deny_samples)
        result.allow_propagation = timing_summary(allow_samples)
        result.isolation_preserved = isolation

    def run(self) -> ProbeResult:
        caller = self.sts.get_caller_identity()
        role_account, _ = role_account_and_name(self.config.role_arn)
        if caller["Account"] != role_account:
            raise RuntimeError("caller account does not match the confirmed role account")
        result = ProbeResult(
            caller_arn=caller["Arn"],
            role_chaining_caller=":assumed-role/" in caller["Arn"],
        )
        try:
            self._probe_role_chaining(result)
            self._probe_leases(result)
            self._probe_targeted_revocation(result)
            validate_probe_result(result)
            return result
        finally:
            self._cleanup_policy_versions()


def dry_run_report(config: ProbeConfig) -> dict:
    config.validate()
    account_id, role_name = role_account_and_name(config.role_arn)
    example_expiry = datetime(2030, 1, 1, tzinfo=timezone.utc)
    lease_documents = {
        str(seconds): {
            "document": lease_policy(example_expiry),
            "compact_characters": len(_compact(lease_policy(example_expiry))),
        }
        for seconds in config.lease_seconds
    }
    return {
        "mode": "execute" if config.execute else "dry-run",
        "target": {
            "account_id": account_id,
            "role_name": role_name,
            "role_arn": config.role_arn,
            "region": config.region,
            "profile": config.profile or "(default credential chain)",
            "model_id": config.model_id,
            "managed_policy_arn": config.managed_policy_arn or "(required for live mode)",
        },
        "planned_operations": [
            "sts:GetCallerIdentity",
            "iam:ListEntitiesForPolicy, iam:ListRoleTags, iam:ListPolicyTags, "
            "iam:GetPolicy, iam:GetPolicyVersion (read-only guards)",
            "sts:AssumeRole at 3600 and 3601 seconds (role-chaining check)",
            "sts:AssumeRole with a session policy per lease duration ("
            + ", ".join(
                f"{seconds}s lease / {lease_session_seconds(seconds)}s session"
                for seconds in config.lease_seconds
            )
            + ")",
            "bedrock:CountTokens with two isolated source identities",
            "iam:CreatePolicyVersion on the pre-attached placeholder policy",
            "iam:SetDefaultPolicyVersion and DeletePolicyVersion of the "
            "versions this probe created (never pre-existing ones)",
        ],
        "prerequisites": {
            "required_tag": f"{QUALIFICATION_TAG_KEY}={QUALIFICATION_TAG_VALUE} "
            "on both the role and the managed policy",
            "policy_must_not_be_cloudformation_managed": True,
            "placeholder_policy_document": probe_placeholder_policy(),
        },
        "lease_policies": lease_documents,
        "targeted_deny_policy": source_identity_deny_policy(
            ["quota-probe-example"]
        ),
        "live_confirmation": {
            "expected_account_id": account_id,
            "confirm_role_arn": config.role_arn,
            "confirm_managed_policy_arn": (
                config.managed_policy_arn or "<dedicated sandbox policy ARN>"
            ),
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role-arn", required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument(
        "--profile",
        default="",
        help="AWS CLI profile for the approved sandbox; empty uses the default chain",
    )
    parser.add_argument(
        "--managed-policy-arn",
        default="",
        help="Dedicated pre-attached sandbox managed deny policy",
    )
    parser.add_argument(
        "--confirm-managed-policy-arn",
        default="",
        help="Required exact confirmation for live managed-policy versioning",
    )
    parser.add_argument(
        "--lease-seconds",
        type=int,
        nargs="+",
        default=[60, 300],
        choices=list(LEASE_CHOICES),
        help="Lease windows to probe (60, 300, 900); 900 waits 15 minutes",
    )
    parser.add_argument("--revocation-samples", type=int, default=5)
    parser.add_argument("--propagation-timeout-seconds", type=int, default=300)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--expected-account-id", default="")
    parser.add_argument("--confirm-role-arn", default="")
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = ProbeConfig(
        role_arn=args.role_arn,
        model_id=args.model_id,
        region=args.region,
        profile=args.profile,
        managed_policy_arn=args.managed_policy_arn,
        confirm_managed_policy_arn=args.confirm_managed_policy_arn,
        lease_seconds=tuple(args.lease_seconds),
        revocation_samples=args.revocation_samples,
        propagation_timeout_seconds=args.propagation_timeout_seconds,
        poll_seconds=args.poll_seconds,
        execute=args.execute,
        expected_account_id=args.expected_account_id,
        confirm_role_arn=args.confirm_role_arn,
    )
    if config.execute:
        payload = {"config": asdict(config), "result": asdict(LiveProbe(config).run())}
    else:
        payload = dry_run_report(config)
    rendered = json.dumps(payload, indent=2, default=str)
    print(rendered)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
