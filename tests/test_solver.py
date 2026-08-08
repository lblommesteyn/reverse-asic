"""Regression tests for the incremental BMC engine.

Checks that the solver finds the minimum depth, that every reported solution is
genuinely valid (sums to 496 in the warm-up), that enumeration returns distinct
solutions, that grouping constraints are actually enforced, and that the
concrete replay agrees with the symbolic model on every one.
"""

import io
import json
import os
import sys
import contextlib

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

import solve_bmc
from sim import Circuit

NETLIST = os.path.join(HERE, "..", "generated", "warmup.json")
OUT = os.path.join(HERE, "..", "generated", "solver_test.json")


def run(extra):
    argv = [NETLIST, "--output", "S", "--hold", "en=1",
            "--data", "A", "--data", "B", "--json", OUT] + extra
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = solve_bmc.main(argv)
    return rc, json.load(open(OUT)) if os.path.exists(OUT) else None, buf.getvalue()


def bits_to_int(bits):
    return int(bits, 2)


def main():
    fails = []

    def check(name, cond, got):
        print(f"  {'ok  ' if cond else 'FAIL'}  {name}: {got}")
        if not cond:
            fails.append(name)

    # --- minimum depth search -------------------------------------------
    rc, res, _ = run(["--search", "1", "16"])
    check("finds a solution", rc == 0 and res is not None, f"rc={rc}")
    check("minimum depth is 8", res["depth"] == 8, res["depth"])
    sol = res["solutions"][0]
    check("replay confirms", sol["replay_confirms"] is True,
          sol["replay_confirms"])
    a = bits_to_int(sol["decoded"]["A"]["bits"])
    b = bits_to_int(sol["decoded"]["B"]["bits"])
    check("A + B == 496", a + b == 496, f"{a} + {b} = {a + b}")

    # --- enumeration ------------------------------------------------------
    rc, res, _ = run(["--cycles", "8", "--enumerate", "6"])
    sols = res["solutions"]
    check("six solutions enumerated", len(sols) == 6, len(sols))
    pairs = []
    for s in sols:
        aa = bits_to_int(s["decoded"]["A"]["bits"])
        bb = bits_to_int(s["decoded"]["B"]["bits"])
        pairs.append((aa, bb))
        if not s["replay_confirms"]:
            fails.append("enumerated solution replay")
    check("all enumerated sums are 496",
          all(x + y == 496 for x, y in pairs), pairs)
    check("solutions are distinct", len(set(pairs)) == len(pairs), pairs)

    # --- grouped range constraint ----------------------------------------
    rc, res, _ = run(["--cycles", "8", "--range", "A=241:243"])
    sol = res["solutions"][0]
    a = bits_to_int(sol["decoded"]["A"]["bits"])
    b = bits_to_int(sol["decoded"]["B"]["bits"])
    check("range constraint honoured", 241 <= a <= 243, a)
    check("still sums to 496", a + b == 496, f"{a}+{b}")

    # --- unsatisfiable constraint is reported as such --------------------
    rc, _, out = run(["--cycles", "8", "--charset", "A=digits",
                      "--charset", "B=digits"])
    check("impossible charset is unsat", rc == 1, f"rc={rc}")

    # --- any-cycle mode ---------------------------------------------------
    rc, res, _ = run(["--cycles", "12", "--any-cycle"])
    check("any-cycle finds a solution", rc == 0, f"rc={rc}")
    if rc == 0:
        s = res["solutions"][0]
        check("any-cycle replay confirms", s["replay_confirms"] is True,
              f"success at cycle {s['success_cycle']}")

    if fails:
        print(f"\nFAIL: {fails}")
        return 1
    print("\nPASS  solver behaves correctly on every check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
