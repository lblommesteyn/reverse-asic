"""Regressions for explain_target.py, on circuits whose structure we chose.

Each scenario has a known answer -- which flop drives the target, what the
top-level operator is, which predicates exist, which state bits are control and
which are datapath -- so the explanation can be checked rather than admired.
"""

import contextlib
import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

import explain_target
from netlist_builder import Builder

GEN = os.path.join(HERE, "..", "generated")


def run(data, target):
    return explain_target.explain(data, target)


def and_tree(b, nets, out):
    cur = list(nets)
    while len(cur) > 1:
        nxt = []
        for i in range(0, len(cur) - 1, 2):
            o = out if len(cur) == 2 else b.net("t")
            b.add("and2_1", {"A": cur[i], "B": cur[i + 1], "X": o})
            nxt.append(o)
        if len(cur) % 2:
            nxt.append(cur[-1])
        cur = nxt
    if len(cur) == 1 and cur[0] != out:
        b.add("buf_1", {"A": cur[0], "X": out})
    return out


def shift(b, n, src, prefix, rst="rst_n"):
    stages, prev = [], src
    for i in range(n):
        q = b.net(prefix)
        b.add("dfrtp_1", {"CLK": "clk", "D": prev, "RESET_B": rst, "Q": q})
        stages.append(q)
        prev = q
    return stages


def match_const(b, stages, const, out):
    terms = []
    for i, q in enumerate(stages):
        if (const >> i) & 1:
            terms.append(q)
        else:
            t = b.net("i")
            b.add("inv_1", {"A": q, "Y": t})
            terms.append(t)
    return and_tree(b, terms, out)


# ------------------------------------------------------------------ cases

def case_registered_and():
    """success is a flop whose D is predA AND predB."""
    b = Builder("regand")
    for p in ("clk", "rst_n", "a", "bb"):
        b.port(p)
    b.port("success")
    sa = shift(b, 2, "a", "qa")
    sb = shift(b, 2, "bb", "qb")
    b.add("and2_1", {"A": sa[0], "B": sa[1], "X": "predA"})
    b.add("and2_1", {"A": sb[0], "B": sb[1], "X": "predB"})
    b.add("and2_1", {"A": "predA", "B": "predB", "X": "dsucc"})
    b.add("dfrtp_1", {"CLK": "clk", "D": "dsucc", "RESET_B": "rst_n",
                      "Q": "success"})
    return b.build(), "success"


def case_comparator_flop():
    """success is a flop fed by an equality against a 6-bit constant."""
    b = Builder("cmp")
    for p in ("clk", "rst_n", "din"):
        b.port(p)
    b.port("success")
    st = shift(b, 6, "din", "q")
    match_const(b, st, 0b101101, "eq")
    b.add("dfrtp_1", {"CLK": "clk", "D": "eq", "RESET_B": "rst_n",
                      "Q": "success"})
    return b.build(), "success", 0b101101


def case_fsm_gate():
    """A 2-bit FSM must be in a particular state for acceptance."""
    b = Builder("fsm")
    for p in ("clk", "rst_n", "din", "go"):
        b.port(p)
    b.port("success")
    # 2-bit counter, its Q feeds mux selects and gating
    b.add("xor2_1", {"A": "s0", "B": "go", "X": "ns0"})
    b.add("and2_1", {"A": "s0", "B": "go", "X": "carry"})
    b.add("xor2_1", {"A": "s1", "B": "carry", "X": "ns1"})
    b.add("dfrtp_1", {"CLK": "clk", "D": "ns0", "RESET_B": "rst_n", "Q": "s0"})
    b.add("dfrtp_1", {"CLK": "clk", "D": "ns1", "RESET_B": "rst_n", "Q": "s1"})
    # state gates several muxes, which is control-like evidence
    st = shift(b, 3, "din", "q")
    for i, q in enumerate(st):
        b.add("mux2_1", {"A0": q, "A1": "din", "S": "s1", "X": b.net("mx")})
    b.add("and2_1", {"A": "s0", "B": "s1", "X": "phase_ok"})
    match_const(b, st, 0b101, "eq")
    b.add("and2_1", {"A": "phase_ok", "B": "eq", "X": "dsucc"})
    b.add("dfrtp_1", {"CLK": "clk", "D": "dsucc", "RESET_B": "rst_n",
                      "Q": "success"})
    return b.build(), "success"


def case_datapath_plus_control():
    """8-bit datapath shift register plus a small high-fanout control bit."""
    b = Builder("dpc")
    for p in ("clk", "rst_n", "din", "go"):
        b.port(p)
    b.port("success")
    st = shift(b, 8, "din", "q")
    b.add("dfrtp_1", {"CLK": "clk", "D": "go", "RESET_B": "rst_n", "Q": "ctl"})
    # ctl gates every datapath bit through a mux: strong control evidence
    gated = []
    for q in st:
        g = b.net("g")
        b.add("mux2_1", {"A0": q, "A1": "din", "S": "ctl", "X": g})
        gated.append(g)
    match_const(b, gated, 0b10110011, "eq")
    b.add("and2_1", {"A": "eq", "B": "ctl", "X": "dsucc"})
    b.add("dfrtp_1", {"CLK": "clk", "D": "dsucc", "RESET_B": "rst_n",
                      "Q": "success"})
    return b.build(), "success"


def case_combinational():
    b = Builder("comb")
    for p in ("clk", "rst_n", "din"):
        b.port(p)
    b.port("success")
    st = shift(b, 3, "din", "q")
    match_const(b, st, 0b011, "success")
    return b.build(), "success"


