"""The summary printed once every question is answered.

Three parts: the values that will be written (marking what differs from the
template), the monthly cost estimate from ``tools/estimate_cost.py`` for the
matching scenario (through the preflight ``quotas_cost`` check, so the two
never disagree), and the tasks other teams must do for the deployment to be
effective: registering the console callback URL with the identity provider,
applying the bypass-prevention SCP (read from DEPLOYMENT.md), enabling model
access, and granting the invoking principals.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from tools.preflight import execute
from tools.preflight.context import REPO_ROOT, Context, validate_values

from .flow import truthy

SCP_ROLE_PLACEHOLDER = "BEDROCK_USER_ROLE_ARN"
SCP_HEADING = "Prevent bypass"

# Mirrors DEPLOYMENT.md, section "Prevent bypass"; used only when that file
# cannot be read (for example when the wizard runs outside the checkout).
FALLBACK_SCP: dict[str, Any] = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "RequireQuotaBrokerForBedrockRuntime",
            "Effect": "Deny",
            "Action": [
                "bedrock:InvokeModel",
                "bedrock:InvokeModelWithResponseStream",
                "bedrock:StartAsyncInvoke",
                "bedrock:InvokeModelWithBidirectionalStream",
                "bedrock:CallWithBearerToken",
                "bedrock-mantle:CreateInference",
                "bedrock-mantle:CallWithBearerToken",
            ],
            "Resource": "*",
            "Condition": {"ArnNotEquals": {"aws:PrincipalArn": SCP_ROLE_PLACEHOLDER}},
        }
    ],
}


def scp_document(root: Path, *, extra_principals: Sequence[str] = ()) -> dict[str, Any]:
    """The SCP from DEPLOYMENT.md (first ```json block after the "Prevent
    bypass" heading), with the workload roles added to its exception list."""
    document: dict[str, Any] | None = None
    try:
        text = (root / "DEPLOYMENT.md").read_text(encoding="utf-8")
        start = text.index(SCP_HEADING)
        fence = text.index("```json", start) + len("```json")
        end = text.index("```", fence)
        parsed = json.loads(text[fence:end])
        if isinstance(parsed, dict) and parsed.get("Statement"):
            document = parsed
    except (OSError, ValueError):
        document = None
    if document is None:
        document = copy.deepcopy(FALLBACK_SCP)
    if extra_principals:
        for statement in document.get("Statement", []):
            condition = statement.get("Condition", {}).get("ArnNotEquals", {})
            current = condition.get("aws:PrincipalArn")
            if current is None:
                continue
            principals = [current] if isinstance(current, str) else list(current)
            principals.extend(arn for arn in extra_principals if arn not in principals)
            condition["aws:PrincipalArn"] = principals
    return document


def roster_entries(roster: Any) -> list[dict[str, Any]]:
    if isinstance(roster, Mapping) and isinstance(roster.get("workloads"), list):
        return [dict(entry) for entry in roster["workloads"] if isinstance(entry, Mapping)]
    return []


def tasks_for_other_teams(
    values: Mapping[str, Any],
    *,
    root: Path = REPO_ROOT,
    region: str | None = None,
    flagged_models: Sequence[str] = (),
    roster: Any = None,
) -> list[tuple[str, str]]:
    """``(title, body)`` pairs derived from the answers."""
    tasks: list[tuple[str, str]] = []
    issuer = str(values.get("jwt_issuer") or "").strip()
    admin_ui = truthy(values.get("admin_ui"))
    where = f" in {region}" if region else ""

    if issuer and admin_ui:
        client = values.get("admin_ui_client_id") or values.get("jwt_audience") or "<client>"
        tasks.append((
            "Identity provider team: register the console redirect URI",
            f"After the first deploy, add the AdminUiCallbackUrl stack output as a redirect URI of "
            f"the public SPA OAuth client '{client}' at {issuer} (DEPLOYMENT.md, "
            "'Admin console with your IdP').",
        ))
    if issuer:
        claims = [f"'{values.get('jwt_user_claim') or 'sub'}' (quota subject)"]
        admin_claim = str(values.get("admin_jwt_claim") or "").strip()
        if admin_claim:
            claims.append(
                f"'{admin_claim}' containing '{values.get('admin_jwt_value')}' for administrators"
            )
        tasks.append((
            "Identity provider team: token claims",
            f"Tokens issued by {issuer} for audience '{values.get('jwt_audience')}' must carry "
            + " and ".join(claims) + ".",
        ))

    entries = roster_entries(roster)
    workload_roles = [str(entry["role_arn"]) for entry in entries if entry.get("role_arn")]
    scp = json.dumps(scp_document(root, extra_principals=workload_roles), indent=2)
    tasks.append((
        "Organization or security team: prevent bypass",
        f"Apply this SCP (or attach the DenyDirectBedrockPolicyArn managed policy to every "
        f"non-vended role) so application principals cannot call Bedrock directly; replace "
        f"{SCP_ROLE_PLACEHOLDER} with the BedrockUserRoleArn stack output"
        + (" (workload roles are already listed)" if workload_roles else "")
        + ":\n" + scp,
    ))

    if flagged_models:
        tasks.append((
            "Bedrock account owner: enable model access",
            f"The live check found these models not enabled{where}: {', '.join(flagged_models)}. "
            "Enable them under Amazon Bedrock > Model access before deploying.",
        ))

    invokers = values.get("invoker_principal_arns") or []
    if isinstance(invokers, str):
        invokers = [item.strip() for item in invokers.split(",") if item.strip()]
    if invokers:
        tasks.append((
            "Owners of the invoking roles: allow calls to the broker",
            "Add lambda:InvokeFunctionUrl on the broker (BrokerApiUrl stack output) to the "
            "identity policies of: " + ", ".join(str(arn) for arn in invokers) + ".",
        ))

    if entries:
        with_roles = [entry["name"] for entry in entries if entry.get("role_arn")]
        without_roles = [entry["name"] for entry in entries if not entry.get("role_arn")]
        lines = []
        if with_roles:
            lines.append(
                "Workloads with a role (" + ", ".join(with_roles) + "): the enforcer manages an "
                "inline Deny on these roles; keep them out of other policy automation and in the "
                "SCP exception above."
            )
        if without_roles:
            lines.append(
                "Workloads without a role (" + ", ".join(without_roles) + "): provide the "
                "application's IAM role ARN to enable hard enforcement; until then the stack "
                "emits a policy snippet and the workload is metered and alerted only."
            )
        tasks.append(("Workload owners", "\n".join(lines)))
    return tasks


def cost_estimate(
    values: Mapping[str, Any], *, output_dir: Path, root: Path = REPO_ROOT, region: str | None
) -> str:
    """One line from the preflight ``quotas_cost`` check (offline)."""
    config, _ = validate_values(values, base_dir=output_dir, account=None)
    context = Context(
        session=None,  # the estimate reads files only
        region=region or "us-east-1",
        raw=dict(values),
        config=config,
        config_dir=output_dir,
        root=root,
    )
    result = execute("quotas_cost", context)
    return result.detail


def render_summary(
    written: Mapping[str, Any],
    *,
    changed: set[str],
    output_dir: Path,
    root: Path = REPO_ROOT,
    region: str | None = None,
    flagged_models: Sequence[str] = (),
    roster: Any = None,
) -> list[str]:
    """Lines of the summary: values, cost estimate, tasks for other teams."""
    lines = ["", "Summary", ""]
    width = max((len(key) for key in written), default=0)
    for key in sorted(written):
        text = json.dumps(written[key], sort_keys=True)
        mark = "  (changed from template)" if key in changed else ""
        lines.append(f"  {key.ljust(width)}  {text}{mark}")
    lines.append("")
    lines.append("Estimated monthly cost")
    estimate = cost_estimate(written, output_dir=output_dir, root=root, region=region)
    lines.extend(f"  {line}" for line in estimate.splitlines())
    lines.append("")
    lines.append("Tasks for other teams")
    tasks = tasks_for_other_teams(
        written, root=root, region=region, flagged_models=flagged_models, roster=roster
    )
    for number, (title, body) in enumerate(tasks, 1):
        lines.append(f"  {number}. {title}")
        for line in body.splitlines():
            lines.append(f"     {line}")
    return lines
