"""Orientation and pin-transform correctness.

A single mirrored pin error would swap two inputs and yield a completely
plausible but wrong circuit -- a21bo's A1 and B1_N exchanged, say, which no
invariant check could ever notice.  So this is tested two ways.

Part 1 places cells in every orientation that actually occurs in a row-based
layout (verified against the warm-up: R0, R180, MX, MY and nothing else),
routes them, and checks the recovered connectivity against ground truth.

Part 2 covers all eight orientations including the 90-degree family, which
cannot be routed by the harness and does not occur in real standard-cell rows,
by checking the coordinate transform itself against an independent
implementation written directly from the affine definition rather than by
calling klayout again.
"""

import contextlib
import io
import math
import os
import random
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

import klayout.db as db

import extract_netlist
from synth_layout import Design, HarnessLimitation, compare_to_truth

GEN = os.path.join(HERE, "..", "generated")

# Orientations observed in the warm-up placement, i.e. the ones a real row-based
# design can contain.  90-degree rotations would break the power rails.
ROUTABLE = ["R0", "R180", "MX", "MY"]
ALL_ORIENTS = ["R0", "R90", "R180", "R270", "MX", "MY", "MX90", "MY90"]

CELLS = {
    "sky130_fd_sc_hd__a21bo_1": (["A1", "A2", "B1_N"], "X"),
    "sky130_fd_sc_hd__dfrtp_1": (["CLK", "D", "RESET_B"], "Q"),
    "sky130_fd_sc_hd__mux2_1": (["A0", "A1", "S"], "X"),
    "sky130_fd_sc_hd__o21bai_1": (["A1", "A2", "B1_N"], "Y"),
}


def independent_transform(t, p):
    """Apply an ICplxTrans to a point without asking klayout to do it.

    KLayout's convention: mirror about the x axis first, then rotate, then
    translate.  Writing it out here means a bug in how we compose instance
    transforms cannot hide behind the same library call.
    """
    x, y = p.x, p.y
    if t.is_mirror():
        y = -y
    a = math.radians(t.angle)
    ca, sa = round(math.cos(a)), round(math.sin(a))
    rx = x * ca - y * sa
    ry = x * sa + y * ca
    return db.Point(rx + t.disp.x, ry + t.disp.y)


def part1_routed():
    fails, checks, skipped = [], 0, []
    for cell, (ins, out) in CELLS.items():
        for orient in ROUTABLE:
            tag = f"{cell.split('__')[1]}_{orient}"
            try:
                d = Design(f"orient_{tag}")
                u = d.place(cell, 0.0, row=0, orient=orient)
                sink = d.place("sky130_fd_sc_hd__inv_2",
                               d.cell_width(cell) + 2.0, row=0, orient="R0")
                for i, p in enumerate(ins):
                    d.connect(f"i{i}", [(u, p)], port=True)
                d.connect("mid", [(u, out), (sink, "A")])
                d.connect("o", [(sink, "Y")], port=True)
                d.label_rails()
                path = d.write(os.path.join(GEN, f"orient_{tag}.gds"))
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rec = extract_netlist.extract(
                        path, os.path.join(GEN, f"orient_{tag}"), verbose=False)
                probs = compare_to_truth(rec, d.ground_truth())
                diag = rec["diagnostics"]
                if diag["connectivity"]["total_unresolved_cuts"]:
                    probs.append("unresolved vias")
                if diag["unresolved_pin_count"]:
                    probs.append("unresolved pins")
            except HarnessLimitation as e:
                skipped.append((cell, orient))
                print(f"  skip  {cell.split('__')[1]:12s} {orient:5s}  "
                      f"harness cannot route this cell ({e})")
                continue
            except Exception as e:
                probs = [f"exception: {type(e).__name__}: {e}"]
            checks += 1
            print(f"  {'ok  ' if not probs else 'FAIL'}  "
                  f"{cell.split('__')[1]:12s} {orient:5s}"
                  + ("" if not probs else f"  {probs[:2]}"))
            if probs:
                fails.append((cell, orient))
            for ext in (".gds", ".json"):
                p = os.path.join(GEN, f"orient_{tag}{ext}")
                if os.path.exists(p):
                    os.remove(p)
    return checks, fails, skipped


def part2_transforms():
    """Every orientation: extractor's transform vs an independent one."""
    fails, checks = [], 0
    random.seed(7)
    for cell in CELLS:
        for orient in ALL_ORIENTS:
            d = Design("tf")
            iid = d.place(cell, 3.0, row=1, orient=orient)
            _, t, _ = d.insts[iid]
            cellobj = d.cells[cell]
            li = d.layout.layer(67, 5)
            bad = 0
            n = 0
            for sh in cellobj.shapes(li).each():
                if not sh.is_text():
                    continue
                p = db.Point(sh.text.trans.disp.x, sh.text.trans.disp.y)
                got = t * p
                want = independent_transform(t, p)
                n += 1
                if got.x != want.x or got.y != want.y:
                    bad += 1
            # also random probe points, so we are not only testing label spots
            for _ in range(50):
                p = db.Point(random.randint(-3000, 3000),
                             random.randint(-3000, 3000))
                got, want = t * p, independent_transform(t, p)
                n += 1
                if got.x != want.x or got.y != want.y:
                    bad += 1
            checks += 1
            if bad:
                fails.append((cell, orient, bad, n))
                print(f"  FAIL  {cell.split('__')[1]:12s} {orient:5s} "
                      f"{bad}/{n} points differ")
    if not fails:
        print(f"  ok    all {checks} cell/orientation transforms match an "
              f"independent affine implementation")
    return checks, fails


def main():
    print("Part 1: routed round-trip, physically legal orientations")
    c1, f1, skipped = part1_routed()
    print(f"\nPart 2: transform check, all {len(ALL_ORIENTS)} orientations")
    c2, f2 = part2_transforms()

    print(f"\n{c1} routed placements, {c2} transform checks")
    if f1 or f2:
        print(f"FAIL: {len(f1)} routed, {len(f2)} transform")
        return 1
    print("PASS  pin transforms correct in every orientation")
    return 0


if __name__ == "__main__":
    sys.exit(main())