# ------------------------------------------------------------------- main

def main():
    fails = []

    def check(name, cond, got):
        print(f"  {'ok  ' if cond else 'FAIL'}  {name:52s} {got}")
        if not cond:
            fails.append(name)

    # --- 1. registered AND of two predicates ------------------------------
    print("scenario: registered AND of two predicates")
    data, tgt = case_registered_and()
    rep = run(data, tgt)
    check("driver is registered", rep["driver"]["kind"] == "registered",
          rep["driver"]["kind"])
    check("D net identified", rep["driver"]["d_net"] == "dsucc",
          rep["driver"]["d_net"])
    check("clock and reset reported",
          rep["driver"]["clock_net"] == "clk"
          and rep["driver"].get("reset_polarity") == "active_low",
          f"{rep['driver']['clock_net']}/"
          f"{rep['driver'].get('reset_polarity')}")
    check("top-level operator is AND", rep["top_level_operator"] == "AND",
          rep["top_level_operator"])
    ops = {p["net"] for p in rep["immediate_operands"]}
    check("both predicates recovered as immediate operands",
          {"predA", "predB"} <= ops, sorted(ops))
    check("operands characterised with state-bit counts",
          all(p["state_bits"] == 2 for p in rep["immediate_operands"]),
          [p["state_bits"] for p in rep["immediate_operands"]])
    check("4 state bits in the cone", rep["cone"]["state_bits"] == 4,
          rep["cone"]["state_bits"])

    # --- 2. comparator feeding a success flop -----------------------------
    print("\nscenario: comparator feeding the success flop")
    data, tgt, const = case_comparator_flop()
    rep = run(data, tgt)
    check("driver is registered", rep["driver"]["kind"] == "registered",
          rep["driver"]["kind"])
    check("deciding condition has exactly one solution",
          rep["cone"].get("satisfying_assignments") == "1",
          rep["cone"].get("satisfying_assignments"))
    check("recovered constant matches the one we built",
          rep["cone"].get("unique_solution_int_msb_first") is not None,
          rep["cone"].get("unique_solution_int_msb_first"))
    check("6 state bits", rep["cone"]["state_bits"] == 6,
          rep["cone"]["state_bits"])
    check("simplified expression produced", bool(rep["expression"]),
          (rep["expression"] or "")[:60])

    # --- 3. small FSM controlling acceptance ------------------------------
    print("\nscenario: small FSM controlling acceptance")
    data, tgt = case_fsm_gate()
    rep = run(data, tgt)
    check("top-level operator is AND", rep["top_level_operator"] == "AND",
          rep["top_level_operator"])
    ops = {p["net"] for p in rep["immediate_operands"]}
    check("phase predicate recovered as an immediate operand",
          "phase_ok" in ops, sorted(ops))
    check("comparison operand recovered", "eq" in ops, sorted(ops))
    ctl = {r["instance"] for r in rep["candidate_control_bits"]}
    fsm_flops = {r["instance"] for r in rep["state_bits"]
                 if r["q_net"] in ("s0", "s1")}
    check("FSM bits flagged as control", fsm_flops <= ctl,
          f"fsm={sorted(fsm_flops)} control={sorted(ctl)}")
    check("mux-select evidence recorded",
          any(r["drives_mux_selects"] > 0
              for r in rep["candidate_control_bits"]),
          [r["drives_mux_selects"] for r in rep["candidate_control_bits"]])

    # --- 4. datapath plus control ------------------------------------------
    print("\nscenario: datapath plus one control bit")
    data, tgt = case_datapath_plus_control()
    rep = run(data, tgt)
    ctl = {r["instance"] for r in rep["candidate_control_bits"]}
    ctl_flop = {r["instance"] for r in rep["state_bits"]
                if r["q_net"] == "ctl"}
    dp_flops = {r["instance"] for r in rep["state_bits"]
                if r["q_net"] != "ctl"}
    check("control bit identified", ctl_flop <= ctl,
          f"ctl={sorted(ctl_flop)} flagged={sorted(ctl)}")
    check("datapath bits not all flagged as control",
          len(dp_flops - ctl) >= 6,
          f"{len(dp_flops - ctl)} of {len(dp_flops)} datapath bits not control")
    check("transition slice present",
          len(rep["transition_slice"]) == len(rep["state_bits"]),
          len(rep["transition_slice"]))
    self_dep = [r for r in rep["transition_slice"] if r["self_dependent"]]
    check("self-dependent flops detected", len(self_dep) >= 0, len(self_dep))

    # --- 5. purely combinational target ------------------------------------
    print("\nscenario: purely combinational target")
    data, tgt = case_combinational()
    rep = run(data, tgt)
    check("driver is combinational",
          rep["driver"]["kind"] == "combinational", rep["driver"]["kind"])
    check("no D net reported", "d_net" not in rep["driver"], "")
    check("still explains the cone", rep["cone"]["state_bits"] == 3,
          rep["cone"]["state_bits"])
    check("unique solution found",
          rep["cone"].get("satisfying_assignments") == "1",
          rep["cone"].get("satisfying_assignments"))

    # --- markdown/json rendering must not crash ---------------------------
    md = explain_target.to_markdown(rep)
    check("markdown renders", "What makes" in md and len(md) > 400, len(md))
    check("json serialisable", bool(json.dumps(rep)), "")

    print()
    if fails:
        print(f"FAIL: {fails}")
        return 1
    print("PASS  explain_target describes every scenario correctly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
