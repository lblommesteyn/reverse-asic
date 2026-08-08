"""One command: GDS -> netlist -> validation -> structural report -> artifacts.

Stops at the first FAIL from the validator unless --force is given, because
every downstream result is meaningless on a netlist that failed its invariants.

Usage:
  python scripts/run_pipeline.py <in.gds> <name> [--output success] [--fast]

Writes into generated/:
  <name>.json                recovered netlist
  <name>_validate.json       PASS/WARN/FAIL invariant report
  <name>_report.md / .json   derived structural report  <- read this first
  <name>_reduced.json        cone-restricted, simplified netlist for the solver
  <name>_*.svg               floorplan, cone, blocks, register graph
  <name>_behavioural.v       self-contained Verilog
  <name>_bmc.smt2            SMT2 for an independent solver
"""

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GEN = os.path.normpath(os.path.join(HERE, "..", "generated"))


def banner(n, title):
    print("\n" + "=" * 72)
    print(f"STEP {n}  {title}")
    print("=" * 72)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("gds")
    ap.add_argument("name")
    ap.add_argument("--output", default=None,
                    help="target output port (default: auto-detect 'success')")
    ap.add_argument("--fast", action="store_true",
                    help="skip symbolic comparator detection (large designs)")
    ap.add_argument("--force", action="store_true",
                    help="continue even if validation FAILs")
    ap.add_argument("--skip-export", action="store_true")
    a = ap.parse_args()

    import check_cells
    import extract_netlist
    import validate_netlist
    import puzzle_report
    import visualize
    import reduce as reduce_mod
    from sim import Circuit

    os.makedirs(GEN, exist_ok=True)
    prefix = os.path.join(GEN, a.name)
    t0 = time.time()

    banner(1, "inspect: cell library vs GDS pin labels")
    clean_cells = check_cells.main(a.gds)
    if not clean_cells:
        print("\n!! Cells above are unmodelled or mismatched. Add them to "
              "cells.py\n   before trusting any simulation or SAT result.")

    banner(2, "extract netlist from layout geometry")
    data = extract_netlist.extract(a.gds, prefix)

    banner(3, "validate invariants")
    verdict = validate_netlist.validate(prefix + ".json",
                                        prefix + "_validate.json")
    if verdict == "FAIL" and not a.force:
        print("\nSTOPPING: validation failed. Fix extraction before "
              "continuing, or re-run with --force to proceed anyway.")
        return 1

    banner(4, "deep diagnostics for validator warnings")
    if verdict != "FAIL":
        import diagnose
        saved = sys.argv
        sys.argv = ["diagnose", prefix + ".json", "--output",
                    a.output or "success", "--json", prefix + "_diagnose.json"]
        try:
            diagnose.main()
        except Exception as e:
            print(f"diagnostics failed ({e}); continuing")
        finally:
            sys.argv = saved
    else:
        print("skipped: validation failed")

    banner(5, "structural report")
    target = a.output
    if target is None:
        for cand in ("success", "S", "out", "valid", "done"):
            if cand in data["ports"]:
                target = cand
                break
    print(f"target output: {target}")
    rep = puzzle_report.gather(data, target, deep=not a.fast)
    open(prefix + "_report.md", "w").write(puzzle_report.to_markdown(rep))
    json.dump(rep, open(prefix + "_report.json", "w"), indent=1)
    print(f"wrote {prefix}_report.md and .json")

    banner(6, "reduce to the target cone")
    try:
        red, stats = reduce_mod.reduce_netlist(data, target)
        json.dump(red, open(prefix + "_reduced.json", "w"), indent=1,
                  sort_keys=True)
        print(f"wrote {prefix}_reduced.json")
    except Exception as e:
        print(f"reduction failed ({e}); the full netlist is still usable")
        stats = None

    banner(7, "visualizations")
    argv = ["visualize", prefix + ".json", GEN,
            "--report", prefix + "_report.json"]
    if target:
        argv += ["--output", target]
    saved, sys.argv = sys.argv, argv
    try:
        visualize.main()
    finally:
        sys.argv = saved

    if not a.skip_export:
        banner(8, "export for independent solvers")
        import emit_behavioural
        import emit_verilog
        emit_behavioural.emit(data, prefix + "_behavioural.v", data.get("top"))
        emit_verilog.emit(data, prefix + "_structural.v", data.get("top"))

    # ---------------------------------------------------------------- summary
    print("\n" + "=" * 72)
    print("SUMMARY")
    print("=" * 72)
    c = Circuit(data)
    print(f"  validation      : {verdict}")
    print(f"  cells / flops   : {len(c.insts)} / {len(c.flops)}")
    print(f"  primary inputs  : {c.inputs}")
    print(f"  driven outputs  : {c.outputs}")
    sc = rep.get("sequential_cone")
    if sc:
        print(f"  cone state bits : {sc['transitive_state_bits']} of "
              f"{sc['total_state_bits']}  "
              f"({sc['state_bits_outside_cone']} outside)")
        print(f"  cells outside   : {rep['cells_outside_cone']} "
              f"(output-generator candidate)")
        b = rep.get("outside_cone_bbox")
        if b:
            print(f"  outside bbox    : x {b['x0']:.1f}..{b['x1']:.1f}  "
                  f"y {b['y0']:.1f}..{b['y1']:.1f}")
        print(f"  suggested BMC   : >= {sc['suggested_min_bmc_cycles']} cycles")
    if stats:
        print(f"  reduced to      : {stats['reduced_cells']} cells "
              f"({stats['cell_reduction_pct']}% smaller)")
    print(f"\n  READ FIRST      : {prefix}_report.md")
    print(f"  THEN            : {prefix}_blocks.svg, {prefix}_cone.svg")
    print(f"\n  elapsed {time.time() - t0:.1f}s")
    return 0 if verdict != "FAIL" else 1


if __name__ == "__main__":
    sys.exit(main())
