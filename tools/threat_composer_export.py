"""Export ``docs/threat-model.json`` as a Threat Composer workspace.

Threat Composer (https://github.com/awslabs/threat-composer) is the threat
modelling tool AWS security reviewers work with; its ``.tc.json`` workspace can
be imported into any deployment of the tool. This script derives that file from
the repository's own machine-readable threat model so the two never diverge:

* every ``T-n`` becomes a threat written in the threat grammar (*A [source]
  [prerequisites] can [action], which leads to [impact], resulting in reduced
  [goal] of [assets]*) with its STRIDE letters and priority;
* its mitigation, code references, and tests become a linked mitigation;
* ``AS-n`` assumptions are linked to the threats they underpin;
* the trust boundaries, assets, and ``DF-n`` data flows fill the architecture
  and data-flow sections, with ``assets/data-flow.png`` embedded.

Identifiers are UUIDv5 values derived from the stable ``T-n``/``AS-n`` ids, so
re-exporting after an edit yields a minimal diff. Usage::

    python tools/threat_composer_export.py          # writes docs/threat-model.tc.json
    python tools/threat_composer_export.py --check  # exit 1 if the file is stale
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "docs" / "threat-model.json"
OUTPUT = ROOT / "docs" / "threat-model.tc.json"
DATA_FLOW_PNG = ROOT / "assets" / "data-flow.png"

NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/aws-samples/amazon-bedrock-samples/poc-to-prod/bedrock-spend-controls/threat-model")

STRIDE_LETTERS = {
    "Spoofing": "S",
    "Tampering": "T",
    "Repudiation": "R",
    "Information disclosure": "I",
    "Denial of service": "D",
    "Elevation of privilege": "E",
}
THREAT_STATUS = {"Mitigated": "threatResolved", "Accepted": "threatResolved", "Open": "threatIdentified"}
MITIGATION_STATUS = {"Mitigated": "mitigationResolved", "Accepted": "mitigationResolved", "Open": "mitigationIdentified"}
MITIGATION_STATUS_OVERRIDE = {"will_not_action": "mitigationResolvedWillNotAction", "in_progress": "mitigationInProgress"}

# Threat Composer schema limits (schemas/threat-composer-v1.schema.json).
LIMITS = {"grammar": 200, "statement": 1400, "content": 1000, "comment": 1000, "tag": 30, "name": 200}


def _uuid(kind: str, key: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"{kind}:{key}"))


def _numeric(threat_id: str) -> int:
    return int(threat_id.split("-")[1])


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _article(source: str) -> str:
    return "An" if source[:1].lower() in "aeiou" else "A"


def _clip(text: str, limit: int, what: str) -> str:
    if len(text) > limit:
        raise ValueError(f"{what} exceeds the Threat Composer limit of {limit} characters ({len(text)}): {text[:60]}...")
    return text


def render_statement(grammar: dict[str, Any], asset_names: dict[str, str]) -> str:
    assets = _join([asset_names[a] for a in grammar["impacted_assets"]])
    goals = _join(grammar["impacted_goal"])
    statement = (
        f"{_article(grammar['threat_source'])} {grammar['threat_source']} {grammar['prerequisites']} "
        f"can {grammar['threat_action']}, which leads to {grammar['threat_impact']}, "
        f"resulting in reduced {goals} of {assets}"
    )
    return _clip(" ".join(statement.split()), LIMITS["statement"], "statement")


def _application_info(model: dict[str, Any]) -> dict[str, str]:
    legend = "\n".join(f"- **{k}** — {v}" for k, v in model["status_legend"].items())
    description = (
        f"### What this system protects\n\n{model['asset']}\n\n"
        f"### Method\n\n{model['method']}. Last updated {model['reviewed']}. "
        "The prose review with the reasoning behind every status lives in `docs/threat-model.md`; this workspace is "
        "generated from `docs/threat-model.json` by `tools/threat_composer_export.py`.\n\n"
        f"### Status legend (carried in each threat's Comments)\n\n{legend}\n\n"
        "### Headline accepted risks\n\n" + "\n".join(f"- {line}" for line in model["headline_accepts"])
    )
    return {"name": _clip(model["title"], LIMITS["name"], "application name"), "description": description}


def _architecture(model: dict[str, Any]) -> dict[str, str]:
    rows = "\n".join(
        f"| {b['id']} | {b['name']} | {b['crosses']} | {', '.join('`' + i + '`' for i in b['implemented_by'])} |"
        for b in model["boundaries"]
    )
    description = (
        "### Trust boundaries\n\nThe editable architecture diagram is `assets/architecture.drawio` "
        "(page *Runtime-only architecture*); the data-flow page of the same file draws these boundaries.\n\n"
        "| # | Boundary | Crosses | Implemented by |\n|---|---|---|---|\n" + rows
    )
    return {"description": description}


def _dataflow(model: dict[str, Any], asset_names: dict[str, str], image: bytes | None) -> dict[str, str]:
    flows = "\n".join(
        f"- **{f['id']}** ({f['boundary']}) — {f['source']} → {f['destination']}: {f['channel']}. Carries {f['carries']}"
        + (f" (assets: {', '.join(asset_names[a] for a in f['assets'])})." if f["assets"] else ".")
        for f in model["dataflows"]
    )
    assets = "\n".join(
        f"- **{a['name']}** ({a['id']}) — {a['description']} Goals: {', '.join(a['security_goals'])}."
        for a in model["assets"]
    )
    out = "\n".join(f"- {line}" for line in model["out_of_scope_assets"])
    description = (
        "### Data flows\n\nRed arrows in the diagram are trust-boundary crossings; `B-n` matches the architecture table.\n\n"
        f"{flows}\n\n### Assets\n\n{assets}\n\n### Out of scope\n\n{out}"
    )
    result = {"description": description}
    if image is not None:
        result["image"] = "data:image/png;base64," + base64.b64encode(image).decode("ascii")
    return result


def build_workspace(model: dict[str, Any], data_flow_png: bytes | None) -> dict[str, Any]:
    asset_names = {a["id"]: a["name"] for a in model["assets"]}
    boundary_names = {b["id"]: b["name"] for b in model["boundaries"]}
    threat_uuid = {t["id"]: _uuid("threat", t["id"]) for t in model["threats"]}

    threats, mitigations, mitigation_links = [], [], []
    for order, t in enumerate(model["threats"], start=1):
        g = t["grammar"]
        for key in ("threat_source", "prerequisites", "threat_action", "threat_impact"):
            _clip(g[key], LIMITS["grammar"], f"{t['id']} {key}")
        detail = f" ({t['status_detail']})" if t.get("status_detail") else ""
        residual = f" Residual: {t['residual_risk']}" if t.get("residual_risk") else ""
        comment = _clip(
            f"{t['id']} — status {t['status']}{detail}. Boundary {t['boundary']}: {boundary_names[t['boundary']]}.{residual}",
            LIMITS["comment"],
            f"{t['id']} comment",
        )
        threats.append(
            {
                "id": threat_uuid[t["id"]],
                "numericId": _numeric(t["id"]),
                "displayOrder": order,
                "metadata": [
                    {"key": "Comments", "value": comment},
                    {"key": "Priority", "value": t["priority"]},
                    {"key": "STRIDE", "value": [STRIDE_LETTERS[s] for s in t["stride"]]},
                ],
                "tags": [_clip(t["id"], LIMITS["tag"], "tag"), t["boundary"], t["status"]],
                "threatSource": g["threat_source"],
                "prerequisites": g["prerequisites"],
                "threatAction": g["threat_action"],
                "threatImpact": g["threat_impact"],
                "impactedGoal": list(g["impacted_goal"]),
                "impactedAssets": [asset_names[a] for a in g["impacted_assets"]],
                "statement": render_statement(g, asset_names),
                "status": THREAT_STATUS[t["status"]],
            }
        )
        evidence = []
        if t["code_references"]:
            evidence.append("Code: " + "; ".join(t["code_references"]))
        if t["tests"]:
            evidence.append("Tests: " + "; ".join(t["tests"]))
        mitigation_id = _uuid("mitigation", t["id"])
        mitigations.append(
            {
                "id": mitigation_id,
                "numericId": _numeric(t["id"]),
                "displayOrder": order,
                "metadata": [{"key": "Comments", "value": _clip(" | ".join(evidence) or "No code control; see the threat comment.", LIMITS["comment"], f"{t['id']} evidence")}],
                "tags": [t["id"]],
                "content": _clip(t["mitigation"], LIMITS["content"], f"{t['id']} mitigation"),
                "status": MITIGATION_STATUS_OVERRIDE.get(t.get("mitigation_status", ""), MITIGATION_STATUS[t["status"]]),
            }
        )
        mitigation_links.append({"mitigationId": mitigation_id, "linkedId": threat_uuid[t["id"]]})

    assumptions, assumption_links = [], []
    for order, a in enumerate(model["assumptions"], start=1):
        assumption_id = _uuid("assumption", a["id"])
        assumptions.append(
            {
                "id": assumption_id,
                "numericId": order,
                "displayOrder": order,
                "metadata": [{"key": "Comments", "value": _clip(f"{a['id']}; underpins {', '.join(a['linked_threats'])}.", LIMITS["comment"], "assumption comment")}],
                "tags": [a["id"]],
                "content": _clip(a["content"], LIMITS["content"], f"{a['id']} content"),
            }
        )
        for linked in a["linked_threats"]:
            assumption_links.append({"type": "Threat", "assumptionId": assumption_id, "linkedId": threat_uuid[linked]})

    return {
        "schema": 1,
        "applicationInfo": _application_info(model),
        "architecture": _architecture(model),
        "dataflow": _dataflow(model, asset_names, data_flow_png),
        "assumptions": assumptions,
        "mitigations": mitigations,
        "assumptionLinks": assumption_links,
        "mitigationLinks": mitigation_links,
        "threats": threats,
    }


def render(model_path: Path = MODEL, png_path: Path = DATA_FLOW_PNG) -> str:
    model = json.loads(model_path.read_text(encoding="utf-8"))
    png = png_path.read_bytes() if png_path.exists() else None
    return json.dumps(build_workspace(model, png), indent=2, ensure_ascii=False) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", maxsplit=1)[0])
    parser.add_argument("--check", action="store_true", help="exit 1 when docs/threat-model.tc.json differs from a fresh export")
    args = parser.parse_args()
    fresh = render()
    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current != fresh:
            sys.exit(f"{OUTPUT.relative_to(ROOT)} is stale; run tools/threat_composer_export.py")
        print(f"{OUTPUT.relative_to(ROOT)} is up to date")
        return
    OUTPUT.write_text(fresh, encoding="utf-8")
    workspace = json.loads(fresh)
    print(f"wrote {OUTPUT.relative_to(ROOT)}: {len(workspace['threats'])} threats, {len(workspace['mitigations'])} mitigations, {len(workspace['assumptions'])} assumptions")


if __name__ == "__main__":
    main()
