"""BMC soundness against circuits whose answers are known by construction.

Each circuit has a minimum satisfying depth we can state independently of the
solver, so a solver that is unsound in either direction -- claiming sat too
early, or unsat when a solution exists -- is caught.

Covers exact-cycle vs any-cycle, the reset prefix, held and patterned control
signals, bit grouping with character classes and ranges, enumeration, and the
impossible case.
"""

import contextlib
import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

from netlist_builder import Builder
import solve_bmc

GEN = os.path.join(HERE, "..", "generated")


# ------------------------------------------------------------- constructions

def and_tree(b, nets, out):
    """AND together any number of nets, returning through `out`."""
    cur = list(nets)
    while len(cur) > 1:
        nxt = []
        for i in range(0, len(cur) - 1, 2):
            o = out if (len(cur) == 2) else b.net("t")
            b.add("and2_1", {"A": cur[i], "B": cur[i + 1], "X": o})
            nxt.append(o)
        if len(cur) % 2:
            nxt.append(cur[-1])
        cur = nxt
    if len(cur) == 1 and cur[0] != out:
        b.add("buf_1", {"A": cur[0], "X": out})
    return out


def shift_compare(n, const, enable=False):
    """n-bit shift register on `din`, success when it equals `const`.

    Minimum satisfying depth is exactly n: the register must fill first.
    """
    b = Builder(f"sc{n}")
    for p in ("clk", "rst_n", "din"):
        b.port(p)
    if enable:
        b.port("en")
    b.port("success")
    stages = []
    prev = "din"
    for i in range(n):
        q = b.net("q")
        if enable:
            b.add("edfxtp_1", {"CLK": "clk", "D": prev, "DE": "en", "Q": q})
        else:
            b.add("dfrtp_1", {"CLK": "clk", "D": prev, "RESET_B": "rst_n",
                              "Q": q})
        stages.append(q)
        prev = q
    # stages[0] is the most recent bit; MSB-first means stages[n-1] is bit n-1
    terms = []
    for i, q in enumerate(stages):
        bit = (const >> i) & 1
        if bit:
            terms.append(q)
        else:
            t = b.net("i")
            b.add("inv_1", {"A": q, "Y": t})
            terms.append(t)
    and_tree(b, terms, "success")
    return b.build()


def impossible():
    b = Builder("imp")
    for p in ("clk", "rst_n", "din"):
        b.port(p)
    b.port("success")
    b.add("inv_1", {"A": "din", "Y": "nd"})
    b.add("and2_1", {"A": "din", "B": "nd", "X": "success"})
    return b.build()


def reset_required():
    """success is only high once reset has been applied and released."""
    b = Builder("rst")
    for p in ("clk", "rst_n"):
        b.port(p)
    b.port("success")
    b.add("dfstp_1", {"CLK": "clk", "D": "qs", "SET_B": "rst_n", "Q": "qs"})
    b.add("dfrtp_1", {"CLK": "clk", "D": "qr", "RESET_B": "rst_n", "Q": "qr"})
    b.add("inv_1", {"A": "qr", "Y": "nqr"})
    b.add("and2_1", {"A": "qs", "B": "nqr", "X": "success"})
    return b.build()


def two_inputs():
    """Two independent 2-bit registers that must both match."""
    b = Builder("two")
    for p in ("clk", "rst_n", "a", "bb"):
        b.port(p)
    b.port("success")
    terms = []
    for src, want in (("a", 0b10), ("bb", 0b01)):
        prev = src
        stages = []
        for i in range(2):
            q = b.net("q")
            b.add("dfrtp_1", {"CLK": "clk", "D": prev, "RESET_B": "rst_n",
                              "Q": q})
            stages.append(q)
            prev = q
        for i, q in enumerate(stages):
            if (want >> i) & 1:
                terms.append(q)
            else:
                t = b.net("i")
                b.add("inv_1", {"A": q, "Y": t})
                terms.append(t)
    and_tree(b, terms, "success")
    return b.build()


# ------------------------------------------------------------------ harness

def solve(data, argv_extra, expect_sat=True):
    fd, path = tempfile.mkstemp(suffix=".json", dir=GEN)
    os.close(fd)
    json.dump(data, open(path, "w"))
    out = os.path.join(GEN, "_bmc_res.json")
    argv = [path, "--output", "success", "--json", out] + argv_extra
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = solve_bmc.main(argv)
    res = json.load(open(out)) if os.path.exists(out) and rc == 0 else None
    for p in (path, out):
        if os.path.exists(p):
            os.remove(p)
    return rc, res, buf.getvalue()


