"""Render the threat-model data flow diagram from ``assets/data-flow.dot``.

Produces two artefacts from the single Graphviz source so the picture in the
threat model, the Threat Composer export, and the editable diagram never
disagree:

* ``assets/data-flow.png`` — the raster embedded by
  ``tools/threat_composer_export.py`` and linked from ``docs/threat-model.md``.
* the ``Data flow (trust boundaries)`` page of ``assets/architecture.drawio``
  — regenerated from the ``dot`` layout (node positions, cluster boxes) so it
  opens in draw.io with the same arrangement as the PNG. Any existing page
  with that name is replaced; the hand-drawn architecture page is untouched.

Requires Graphviz (``dot``) on ``PATH``. Usage::

    python tools/render_data_flow.py [--dpi 110]
"""

from __future__ import annotations

import argparse
import html
import json
import re
import subprocess  # nosec B404  # static argv; only the local Graphviz binary is executed
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOT = ROOT / "assets" / "data-flow.dot"
PNG = ROOT / "assets" / "data-flow.png"
DRAWIO = ROOT / "assets" / "architecture.drawio"
PAGE_ID = "data-flow"
PAGE_NAME = "Data flow (trust boundaries)"
DOT_BIN = "dot"
POINTS_PER_INCH = 72.0
MARGIN = 40  # drawio px around the layout

CLUSTER_STYLE = {
    # Graphviz cluster name -> (fill, stroke, font colour)
    "cluster_outside": ("#FAFAFA", "#9CA3AF", "#374151"),
    "cluster_stack": ("#F4F8FE", "#2563EB", "#1E3A8A"),
    "cluster_vend": ("#F8FBFF", "#93C5FD", "#1E40AF"),
    "cluster_meter": ("#F8FBFF", "#93C5FD", "#1E40AF"),
    "cluster_enforce": ("#F8FBFF", "#93C5FD", "#1E40AF"),
    "cluster_aws": ("#FFF8F0", "#D97706", "#92400E"),
}
CROSSING_COLOR = "#B91C1C"


def _run_dot(args: list[str]) -> bytes:
    try:
        return subprocess.run(  # nosec B603  # nosemgrep -- static argv, no shell
            [DOT_BIN, *args], check=True, capture_output=True
        ).stdout
    except FileNotFoundError:  # pragma: no cover - depends on the host
        sys.exit("Graphviz 'dot' was not found on PATH; install graphviz to render the data flow diagram.")


def render_png(dpi: int) -> None:
    PNG.write_bytes(_run_dot(["-Tpng", f"-Gdpi={dpi}", str(DOT)]))


def _layout() -> dict:
    return json.loads(_run_dot(["-Tjson", str(DOT)]))


def _label(raw: str) -> str:
    return html.escape(raw.replace("\\n", "\n"), quote=True).replace("\n", "&lt;br&gt;")


def _bb(bb: str, height: float) -> tuple[float, float, float, float]:
    """Graphviz bb 'llx,lly,urx,ury' (y up, points) -> drawio x,y,w,h (y down, px)."""
    llx, lly, urx, ury = (float(v) for v in bb.split(","))
    return llx + MARGIN, height - ury + MARGIN, urx - llx, ury - lly


