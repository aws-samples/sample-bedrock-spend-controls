"""The threat model is kept in three files that must agree.

`docs/threat-model.json` is the source of truth, `docs/threat-model.md` is
its narrative, and `docs/threat-model.tc.json` is the Threat Composer
workspace exported by `tools/threat_composer_export.py`. These tests fail
when a threat, asset, assumption, or data flow is edited in one place only,
when the export is stale, or when the JSON drifts from the shape the export
(and a Threat Composer import) relies on.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from threat_composer_export import LIMITS, build_workspace, render, render_statement  # noqa: E402

MODEL_PATH = ROOT / "docs" / "threat-model.json"
MD_PATH = ROOT / "docs" / "threat-model.md"
TC_PATH = ROOT / "docs" / "threat-model.tc.json"
PNG_PATH = ROOT / "assets" / "data-flow.png"
DOT_PATH = ROOT / "assets" / "data-flow.dot"
DRAWIO_PATH = ROOT / "assets" / "architecture.drawio"

STRIDE = {"Spoofing", "Tampering", "Repudiation", "Information disclosure", "Denial of service", "Elevation of privilege"}
GOALS = {"confidentiality", "integrity", "availability", "economy"}
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


@pytest.fixture(scope="module")
def model():
    return json.loads(MODEL_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def markdown():
    return MD_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def workspace():
    return json.loads(TC_PATH.read_text(encoding="utf-8"))


# --- the JSON is well formed for both the narrative and the export ------------


def test_every_threat_has_the_fields_the_export_needs(model):
    asset_ids = {a["id"] for a in model["assets"]}
    boundary_ids = {b["id"] for b in model["boundaries"]}
    ids = [t["id"] for t in model["threats"]]
    assert len(ids) == len(set(ids)), "duplicate threat id"
    for t in model["threats"]:
        assert re.fullmatch(r"T-\d\d", t["id"]), t["id"]
        assert t["boundary"] in boundary_ids, t["id"]
        assert set(t["stride"]) <= STRIDE and t["stride"], t["id"]
        assert t["status"] in model["status_legend"], t["id"]
        assert t["priority"] in {"High", "Medium", "Low"}, t["id"]
        g = t["grammar"]
        assert set(g) == {"threat_source", "prerequisites", "threat_action", "threat_impact", "impacted_goal", "impacted_assets"}, t["id"]
        for key in ("threat_source", "prerequisites", "threat_action", "threat_impact"):
            assert 0 < len(g[key]) <= LIMITS["grammar"], (t["id"], key)
        assert g["threat_action"][0].islower(), (t["id"], "threat_action must start lower-case for the grammar")
        assert set(g["impacted_goal"]) <= GOALS and g["impacted_goal"], t["id"]
        assert set(g["impacted_assets"]) <= asset_ids and g["impacted_assets"], t["id"]
        assert len(t["mitigation"]) <= LIMITS["content"], t["id"]
        assert t.get("mitigation_status", "will_not_action") in {"will_not_action", "in_progress"}, t["id"]


def test_assets_assumptions_and_dataflows_reference_real_ids(model):
    asset_ids = {a["id"] for a in model["assets"]}
    threat_ids = {t["id"] for t in model["threats"]}
    boundary_ids = {b["id"] for b in model["boundaries"]} | {"outside", "internal", "B3/B4"}
    assert [a["id"] for a in model["assets"]] == [f"A-{i}" for i in range(1, len(model["assets"]) + 1)]
    for a in model["assets"]:
        assert set(a["security_goals"]) <= GOALS and a["security_goals"], a["id"]
        assert len(a["name"]) <= 60 and a["description"], a["id"]
    assert [a["id"] for a in model["assumptions"]] == [f"AS-{i}" for i in range(1, len(model["assumptions"]) + 1)]
    for a in model["assumptions"]:
        assert set(a["linked_threats"]) <= threat_ids and a["linked_threats"], a["id"]
        assert 0 < len(a["content"]) <= LIMITS["content"], a["id"]
    assert [f["id"] for f in model["dataflows"]] == [f"DF-{i}" for i in range(1, len(model["dataflows"]) + 1)]
    for f in model["dataflows"]:
        assert f["boundary"] in boundary_ids, f["id"]
        assert set(f["assets"]) <= asset_ids, f["id"]
    # every asset is impacted by at least one threat and every boundary has at least one threat
    impacted = {a for t in model["threats"] for a in t["grammar"]["impacted_assets"]}
    assert impacted == asset_ids, asset_ids - impacted
    assert {t["boundary"] for t in model["threats"]} == {b["id"] for b in model["boundaries"]}


def test_summary_block_matches_the_threats(model):
    by_status = {status: sorted(t["id"] for t in model["threats"] if t["status"] == status) for status in model["status_legend"]}
    assert {k: sorted(v) for k, v in model["summary"].items()} == by_status
    assert not by_status["Open"], "an Open threat must be resolved or explicitly accepted before release"
    # a High-priority accepted risk must be one of the headline accepts an adopter is told about
    headline_ids = set(re.findall(r"T-\d\d", " ".join(model["headline_accepts"])))
    high_accepted = {t["id"] for t in model["threats"] if t["priority"] == "High" and t["status"] == "Accepted"}
    assert high_accepted <= headline_ids, high_accepted - headline_ids


# --- the narrative carries the same content ---------------------------------


def _unwrap_quote(block: str) -> str:
    return " ".join(line[2:].strip() for line in block.splitlines() if line.startswith(">"))


def test_markdown_states_every_threat_with_its_priority_and_grammar(model, markdown):
    asset_names = {a["id"]: a["name"] for a in model["assets"]}
    headings = re.findall(r"\*\*(T-\d\d) · [^*]*?\*\* Priority \*\*(High|Medium|Low)\*\*\.\n((?:> [^\n]*\n)+)", markdown)
    assert [h[0] for h in headings] == [t["id"] for t in model["threats"]], "threat order or set differs between md and json"
    for tid, priority, quote in headings:
        t = next(t for t in model["threats"] if t["id"] == tid)
        assert priority == t["priority"], tid
        assert _unwrap_quote(quote).rstrip(".") == render_statement(t["grammar"], asset_names), tid
        assert f"**Status: {t['status']}" in markdown.split(f"**{tid} · ", 1)[1].split("\n**T-", 1)[0], (tid, "status line missing or different")


def test_markdown_lists_every_asset_assumption_data_flow_and_boundary(model, markdown):
    for section, items in (("## Assets", model["assets"]), ("## Assumptions", model["assumptions"]), ("## Data flows", model["dataflows"]), ("## Trust boundaries", model["boundaries"])):
        body = markdown.split(section, 1)[1].split("\n## ", 1)[0]
        for item in items:
            assert f"| {item['id']} |" in body, (section, item["id"])
    assert "![Data flow diagram" in markdown and "assets/data-flow.png" in markdown
    counts = Counter(t["priority"] for t in model["threats"])
    assert f"**Medium** {counts['Medium']}; **Low** {counts['Low']}" in markdown
    assert f"| Mitigated | {len(model['summary']['Mitigated'])} |" in markdown
    assert f"| Accepted | {len(model['summary']['Accepted'])} |" in markdown


# --- the Threat Composer export is fresh and importable ----------------------


def test_threat_composer_export_is_up_to_date():
    assert TC_PATH.read_text(encoding="utf-8") == render(), "run tools/threat_composer_export.py"


def test_threat_composer_workspace_shape(model, workspace):
    assert workspace["schema"] == 1
    assert set(workspace) == {"schema", "applicationInfo", "architecture", "dataflow", "assumptions", "mitigations", "assumptionLinks", "mitigationLinks", "threats"}
    assert len(workspace["threats"]) == len(model["threats"]) == len(workspace["mitigations"])
    assert len(workspace["assumptions"]) == len(model["assumptions"])
    ids = {t["id"] for t in workspace["threats"]} | {m["id"] for m in workspace["mitigations"]} | {a["id"] for a in workspace["assumptions"]}
    assert all(UUID_RE.match(i) for i in ids), "ids must be UUIDv5 so re-exports diff minimally"
    assert len(ids) == len(workspace["threats"]) + len(workspace["mitigations"]) + len(workspace["assumptions"])
    threat_ids = {t["id"] for t in workspace["threats"]}
    assert {link["linkedId"] for link in workspace["mitigationLinks"]} == threat_ids, "every threat has exactly one linked mitigation"
    assert {link["mitigationId"] for link in workspace["mitigationLinks"]} == {m["id"] for m in workspace["mitigations"]}
    assert all(link["type"] == "Threat" and link["linkedId"] in threat_ids for link in workspace["assumptionLinks"])
    assert len(workspace["assumptionLinks"]) == sum(len(a["linked_threats"]) for a in model["assumptions"])
    for t in workspace["threats"]:
        keys = {m["key"] for m in t["metadata"]}
        assert keys == {"Comments", "Priority", "STRIDE"}, t["numericId"]
        assert t["status"] in {"threatIdentified", "threatResolved", "threatResolvedNotUseful"}
        assert t["statement"].startswith(("A ", "An ")) and len(t["statement"]) <= LIMITS["statement"]
        assert all(len(tag) <= LIMITS["tag"] for tag in t["tags"])
    for m in workspace["mitigations"]:
        assert m["status"] in {"mitigationIdentified", "mitigationInProgress", "mitigationResolved", "mitigationResolvedWillNotAction"}
        assert len(m["content"]) <= LIMITS["content"]
    will_not_action = {m["tags"][0] for m in workspace["mitigations"] if m["status"] == "mitigationResolvedWillNotAction"}
    assert will_not_action == {t["id"] for t in model["threats"] if t.get("mitigation_status") == "will_not_action"}


def test_threat_composer_export_embeds_the_data_flow_diagram(workspace):
    image = workspace["dataflow"]["image"]
    assert image.startswith("data:image/png;base64,iVBORw0KGgo"), "PNG signature"
    assert len(image) <= 1_000_000, "Threat Composer caps base64 images at 1 000 000 characters"
    assert PNG_PATH.exists() and DOT_PATH.exists()
    for section in ("### Data flows", "### Assets", "### Out of scope"):
        assert section in workspace["dataflow"]["description"]
    assert "| B1 |" in workspace["architecture"]["description"] and "| B7 |" in workspace["architecture"]["description"]


def test_export_is_deterministic_and_independent_of_the_image(model):
    first = build_workspace(model, None)
    second = build_workspace(model, None)
    assert first == second
    assert "image" not in first["dataflow"]
    with_image = build_workspace(model, b"\x89PNG\r\n\x1a\nfake")
    assert with_image["dataflow"]["image"].startswith("data:image/png;base64,")
    assert {k: v for k, v in with_image.items() if k != "dataflow"} == {k: v for k, v in first.items() if k != "dataflow"}


def test_architecture_drawio_has_the_data_flow_page_with_every_dot_node():
    xml = DRAWIO_PATH.read_text(encoding="utf-8")
    assert 'name="Runtime-only architecture"' in xml and 'name="Data flow (trust boundaries)"' in xml
    page = xml.split('name="Data flow (trust boundaries)"', 1)[1]
    dot_nodes = re.findall(r"^\s+(\w+)\s+\[label=", DOT_PATH.read_text(encoding="utf-8"), re.M)
    assert len(dot_nodes) >= 20
    for node in dot_nodes:
        assert f'<mxCell id="{node}"' in page, node
    for cluster in ("cluster_outside", "cluster_stack", "cluster_aws"):
        assert f'<mxCell id="{cluster}"' in page, cluster
