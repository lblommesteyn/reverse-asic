"""The deep diagnostics must correctly classify what they find."""

import contextlib
import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

import diagnose
from netlist_builder import Builder

GEN = os.path.join(HERE, "..", "generated")


def run(data, target):
    fd, path = tempfile.mkstemp(suffix=".json", dir=GEN)
    os.close(fd)
    json.dump(data, open(path, "w"))
    out = os.path.join(GEN, "_diag.json")
    saved = sys.argv
    sys.argv = ["diagnose", path, "--output", target, "--json", out]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            diagnose.main()
    finally:
        sys.argv = saved
    rep = json.load(open(out))
    for p in (path, out):
        if os.path.exists(p):
            os.remove(p)
    return rep, buf.getvalue()


def main():
    fails = []

    def check(name, cond, got):
        print(f"  {'ok  ' if cond else 'FAIL'}  {name:52s} {got}")
        if not cond:
            fails.append(name)

    # --- undriven internal net, buffered clock, dangling output ----------
    b = Builder("diag")
    for p in ("clk", "rst_n", "din"):
        b.port(p)
    b.port("success")
    # two-deep clock buffer tree
    b.add("clkbuf_4", {"A": "clk", "X": "c1"})
    b.add("clkbuf_4", {"A": "c1", "X": "c2"})
    b.add("dfrtp_1", {"CLK": "c2", "D": "din", "RESET_B": "rst_n", "Q": "q0"})
    b.add("dfrtp_1", {"CLK": "c1", "D": "q0", "RESET_B": "rst_n", "Q": "q1"})
    # ORPHAN drives two inputs but nothing drives it
    b.add("and2_1", {"A": "q1", "B": "ORPHAN", "X": "success"})
    b.add("or2_1", {"A": "q0", "B": "ORPHAN", "X": "spare"})
    # dangling: driven, never read, not a port
    b.add("inv_1", {"A": "q0", "Y": "deadnet"})
    rep, out = run(b.build(), "success")

    und = {u["net"]: u for u in rep["undriven"]}
    check("undriven net found", "ORPHAN" in und, sorted(und))
    if "ORPHAN" in und:
        u = und["ORPHAN"]
        check("  both sinks reported", len(u["sinks"]) == 2, len(u["sinks"]))
        check("  sink pins named", {s["pin"] for s in u["sinks"]} == {"B"},
              {s["pin"] for s in u["sinks"]})
        check("  coordinates present",
              all("x" in s and "y" in s for s in u["sinks"]), "")
        check("  cone membership computed",
              u["in_success_cone"] is True, u["in_success_cone"])
        check("  power connection reported",
              u["connected_to_power"] is False, u["connected_to_power"])

    dang = {d["net"]: d for d in rep["dangling"]}
    check("dangling output found", "deadnet" in dang, sorted(dang))
    if "deadnet" in dang:
        check("  categorised, not just listed",
              bool(dang["deadnet"]["category"]), dang["deadnet"]["category"])

    roots = rep["clock_roots"]
    check("clock tree resolves to one source", len(roots) == 1, roots)
    check("clock source is the top-level port",
          any(k.startswith("clk (port)") for k in roots), list(roots))
    check("no inverted clock phases", rep["clock_inverted_flops"] == 0,
          rep["clock_inverted_flops"])

    # --- inverted clock phase is reported --------------------------------
    b2 = Builder("diag2")
    for p in ("clk", "rst_n", "din"):
        b2.port(p)
    b2.port("success")
    b2.add("clkinv_1", {"A": "clk", "Y": "ci"})
    b2.add("dfrtp_1", {"CLK": "ci", "D": "din", "RESET_B": "rst_n",
                       "Q": "success"})
    rep2, _ = run(b2.build(), "success")
    check("inverted clock phase detected",
          rep2["clock_inverted_flops"] == 1, rep2["clock_inverted_flops"])

    print()
    if fails:
        print(f"FAIL: {fails}")
        return 1
    print("PASS  diagnostics classify undriven, dangling and clock correctly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
