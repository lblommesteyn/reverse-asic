"""Extraction guarantees: determinism, clean diagnostics, gold equivalence.

These are the invariants everything else rests on.  If extraction regresses,
every downstream result is quietly wrong, so this test asserts the hard numbers
rather than just "it ran".
"""

import io
import json
import os
import sys
import contextlib

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

import compare_netlists
import extract_netlist
import validate_netlist

GDS = os.path.join(HERE, "..", "warmup", "04_final.gds")
GOLD = os.path.join(HERE, "..", "warmup", "01_netlist.v")
GEN = os.path.join(HERE, "..", "generated")


def main():
    fails = []

    def check(name, cond, got):
        print(f"  {'ok  ' if cond else 'FAIL'}  {name}: {got}")
        if not cond:
            fails.append(name)

    os.makedirs(GEN, exist_ok=True)
    quiet = io.StringIO()
    with contextlib.redirect_stdout(quiet):
        a = extract_netlist.extract(GDS, os.path.join(GEN, "warmup"),
                                    verbose=False)
        b = extract_netlist.extract(GDS, os.path.join(GEN, "warmup_repeat"),
                                    verbose=False)

    ha = a["hashes"]["structural_sha256"]
    hb = b["hashes"]["structural_sha256"]
    check("extraction is deterministic", ha == hb, f"{ha[:16]} vs {hb[:16]}")
    check("full hash is deterministic",
          a["hashes"]["full_sha256"] == b["hashes"]["full_sha256"],
          a["hashes"]["full_sha256"][:16])

    d = a["diagnostics"]
    check("zero unresolved vias",
          d["connectivity"]["total_unresolved_cuts"] == 0,
          d["connectivity"]["total_unresolved_cuts"])
    check("zero unresolved pin lookups", d["unresolved_pin_count"] == 0,
          d["unresolved_pin_count"])
    check("zero unsupported cells", not d["unsupported_cells"],
          list(d["unsupported_cells"]))
    check("zero missing pins", d["pins_missing_count"] == 0,
          d["pins_missing_count"])
    check("no port problems", not d["port_problems"], d["port_problems"])
    check("79 functional cells", len(a["instances"]) == 79,
          len(a["instances"]))

    cuts = d["connectivity"]["cuts"]
    total = sum(v["joined"] for v in cuts.values())
    check("all cuts joined", total > 3000, total)

    with contextlib.redirect_stdout(quiet):
        verdict = validate_netlist.validate(os.path.join(GEN, "warmup.json"))
    check("validator reports PASS", verdict == "PASS", verdict)

    with contextlib.redirect_stdout(quiet):
        iso = compare_netlists.main(os.path.join(GEN, "warmup.json"), GOLD)
    check("structurally isomorphic to gold netlist", iso is True, iso)

    if os.path.exists(os.path.join(GEN, "warmup_repeat.json")):
        os.remove(os.path.join(GEN, "warmup_repeat.json"))

    if fails:
        print(f"\nFAIL: {fails}")
        return 1
    print("\nPASS  extraction guarantees hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
