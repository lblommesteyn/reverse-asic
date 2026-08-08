"""Extraction must descend through intermediate hierarchy.

A design may wrap groups of standard cells in intermediate cells.  Walking only
the top level would treat each container as an opaque leaf with no pins, losing
every gate inside it -- the netlist would be missing most of the design while
still looking structurally valid.

The test builds one circuit twice, flat and wrapped, and requires byte-identical
extraction, including through a shifted container so the transform composition
is exercised too.
"""

import contextlib
import io
import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

import extract_netlist
from synth_layout import Design, compare_to_truth

GEN = os.path.join(HERE, "..", "generated")
NAND = "sky130_fd_sc_hd__nand2_1"
INV = "sky130_fd_sc_hd__inv_2"
AND = "sky130_fd_sc_hd__and2_1"


def build(tag, group=None, depth=1, dx=0.0, dy=0.0):
    d = Design(tag)
    a = d.place(NAND, 0.0, row=0)
    b = d.place(INV, d.cell_width(NAND) + 1.5, row=0)
    c = d.place(AND, d.cell_width(NAND) + d.cell_width(INV) + 3.0, row=0)
    d.connect("i0", [(a, "A")], port=True)
    d.connect("i1", [(a, "B")], port=True)
    d.connect("m1", [(a, "Y"), (b, "A")])
    d.connect("m2", [(b, "Y"), (c, "A")])
    d.connect("i2", [(c, "B")], port=True)
    d.connect("out", [(c, "X")], port=True)
    d.label_rails()
    if group:
        d.group_cells(group, dx, dy)
        for k in range(depth - 1):
            d.group_cells(f"{group}_L{k}", 0.0, 0.0)
    path = d.write(os.path.join(GEN, f"hier_{tag}.gds"))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rec = extract_netlist.extract(path, os.path.join(GEN, f"hier_{tag}"),
                                      verbose=False)
    for ext in (".gds", ".json"):
        p = os.path.join(GEN, f"hier_{tag}{ext}")
        if os.path.exists(p):
            os.remove(p)
    return d, rec


def main():
    fails = []

    def check(name, cond, got):
        print(f"  {'ok  ' if cond else 'FAIL'}  {name:50s} {got}")
        if not cond:
            fails.append(name)

    d0, flat = build("flat")
    check("flat baseline extracts", len(flat["instances"]) == 3,
          f"{len(flat['instances'])} cells")
    check("flat matches ground truth",
          not compare_to_truth(flat, d0.ground_truth()), "")

    d1, wrapped = build("wrapped", group="INTERNAL_X")
    check("wrapped extracts the same cell count",
          len(wrapped["instances"]) == len(flat["instances"]),
          f"{len(wrapped['instances'])} vs {len(flat['instances'])}")
    check("wrapped matches ground truth",
          not compare_to_truth(wrapped, d1.ground_truth()), "")
    check("wrapped hash equals flat hash",
          wrapped["hashes"]["structural_sha256"]
          == flat["hashes"]["structural_sha256"],
          wrapped["hashes"]["structural_sha256"][:16])
    check("no unsupported cells reported",
          not wrapped["diagnostics"]["unsupported_cells"],
          list(wrapped["diagnostics"]["unsupported_cells"]))
    check("container recorded in diagnostics",
          "INTERNAL_X" in wrapped["diagnostics"].get("hierarchy_containers", {}),
          wrapped["diagnostics"].get("hierarchy_containers"))

    d2, nested = build("nested", group="INTERNAL_Y", depth=3)
    check("3-deep nesting extracts identically",
          nested["hashes"]["structural_sha256"]
          == flat["hashes"]["structural_sha256"],
          f"depth {nested['diagnostics'].get('hierarchy_max_depth')}")

    d3, shifted = build("shifted", group="INTERNAL_Z", dx=12.5, dy=0.0)
    check("shifted container extracts identically (transform composed)",
          shifted["hashes"]["structural_sha256"]
          == flat["hashes"]["structural_sha256"],
          shifted["hashes"]["structural_sha256"][:16])

    print()
    if fails:
        print(f"FAIL: {fails}")
        return 1
    print("PASS  extraction descends through hierarchy correctly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
