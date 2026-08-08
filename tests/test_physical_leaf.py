"""Classification of unknown leaf cells.

A design can contain leaf cells that are not standard cells.  Some are pure
annotation and must be ignored; some carry signal and must never be ignored.
The rule is about geometry, not names or pin counts:

  ignorable  -> no logical model, no pin labels, no sub-instances, and NO
                geometry on any conductor, cut or device layer
  hard FAIL  -> anything else

Getting this wrong in the permissive direction silently deletes real logic from
the netlist, so each electrical case below must remain a FAIL.
"""

import contextlib
import io
import json
import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

import extract_netlist
import validate_netlist
from synth_layout import Design

GEN = os.path.join(HERE, "..", "generated")
NAND = "sky130_fd_sc_hd__nand2_1"
INV = "sky130_fd_sc_hd__inv_2"

# (label, layers for the custom leaf, must_be_ignored)
CASES = [
    ("annotation layer only (200/0)", [(200, 0)], True),
    ("two annotation layers (200/0 + 81/23)", [(200, 0), (81, 23)], True),
    ("met1 geometry", [(68, 20)], False),
    ("mcon cut geometry", [(67, 44)], False),
    ("via2 cut geometry", [(69, 44)], False),
    ("li1 geometry", [(67, 20)], False),
    ("poly device layer", [(66, 20)], False),
    ("diff device layer", [(65, 20)], False),
    ("annotation plus one met2 shape", [(200, 0), (69, 20)], False),
]


def build(tag, layers):
    d = Design(tag)
    a = d.place(NAND, 0.0, row=0)
    b = d.place(INV, d.cell_width(NAND) + 1.5, row=0)
    d.connect("i0", [(a, "A")], port=True)
    d.connect("i1", [(a, "B")], port=True)
    d.connect("mid", [(a, "Y"), (b, "A")])
    d.connect("out", [(b, "Y")], port=True)
    d.label_rails()
    d.add_custom_leaf("MYSTERY_CELL", layers, x=30.0, row=0)
    path = d.write(os.path.join(GEN, f"phys_{tag}.gds"))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rec = extract_netlist.extract(path, os.path.join(GEN, f"phys_{tag}"),
                                      verbose=False)
        verdict = validate_netlist.validate(os.path.join(GEN,
                                                         f"phys_{tag}.json"))
    data = json.load(open(os.path.join(GEN, f"phys_{tag}.json")))
    for ext in (".gds", ".json"):
        p = os.path.join(GEN, f"phys_{tag}{ext}")
        if os.path.exists(p):
            os.remove(p)
    return rec, verdict, data


def main():
    fails = []
    print(f"{'custom leaf geometry':40s} {'ignored':8s} {'verdict':8s} outcome")
    print("-" * 76)
    for label, layers, must_ignore in CASES:
        tag = label.replace(" ", "_").replace("/", "").replace("(", "") \
                   .replace(")", "").replace("+", "")[:30]
        rec, verdict, data = build(tag, layers)
        diag = data["diagnostics"]
        phys = diag.get("physical_only_cells", {})
        unsup = diag.get("unsupported_cells", {})
        ignored = "MYSTERY_CELL" in phys
        flagged = "MYSTERY_CELL" in unsup
        cells = len(rec["instances"])

        if must_ignore:
            ok = ignored and not flagged and verdict != "FAIL" and cells == 2
            note = ("ignored, 2 real cells kept" if ok
                    else f"ignored={ignored} flagged={flagged} cells={cells}")
        else:
            ok = flagged and not ignored and verdict == "FAIL"
            note = ("correctly refused" if ok
                    else "SILENTLY DROPPED ELECTRICAL GEOMETRY")
        print(f"{label:40s} {str(ignored):8s} {verdict:8s} {note}")
        if not ok:
            fails.append(label)

    print()
    if fails:
        print(f"FAIL: {fails}")
        return 1
    print(f"PASS  all {len(CASES)} leaf-classification cases behave correctly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