def build_page(layout: dict) -> str:
    height = float(layout["bb"].split(",")[3])
    cells: list[str] = []
    # Clusters first so they sit behind the nodes; Graphviz lists outer clusters before inner ones.
    clusters = [o for o in layout["objects"] if o.get("name", "").startswith("cluster")]
    for o in clusters:
        x, y, w, h = _bb(o["bb"], height)
        fill, stroke, font = CLUSTER_STYLE.get(o["name"], ("#FFFFFF", "#9CA3AF", "#374151"))
        style = (
            f"rounded=1;dashed=1;fillColor={fill};strokeColor={stroke};fontColor={font};"
            "verticalAlign=top;align=center;fontSize=12;fontStyle=1;spacingTop=4;whiteSpace=wrap;html=1;container=0;"
        )
        cells.append(
            f'<mxCell id="{o["name"]}" value="{_label(o.get("label", ""))}" style="{style}" vertex="1" parent="1">'
            f'<mxGeometry x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{h:.0f}" as="geometry"/></mxCell>'
        )
    nodes = [o for o in layout["objects"] if not o.get("name", "").startswith("cluster")]
    for o in nodes:
        cx, cy = (float(v) for v in o["pos"].split(","))
        w = float(o["width"]) * POINTS_PER_INCH
        h = float(o["height"]) * POINTS_PER_INCH
        x, y = cx - w / 2 + MARGIN, height - cy - h / 2 + MARGIN
        shape = o.get("shape", "box")
        fill = o.get("fillcolor", "#F5F7FA").upper()
        if shape == "cylinder":
            base = "shape=cylinder3;boundedLbl=1;backgroundOutline=1;size=8;"
        elif shape == "ellipse":
            base = "ellipse;"
        else:
            base = "rounded=1;"
        style = f"{base}whiteSpace=wrap;html=1;fillColor={fill};strokeColor=#4B5563;fontSize=10;"
        cells.append(
            f'<mxCell id="{o["name"]}" value="{_label(o.get("label", o["name"]))}" style="{style}" vertex="1" parent="1">'
            f'<mxGeometry x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{h:.0f}" as="geometry"/></mxCell>'
        )
    by_gvid = {o["_gvid"]: o["name"] for o in layout["objects"]}
    for i, e in enumerate(layout.get("edges", [])):
        src, dst = by_gvid[e["tail"]], by_gvid[e["head"]]
        label = e.get("label", "")
        crossing = "B" in label and label.startswith("DF-")
        color = CROSSING_COLOR if crossing else "#374151"
        width = "strokeWidth=2;" if crossing else ""
        dash = "dashed=1;" if e.get("style") in {"dashed", "dotted"} else ""
        arrows = "endArrow=none;" if e.get("arrowhead") == "none" else ""
        both = "startArrow=classic;" if e.get("dir") == "both" else ""
        style = (
            f"edgeStyle=orthogonalEdgeStyle;rounded=1;orthogonalLoop=1;jettySize=auto;html=1;"
            f"strokeColor={color};fontColor={color};fontSize=9;{width}{dash}{arrows}{both}"
        )
        cells.append(
            f'<mxCell id="df-e{i}" value="{_label(label)}" style="{style}" edge="1" parent="1" source="{src}" target="{dst}">'
            '<mxGeometry relative="1" as="geometry"/></mxCell>'
        )
    title = layout.get("label", "Data flow diagram")
    cells.insert(
        0,
        f'<mxCell id="df-title" value="{_label(title)}" style="text;html=1;align=left;verticalAlign=middle;fontSize=16;fontStyle=1;fontColor=#232F3E;" vertex="1" parent="1">'
        f'<mxGeometry x="{MARGIN}" y="8" width="1000" height="24" as="geometry"/></mxCell>',
    )
    total_w = float(layout["bb"].split(",")[2]) + 2 * MARGIN
    total_h = height + 2 * MARGIN
    body = "\n        ".join(cells)
    return (
        f'  <diagram id="{PAGE_ID}" name="{PAGE_NAME}">\n'
        f'    <mxGraphModel dx="1420" dy="900" grid="1" gridSize="10" guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" pageWidth="{total_w:.0f}" pageHeight="{total_h:.0f}" math="0" shadow="0">\n'
        "      <root>\n        <mxCell id=\"0\"/>\n        <mxCell id=\"1\" parent=\"0\"/>\n"
        f"        {body}\n"
        "      </root>\n    </mxGraphModel>\n  </diagram>\n"
    )


def write_drawio_page(page_xml: str) -> None:
    text = DRAWIO.read_text(encoding="utf-8")
    pattern = re.compile(rf'  <diagram id="{re.escape(PAGE_ID)}"[^>]*>.*?</diagram>\n', re.S)
    if pattern.search(text):
        text = pattern.sub(lambda _m: page_xml, text)
    else:
        text = text.replace("</mxfile>", page_xml + "</mxfile>")
    DRAWIO.write_text(text, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", maxsplit=1)[0])
    parser.add_argument("--dpi", type=int, default=110)
    args = parser.parse_args()
    render_png(args.dpi)
    write_drawio_page(build_page(_layout()))
    print(f"wrote {PNG.relative_to(ROOT)} and page '{PAGE_NAME}' in {DRAWIO.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