def main():
    fails = []

    def check(name, cond, got):
        print(f"  {'ok  ' if cond else 'FAIL'}  {name:52s} {got}")
        if not cond:
            fails.append(name)

    # --- minimum depth is exactly n ---------------------------------------
    for n, const in ((1, 0b1), (2, 0b10), (8, 0b10110011),
                     (17, 0b10110011010110011)):
        data = shift_compare(n, const)
        rc, res, _ = solve(data, ["--search", "1", str(n + 4), "--data", "din"])
        ok = rc == 0 and res and res["depth"] == n
        check(f"minimum depth is exactly {n}", ok,
              f"got {res['depth'] if res else 'unsat'}")
        if ok:
            bits = res["solutions"][0]["decoded"]["din"]["bits"]
            # din is shifted MSB-last: stage i holds the bit from i cycles ago
            want = "".join(str((const >> i) & 1) for i in reversed(range(n)))
            check(f"  depth {n}: recovered input matches the constant",
                  bits == want, f"{bits} vs {want}")
            check(f"  depth {n}: replay confirms",
                  res["solutions"][0]["replay_confirms"] is True, "")

    # --- exact cycle vs any cycle -----------------------------------------
    data = shift_compare(4, 0b1011)
    rc, res, _ = solve(data, ["--cycles", "6", "--data", "din"])
    check("exact-cycle mode satisfies at the final cycle only",
          rc == 0 and res["solutions"][0]["replay_confirms"], f"rc={rc}")
    rc2, res2, _ = solve(data, ["--cycles", "6", "--any-cycle", "--data", "din"])
    check("any-cycle mode also finds a solution", rc2 == 0, f"rc={rc2}")
    if rc == 0 and rc2 == 0:
        check("  any-cycle may satisfy earlier than exact-cycle",
              res2["solutions"][0]["success_cycle"] <=
              res["solutions"][0]["success_cycle"],
              f"any={res2['solutions'][0]['success_cycle']} "
              f"exact={res['solutions'][0]['success_cycle']}")

    # --- depth below the minimum must be unsat ----------------------------
    data = shift_compare(8, 0b10110011)
    rc, _, _ = solve(data, ["--cycles", "7", "--data", "din"])
    check("depth below the minimum is unsat", rc == 1, f"rc={rc}")

    # --- impossible circuit -----------------------------------------------
    rc, _, _ = solve(impossible(), ["--search", "1", "12", "--data", "din"])
    check("impossible circuit is unsat at every depth", rc == 1, f"rc={rc}")

    # --- reset prefix ------------------------------------------------------
    rc, res, _ = solve(reset_required(), ["--search", "1", "4",
                                          "--reset-cycles", "2"])
    check("reset-required circuit solves with a reset prefix",
          rc == 0, f"rc={rc}, depth={res['depth'] if res else None}")

    # --- enable window -----------------------------------------------------
    data = shift_compare(4, 0b1101, enable=True)
    rc, _, _ = solve(data, ["--cycles", "4", "--data", "din", "--hold", "en=0"])
    check("enable held low makes it unsat", rc == 1, f"rc={rc}")
    rc, res, _ = solve(data, ["--cycles", "4", "--data", "din",
                              "--hold", "en=1"])
    check("enable held high makes it sat", rc == 0, f"rc={rc}")
    rc, res, _ = solve(data, ["--cycles", "6", "--data", "din",
                              "--pattern", "en=001111"])
    check("patterned enable delays the solution", rc == 0, f"rc={rc}")

    # --- charset / range constraints ---------------------------------------
    npr = 0b00000001  # 0x01, not printable
    data = shift_compare(8, npr)
    rc, _, _ = solve(data, ["--cycles", "8", "--data", "din",
                            "--group", "din=8", "--charset", "din=printable"])
    check("non-printable answer is unsat under a printable charset",
          rc == 1, f"rc={rc}")
    rc, res, _ = solve(data, ["--cycles", "8", "--data", "din",
                              "--group", "din=8", "--charset", "din=byte"])
    check("same answer is sat under the byte charset", rc == 0, f"rc={rc}")

    printable = ord("K")
    data = shift_compare(8, printable)
    rc, res, _ = solve(data, ["--cycles", "8", "--data", "din",
                              "--group", "din=8", "--charset", "din=printable"])
    ok = rc == 0 and res["solutions"][0]["decoded"]["din"].get("ascii") == "K"
    check("printable answer decodes to the right ASCII character", ok,
          res["solutions"][0]["decoded"]["din"].get("ascii") if res else "unsat")
    rc, _, _ = solve(data, ["--cycles", "8", "--data", "din",
                            "--group", "din=8", "--range", "din=0:74"])
    check("range excluding the answer is unsat", rc == 1, f"rc={rc}")

    # --- enumeration --------------------------------------------------------
    data = shift_compare(3, 0b101)
    rc, res, _ = solve(data, ["--cycles", "3", "--data", "din",
                              "--enumerate", "4"])
    n_sol = len(res["solutions"]) if res else 0
    check("a single-constant match has exactly one solution", n_sol == 1,
          f"{n_sol} solutions")

    # --- two independent symbolic inputs ------------------------------------
    rc, res, _ = solve(two_inputs(), ["--search", "1", "6",
                                      "--data", "a", "--data", "bb"])
    ok = rc == 0 and res["depth"] == 2
    check("two independent inputs solve at the right depth", ok,
          f"depth={res['depth'] if res else 'unsat'}")
    if ok:
        d = res["solutions"][0]["decoded"]
        check("  both inputs recovered", "a" in d and "bb" in d,
              f"{d['a']['bits']} / {d['bb']['bits']}")

    print()
    if fails:
        print(f"FAIL: {fails}")
        return 1
    print("PASS  BMC is sound on every known-answer circuit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
