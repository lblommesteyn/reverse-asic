"""Regressions for replay_solution.py, on circuits that emit a known message.

Each scenario builds a design whose output generator is deliberately outside the
target's cone -- the situation the tool exists for -- emits a string we chose,
and is then replayed from a synthetic solution file.

The unknown-input cases are the important ones: the tool must never pick a value
silently, and must say plainly whether the decoded message depends on that
choice.
"""

import contextlib
import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

import replay_solution
from netlist_builder import Builder

GEN = os.path.join(HERE, "..", "generated")


def or_tree(b, nets, out):
    cur = list(nets)
    if not cur:
        b.add("buf_1", {"A": "VGND", "X": out})
        return out
    while len(cur) > 1:
        nxt = []
        for i in range(0, len(cur) - 1, 2):
            o = out if len(cur) == 2 else b.net("o")
            b.add("or2_1", {"A": cur[i], "B": cur[i + 1], "X": o})
            nxt.append(o)
        if len(cur) % 2:
            nxt.append(cur[-1])
        cur = nxt
    if cur[0] != out:
        b.add("buf_1", {"A": cur[0], "X": out})
    return out


def build_design(message, trigger_len=3, unknown_bit=None,
                 unknown_affects_bus=False):
    """A shift register whose all-ones state asserts success, plus an output
    generator that emits `message` one byte per cycle afterwards.

    The generator is fed only from success, so it sits outside the success cone.
    """
    b = Builder("emit")
    for p in ("clk", "rst_n", "I", "enable"):
        b.port(p)
    b.port("success")

    # --- acceptance: trigger_len consecutive 1s on I ---------------------
    stages, prev = [], "I"
    for _ in range(trigger_len):
        q = b.net("t")
        b.add("dfrtp_1", {"CLK": "clk", "D": prev, "RESET_B": "rst_n",
                          "Q": q})
        stages.append(q)
        prev = q
    cur = stages[0]
    for q in stages[1:]:
        nxt = b.net("acc")
        b.add("and2_1", {"A": cur, "B": q, "X": nxt})
        cur = nxt
    b.add("buf_1", {"A": cur, "X": "success"})

    # --- one-cycle pulse when success first rises ------------------------
    b.add("dfrtp_1", {"CLK": "clk", "D": "success", "RESET_B": "rst_n",
                      "Q": "succ_d"})
    b.add("inv_1", {"A": "succ_d", "Y": "nsucc_d"})
    b.add("and2_1", {"A": "success", "B": "nsucc_d", "X": "pulse"})

    # --- token chain: one token high per emitted byte --------------------
    tokens, prev = [], "pulse"
    for _ in range(len(message)):
        q = b.net("tok")
        b.add("dfrtp_1", {"CLK": "clk", "D": prev, "RESET_B": "rst_n",
                          "Q": q})
        tokens.append(q)
        prev = q

    # --- output bus: bit i is the OR of tokens whose byte has bit i set ---
    for bit in range(8):
        contributors = [tokens[j] for j, ch in enumerate(message)
                        if (ord(ch) >> bit) & 1]
        if unknown_bit is not None and unknown_affects_bus and bit == 0:
            contributors = contributors + ["UNKNOWN_NET"]
        or_tree(b, contributors, f"O[{bit}]")
        b.port(f"O[{bit}]")

    # an unknown input that does not touch the bus, to test invariance
    if unknown_bit is not None and not unknown_affects_bus:
        b.add("and2_1", {"A": "UNKNOWN_NET", "B": "enable", "X": "unused_sig"})

    return b.build()


def make_solution(bits, extra=None):
    """A solve_bmc-shaped solution file driving I with `bits`."""
    trace = []
    for ch in bits:
        row = {"I": int(ch), "enable": 1, "rst_n": 1}
        if extra:
            row.update(extra)
        trace.append(row)
    return {"depth": len(trace),
            "solutions": [{"depth": len(trace), "success_cycle": None,
                           "replay_confirms": True, "decoded": {},
                           "trace": trace}]}


def run(data, sol, args):
    fd, np_ = tempfile.mkstemp(suffix=".json", dir=GEN)
    os.close(fd)
    json.dump(data, open(np_, "w"))
    fd, sp = tempfile.mkstemp(suffix=".json", dir=GEN)
    os.close(fd)
    json.dump(sol, open(sp, "w"))
    out = os.path.join(GEN, "_replay.json")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = replay_solution.main([np_, sp, "--json", out] + args)
    rep = json.load(open(out)) if os.path.exists(out) and rc == 0 else None
    for p in (np_, sp, out):
        if os.path.exists(p):
            os.remove(p)
    return rc, rep, buf.getvalue()


