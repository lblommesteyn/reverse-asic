"""Adversarial tests on the geometric extractor.

Each case builds a layout containing a specific defect and requires one of:

  A. extraction still recovers the intended netlist, or
  B. validate_netlist.py flags it (WARN or FAIL).

A case that is neither -- a corrupted layout that extracts to a clean-looking
but wrong netlist -- is the failure mode this whole toolkit must not have, so it
is reported as a hard failure here.
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
from synth_layout import Design, compare_to_truth

GEN = os.path.join(HERE, "..", "generated")
INV = "sky130_fd_sc_hd__inv_2"
NAND = "sky130_fd_sc_hd__nand2_1"
AND = "sky130_fd_sc_hd__and2_1"


def run(d, tag):
    """Extract + validate a design, returning (recovered, verdict, checks)."""
    path = d.write(os.path.join(GEN, f"adv_{tag}.gds"))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rec = extract_netlist.extract(path, os.path.join(GEN, f"adv_{tag}"),
                                      verbose=False)
        verdict = validate_netlist.validate(os.path.join(GEN,
                                                         f"adv_{tag}.json"))
    with open(os.path.join(GEN, f"adv_{tag}.json")) as f:
        data = json.load(f)
    for ext in (".gds", ".json"):
        p = os.path.join(GEN, f"adv_{tag}{ext}")
        if os.path.exists(p):
            os.remove(p)
    return rec, verdict, data


def base_design(tag):
    """nand -> inv chain with ports, the common starting point."""
    d = Design(tag)
    a = d.place(NAND, 0.0, row=0)
    b = d.place(INV, d.cell_width(NAND) + 1.5, row=0)
    return d, a, b


CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


@case("clean baseline")
def _clean(tag):
    d, a, b = base_design(tag)
    d.connect("i0", [(a, "A")], port=True)
    d.connect("i1", [(a, "B")], port=True)
    d.connect("mid", [(a, "Y"), (b, "A")])
    d.connect("out", [(b, "Y")], port=True)
    d.label_rails()
    return d, "must_match"


@case("missing via on one leaf")
def _missing_via(tag):
    d, a, b = base_design(tag)
    d.connect("i0", [(a, "A")], port=True)
    d.connect("i1", [(a, "B")], port=True)
    d.connect("mid", [(a, "Y"), (b, "A")], skip_via=1)
    d.connect("out", [(b, "Y")], port=True)
    d.label_rails()
    return d, "must_flag"


@case("open metal segment in a trunk")
def _broken_trunk(tag):
    d, a, b = base_design(tag)
    d.connect("i0", [(a, "A")], port=True)
    d.connect("i1", [(a, "B")], port=True)
    d.connect("mid", [(a, "Y"), (b, "A")], break_trunk=True)
    d.connect("out", [(b, "Y")], port=True)
    d.label_rails()
    return d, "must_flag"


@case("accidental short between two driven nets")
def _short(tag):
    d = Design(tag)
    a = d.place(NAND, 0.0, row=0)
    b = d.place(INV, d.cell_width(NAND) + 1.5, row=0)
    c = d.place(AND, d.cell_width(NAND) + d.cell_width(INV) + 3.0, row=0)
    d.connect("i0", [(a, "A")], port=True)
    d.connect("i1", [(a, "B")], port=True)
    d.connect("i2", [(b, "A")], port=True)
    # two driven nets whose trunks overlap in x, so a vertical bridge really
    # touches both
    y1 = d.connect("n1", [(a, "Y"), (c, "A")])
    y2 = d.connect("n2", [(b, "Y"), (c, "B")])
    d.connect("out", [(c, "X")], port=True)
    d.label_rails()
    x = d.short_nets("n1", "n2", y1, y2)
    assert x is not None
    return d, "must_flag"


@case("two outputs wired to the same net")
def _multi_driver(tag):
    d = Design(tag)
    a = d.place(NAND, 0.0, row=0)
    b = d.place(INV, d.cell_width(NAND) + 1.5, row=0)
    c = d.place(AND, d.cell_width(NAND) + d.cell_width(INV) + 3.0, row=0)
    d.connect("i0", [(a, "A")], port=True)
    d.connect("i1", [(a, "B")], port=True)
    d.connect("i2", [(b, "A")], port=True)
    d.connect("i3", [(c, "B")], port=True)
    # nand.Y and inv.Y both drive the same trunk
    d.connect("shorted", [(a, "Y"), (b, "Y"), (c, "A")])
    d.connect("out", [(c, "X")], port=True)
    d.label_rails()
    return d, "must_flag"


@case("floating input pin")
def _floating(tag):
    d, a, b = base_design(tag)
    d.connect("i0", [(a, "A")], port=True)
    # a.B deliberately left unrouted
    d.connect("mid", [(a, "Y"), (b, "A")])
    d.connect("out", [(b, "Y")], port=True)
    d.label_rails()
    return d, "must_flag"


@case("unused cell output")
def _unused_output(tag):
    d, a, b = base_design(tag)
    d.connect("i0", [(a, "A")], port=True)
    d.connect("i1", [(a, "B")], port=True)
    d.connect("mid", [(a, "Y"), (b, "A")])
    # b.Y deliberately unrouted and not a port
    d.label_rails()
    return d, "must_flag"


@case("duplicated port label on two different nets")
def _dup_port(tag):
    d, a, b = base_design(tag)
    d.connect("i0", [(a, "A")], port=True)
    d.connect("dup", [(a, "B")], port=True)
    y = d.connect("mid", [(a, "Y"), (b, "A")])
    d.connect("out", [(b, "Y")], port=True)
    # paste the same port name onto an unrelated net
    d._label(("met3", (70, 5))[1], "dup", 0.6, y)
    d.label_rails()
    return d, "must_flag"


@case("signal pin tied to VPWR rail")
def _tie_power(tag):
    d, a, b = base_design(tag)
    d.connect("i0", [(a, "A")], port=True)
    d.tie_to_rail(a, "B", "VPWR")
    d.connect("mid", [(a, "Y"), (b, "A")])
    d.connect("out", [(b, "Y")], port=True)
    d.label_rails()
    return d, "tie_power"


@case("signal pin tied to VGND rail")
def _tie_gnd(tag):
    d, a, b = base_design(tag)
    d.connect("i0", [(a, "A")], port=True)
    d.tie_to_rail(a, "B", "VGND")
    d.connect("mid", [(a, "Y"), (b, "A")])
    d.connect("out", [(b, "Y")], port=True)
    d.label_rails()
    return d, "tie_gnd"


@case("wires touching only at a corner")
def _corner(tag):
    d, a, b = base_design(tag)
    d.connect("i0", [(a, "A")], port=True)
    d.connect("i1", [(a, "B")], port=True)
    y1 = d.connect("mid", [(a, "Y"), (b, "A")])
    y2 = d.connect("out", [(b, "Y")], port=True)
    # two met3 boxes meeting at exactly one point
    lay = d._layer((70, 20))
    import klayout.db as kdb
    d.top.shapes(lay).insert(kdb.Box(20000, 20000, 21000, 21000))
    d.top.shapes(lay).insert(kdb.Box(21000, 21000, 22000, 22000))
    d.label_rails()
    return d, "corner"


@case("many vias packed close together")
def _dense_vias(tag):
    """Four independent nets escaping within a 1um window.

    Via centres land close enough that a sloppy point lookup, or any tolerance
    based on proximity rather than containment, would merge them.
    """
    d = Design(tag)
    a = d.place("sky130_fd_sc_hd__and4_1", 0.0, row=0)
    b = d.place(INV, d.cell_width("sky130_fd_sc_hd__and4_1") + 1.5, row=0)
    for i, pin in enumerate(["A", "B", "C", "D"]):
        d.connect(f"i{i}", [(a, pin)], port=True)
    d.connect("mid", [(a, "X"), (b, "A")])
    d.connect("out", [(b, "Y")], port=True)
    d.label_rails()
    return d, "must_match"


def main():
    fails = []
    print(f"{'case':46s} {'verdict':8s} matches  outcome")
    print("-" * 78)
    for name, fn in CASES:
        tag = name.replace(" ", "_")[:28]
        d, expect = fn(tag)
        truth = d.ground_truth()
        rec, verdict, data = run(d, tag)
        probs = compare_to_truth(rec, truth)
        matched = not probs
        diag = data["diagnostics"]

        ok, note = True, ""
        if expect == "must_match":
            ok = matched and verdict == "PASS"
            note = "clean extraction" if ok else f"{probs[:1]} verdict={verdict}"
        elif expect == "must_flag":
            # either we still got it exactly right, or validation caught it
            ok = matched or verdict in ("WARN", "FAIL")
            note = ("still exact" if matched
                    else f"caught as {verdict}" if ok
                    else "SILENTLY WRONG")
        elif expect in ("tie_power", "tie_gnd"):
            rail = "VPWR" if expect == "tie_power" else "VGND"
            pins = rec["instances"]
            tied = any(v == rail for r in pins for v in r["pins"].values())
            const_not_input = rail in data.get("constants", {})
            ok = tied and const_not_input
            note = (f"pin resolved to {rail} constant" if ok
                    else f"tie NOT recognised (tied={tied})")
        elif expect == "corner":
            # whichever way it goes, it must not silently merge real nets
            ok = matched or verdict in ("WARN", "FAIL")
            note = "no false merge" if matched else f"caught as {verdict}"

        print(f"{name:46s} {verdict:8s} {str(matched):7s}  {note}")
        if not ok:
            fails.append((name, note))

    print()
    if fails:
        for n, note in fails:
            print(f"FAIL {n}: {note}")
        return 1
    print(f"PASS  all {len(CASES)} adversarial cases are either extracted "
          f"correctly or caught by validation")
    return 0


if __name__ == "__main__":
    sys.exit(main())
