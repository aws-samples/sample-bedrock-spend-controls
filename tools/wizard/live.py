"""Live checks: the preflight checks run while the wizard asks questions.

:class:`LiveChecks` wraps a ``tools.preflight.Context`` for the target
account and Region. The wizard hands it the partial mapping (answers so far
plus template and defaults) and the name of one check; the result is shown
inline so the operator can fix the answer before moving on. It also lists
the Bedrock models the account can see (for the numbered model menu), reads
the Region's invocation logging configuration, and expands inference
profiles to their underlying foundation models. Everything is read-only.

``LiveChecks.connect`` returns ``None`` (after saying why) when there are no
usable credentials or no Region; the wizard then asks without live checks.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from tools.preflight import CheckResult, Options, run
from tools.preflight.context import (
    REPO_ROOT,
    Context,
    PreflightError,
    make_session,
    partition_for_region,
    resolve_region,
    validate_values,
)

from .console import Console

LABELS = {"pass": "PASS", "warn": "WARN", "fail": "FAIL", "skip": "SKIP"}
_INFERENCE_TYPES = frozenset({"ON_DEMAND", "INFERENCE_PROFILE"})


@dataclass(frozen=True)
class ModelChoice:
    """One entry of the model menu."""

    identifier: str  # foundation-model ID or inference-profile ID
    description: str
    kind: str  # "foundation-model" or "inference-profile"


@dataclass(frozen=True)
class ExistingLogging:
    """What Bedrock invocation logging currently delivers to in the Region."""

    description: str  # "CloudWatch log group X", "S3 bucket Y", ...
    log_group: str  # empty unless it is a CloudWatch log group


def describe_error(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        return f"{error.get('Code', 'ClientError')}: {error.get('Message', '')}".strip()
    return f"{type(exc).__name__}: {exc}"


class LiveChecks:
    """Runs preflight checks and Bedrock lookups for the wizard."""

    def __init__(self, console: Console, context: Context) -> None:
        self.console = console
        self.context = context
        self._models: list[ModelChoice] | None = None
        self._logging: ExistingLogging | None = None
        self._logging_read = False

    @property
    def account(self) -> str | None:
        return self.context.account

    @property
    def region(self) -> str:
        return self.context.region

    @property
    def partition(self) -> str:
        return self.context.partition

    @property
    def profile(self) -> str | None:
        return self.context.profile

    @classmethod
    def connect(
        cls,
        console: Console,
        *,
        profile: str | None,
        region: str | None,
        output_dir: Path,
        root: Path = REPO_ROOT,
    ) -> "LiveChecks | None":
        """Resolve credentials, Region and account; ``None`` when that fails."""
        try:
            session = make_session(profile, region)
            region = resolve_region(region, session)
            if session.region_name != region:
                session = make_session(profile, region)
        except PreflightError as exc:
            console.say(f"Live checks off: {exc}.")
            return None
        try:
            credentials = session.get_credentials()
        except (BotoCoreError, ClientError):
            credentials = None
        if credentials is None:
            console.say(
                "Live checks off: no AWS credentials found (pass --profile or set the AWS_* "
                "variables to check answers against the account)."
            )
            return None
        context = Context(
            session=session,
            region=region,
            partition=partition_for_region(region, session),
            profile=profile,
            config_dir=Path(output_dir),
            root=root,
        )
        try:
            context.identity()
        except (BotoCoreError, ClientError, KeyError) as exc:
            console.say(
                f"Live checks off: the AWS credentials are not usable ({describe_error(exc)})."
            )
            return None
        return cls(console, context)

    # --- checks ------------------------------------------------------------------

    def run(
        self, name: str, values: Mapping[str, Any], *, options: Options | None = None
    ) -> CheckResult:
        """Run one preflight check against ``values`` and show its result."""
        context = self.context
        context.raw = dict(values)
        context.options = options or Options()
        context.config, _ = validate_values(
            values, base_dir=context.config_dir, account=context.account
        )
        result = run(context, [name]).results[0]
        self.show(result)
        return result

    def show(self, result: CheckResult) -> None:
        self.console.say(f"  {LABELS[result.status]}  {result.title}")
        indent = " " * 8
        for line in result.detail.splitlines():
            self.console.say(f"{indent}{line}")
        if result.fix:
            self.console.say(f"{indent}fix: {result.fix}")

    # --- Bedrock lookups -------------------------------------------------------------

    def existing_logging(self) -> ExistingLogging | None:
        """The Region's invocation logging target; ``None`` when nothing is
        configured (or the setting could not be read)."""
        if not self._logging_read:
            self._logging = self._read_logging()
            self._logging_read = True
        return self._logging

    def _read_logging(self) -> ExistingLogging | None:
        try:
            response = self.context.clients("bedrock").get_model_invocation_logging_configuration()
        except (BotoCoreError, ClientError) as exc:
            self.console.say(
                f"  Could not read the invocation logging configuration ({describe_error(exc)})."
            )
            return None
        current = response.get("loggingConfig") or {}
        group = (current.get("cloudWatchConfig") or {}).get("logGroupName") or ""
        bucket = (current.get("s3Config") or {}).get("bucketName") or ""
        if group:
            return ExistingLogging(f"CloudWatch log group {group}", group)
        if bucket:
            return ExistingLogging(f"S3 bucket {bucket}", "")
        if current:
            return ExistingLogging("an existing configuration", "")
        return None

    def model_choices(self) -> list[ModelChoice]:
        """Active foundation models usable on demand or through inference
        profiles, then the Region's system-defined inference profiles."""
        if self._models is None:
            self._models = self._list_models()
        return self._models

    def _list_models(self) -> list[ModelChoice]:
        bedrock = self.context.clients("bedrock")
        try:
            summaries = bedrock.list_foundation_models().get("modelSummaries") or []
        except (BotoCoreError, ClientError) as exc:
            self.console.say(
                f"  Could not list foundation models ({describe_error(exc)}); enter ARNs by hand."
            )
            return []
        models: list[ModelChoice] = []
        for summary in summaries:
            if (summary.get("modelLifecycle") or {}).get("status", "ACTIVE") != "ACTIVE":
                continue
            types = set(summary.get("inferenceTypesSupported") or ())
            if types and not types & _INFERENCE_TYPES:
                continue
            model_id = summary.get("modelId")
            if not model_id:
                continue
            provider = summary.get("providerName", "")
            description = f"{provider} {summary.get('modelName', '')}".strip()
            models.append(ModelChoice(model_id, description, "foundation-model"))
        profiles: list[ModelChoice] = []
        token: str | None = None
        try:
            while True:
                kwargs: dict[str, Any] = {"typeEquals": "SYSTEM_DEFINED"}
                if token:
                    kwargs["nextToken"] = token
                page = bedrock.list_inference_profiles(**kwargs)
                for summary in page.get("inferenceProfileSummaries") or []:
                    if summary.get("status", "ACTIVE") != "ACTIVE":
                        continue
                    profile_id = summary.get("inferenceProfileId")
                    if profile_id:
                        name = summary.get("inferenceProfileName") or profile_id
                        profiles.append(
                            ModelChoice(
                                profile_id, f"inference profile: {name}", "inference-profile"
                            )
                        )
                token = page.get("nextToken")
                if not token:
                    break
        except (BotoCoreError, ClientError) as exc:
            self.console.say(f"  Could not list inference profiles ({describe_error(exc)}).")
        return models + profiles

    def foundation_model_arn(self, model_id: str) -> str:
        # Region wildcard, as in cdk/config/production.json: an inference
        # profile may route the request to another Region.
        return f"arn:{self.partition}:bedrock:*::foundation-model/{model_id}"

    def arns_for(self, choice: ModelChoice) -> list[str]:
        """IAM resource ARNs for a menu choice. An inference profile also
        contributes its underlying foundation models, which the vended
        session policy must allow for the profile to work."""
        if choice.kind != "inference-profile":
            return [self.foundation_model_arn(choice.identifier)]
        account = self.account or "*"
        arns = [f"arn:{self.partition}:bedrock:*:{account}:inference-profile/{choice.identifier}"]
        for model_id in self.profile_models(choice.identifier):
            arns.append(self.foundation_model_arn(model_id))
        return arns

    def profile_models(self, profile_id: str) -> list[str]:
        """Foundation-model IDs an inference profile routes to."""
        try:
            response = self.context.clients("bedrock").get_inference_profile(
                inferenceProfileIdentifier=profile_id
            )
        except (BotoCoreError, ClientError) as exc:
            self.console.say(
                f"  Could not resolve inference profile {profile_id} ({describe_error(exc)}); "
                "add its foundation-model ARNs by hand."
            )
            return []
        model_ids: list[str] = []
        for entry in response.get("models") or []:
            parts = str(entry.get("modelArn") or "").split(":", 5)
            if len(parts) == 6 and "/" in parts[5]:
                model_ids.append(parts[5].split("/", 1)[1])
        return list(dict.fromkeys(model_ids))