def main():
    fails = []

    def check(name, cond, got):
        print(f"  {'ok  ' if cond else 'FAIL'}  {name:54s} {got}")
        if not cond:
            fails.append(name)

    MSG = "Hi!"

    # --- 1. message recovered only because we extended --------------------
    print("scenario: output generator emits a message after success")
    data = build_design(MSG)
    sol = make_solution("111")
    rc, rep, out = run(data, sol, ["--extend", "8"])
    check("runs cleanly", rc == 0, f"rc={rc}")
    res = rep["results"][0]
    check("success detected", res["success_first_high"] is not None,
          res["success_first_high"])
    check("message recovered from the extension",
          MSG in "".join(res["text_dedup"]), res["text_dedup"])
    check("no unknown inputs", rep["unknown_inputs"] == [],
          rep["unknown_inputs"])
    check("declared invariant", rep["invariant_string"] is True, "")

    # --- 2. without extension the message is missed -----------------------
    rc0, rep0, _ = run(data, sol, ["--extend", "0"])
    res0 = rep0["results"][0]
    check("without --extend the message is NOT present",
          MSG not in "".join(res0["text_dedup"]), res0["text_dedup"])

    # --- 3. unknown input that does not affect the bus --------------------
    print("\nscenario: unknown input that does not reach the output")
    data = build_design(MSG, unknown_bit=True, unknown_affects_bus=False)
    rc, rep, out = run(data, sol, ["--extend", "8"])
    check("unknown input detected, not defaulted",
          rep["unknown_inputs"] == ["UNKNOWN_NET"], rep["unknown_inputs"])
    check("both assignments simulated", len(rep["results"]) == 2,
          len(rep["results"]))
    check("string invariant across assignments",
          rep["invariant_string"] is True, "")
    check("message identical in both",
          all(MSG in "".join(r["text_dedup"]) for r in rep["results"]),
          [r["text_dedup"] for r in rep["results"]])

    # --- 4. unknown input that DOES affect the bus ------------------------
    print("\nscenario: unknown input that reaches the output bus")
    data = build_design(MSG, unknown_bit=True, unknown_affects_bus=True)
    rc, rep, out = run(data, sol, ["--extend", "8"])
    check("both assignments simulated", len(rep["results"]) == 2,
          len(rep["results"]))
    check("string reported as NOT invariant",
          rep["invariant_string"] is False, rep["invariant_string"])
    strings = {tuple(r["text_dedup"]) for r in rep["results"]}
    check("the two assignments really differ", len(strings) == 2,
          sorted(strings))
    check("report names the differing strings",
          "differing strings" in out, "")

    # --- 5. pinning an unknown collapses the enumeration ------------------
    rc, rep, out = run(data, sol, ["--extend", "8",
                                   "--unknown", "UNKNOWN_NET=0"])
    check("pinned unknown yields a single run", len(rep["results"]) == 1,
          len(rep["results"]))
    check("pinned unknown removed from the unknown list",
          rep["unknown_inputs"] == [], rep["unknown_inputs"])

    # --- 6. too many unknowns is a refusal, not a guess -------------------
    print("\nscenario: too many unknown inputs")
    b = Builder("many")
    for p in ("clk", "rst_n", "I"):
        b.port(p)
    b.port("success")
    b.add("dfrtp_1", {"CLK": "clk", "D": "I", "RESET_B": "rst_n",
                      "Q": "success"})
    terms = []
    for k in range(5):
        t = b.net("u")
        b.add("and2_1", {"A": f"UNK{k}", "B": "I", "X": t})
        terms.append(t)
    or_tree(b, terms, "O[0]")
    b.port("O[0]")
    rc, rep, out = run(b.build(), make_solution("11"),
                       ["--extend", "4", "--max-unknown", "4"])
    check("refuses rather than guessing", rc == 2, f"rc={rc}")
    check("refusal explains itself", "REFUSING" in out, "")

    print()
    if fails:
        print(f"FAIL: {fails}")
        return 1
    print("PASS  replay_solution recovers messages and handles unknowns safely")
    return 0


if __name__ == "__main__":
    sys.exit(main())
