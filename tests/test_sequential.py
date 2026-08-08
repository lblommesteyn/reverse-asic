"""Differential test of sequential semantics against independent references.

Each circuit is built from real SKY130 cells and simulated by sim.py, then
compared cycle by cycle against a reference model written directly from the
RTL-level description -- not from cells.py -- so a mistake in how the simulator
handles clock edges, async reset, enables or scan cannot cancel out.

This targets the assumption that evaluating the clock low then high, and
detecting a rising edge per flop, is correct for every supported cell type,
including inverted clocks, gated clocks and feedback loops.
"""

import os
import random
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

from cells import BoolOps
from netlist_builder import Builder
from sim import Circuit

CYCLES = 40
SEED = 20260806


def simulate(data, stim, outputs):
    c = Circuit(data)
    ops = BoolOps()
    state = c.reset_state()
    trace = []
    for row in stim:
        state, vals = c.step(ops, row, state)
        trace.append(tuple(bool(vals[o]) for o in outputs))
    return trace


# ------------------------------------------------------------- test circuits

def dff():
    b = Builder()
    for p in ("clk", "d"):
        b.port(p)
    b.port("q")
    b.add("dfxtp_1", {"CLK": "clk", "D": "d", "Q": "q"})
    return b.build(), ["q"], ["clk", "d"]


def ref_dff(stim):
    q, out = False, []
    for r in stim:
        nq = r["d"]
        out.append((q if False else nq,))
        q = nq
    return out


def dff_reset():
    b = Builder()
    for p in ("clk", "d", "rst_n"):
        b.port(p)
    b.port("q")
    b.add("dfrtp_1", {"CLK": "clk", "D": "d", "RESET_B": "rst_n", "Q": "q"})
    return b.build(), ["q"], ["clk", "d", "rst_n"]


def ref_dff_reset(stim):
    q, out = False, []
    for r in stim:
        q = False if not r["rst_n"] else r["d"]
        out.append((q,))
    return out


def dff_set():
    b = Builder()
    for p in ("clk", "d", "set_n"):
        b.port(p)
    b.port("q")
    b.add("dfstp_1", {"CLK": "clk", "D": "d", "SET_B": "set_n", "Q": "q"})
    return b.build(), ["q"], ["clk", "d", "set_n"]


def ref_dff_set(stim):
    q, out = False, []
    for r in stim:
        q = True if not r["set_n"] else r["d"]
        out.append((q,))
    return out


def dff_enable():
    b = Builder()
    for p in ("clk", "d", "de"):
        b.port(p)
    b.port("q")
    b.add("edfxtp_1", {"CLK": "clk", "D": "d", "DE": "de", "Q": "q"})
    return b.build(), ["q"], ["clk", "d", "de"]


def ref_dff_enable(stim):
    q, out = False, []
    for r in stim:
        q = r["d"] if r["de"] else q
        out.append((q,))
    return out


def scan_flop():
    b = Builder()
    for p in ("clk", "d", "rst_n", "scd", "sce"):
        b.port(p)
    b.port("q")
    b.add("sdfrtp_1", {"CLK": "clk", "D": "d", "RESET_B": "rst_n",
                       "SCD": "scd", "SCE": "sce", "Q": "q"})
    return b.build(), ["q"], ["clk", "d", "rst_n", "scd", "sce"]


def ref_scan_flop(stim):
    """Liberty: next_state = (D & !SCE) | (SCD & SCE)."""
    q, out = False, []
    for r in stim:
        nxt = r["scd"] if r["sce"] else r["d"]
        q = False if not r["rst_n"] else nxt
        out.append((q,))
    return out


def scan_enable_flop():
    b = Builder()
    for p in ("clk", "d", "de", "scd", "sce"):
        b.port(p)
    b.port("q")
    b.add("sedfxtp_1", {"CLK": "clk", "D": "d", "DE": "de",
                        "SCD": "scd", "SCE": "sce", "Q": "q"})
    return b.build(), ["q"], ["clk", "d", "de", "scd", "sce"]


def ref_scan_enable_flop(stim):
    """Liberty: (D & DE & !SCE) | (IQ & !DE & !SCE) | (SCD & SCE).

    Scan overrides the enable.  Getting this backwards is exactly the bug the
    Liberty audit caught in sim.py.
    """
    q, out = False, []
    for r in stim:
        if r["sce"]:
            q = r["scd"]
        elif r["de"]:
            q = r["d"]
        out.append((q,))
    return out


def shift_register(n=4):
    b = Builder()
    for p in ("clk", "din", "rst_n"):
        b.port(p)
    b.port("q")
    prev = "din"
    for i in range(n):
        out = "q" if i == n - 1 else b.net("s")
        b.add("dfrtp_1", {"CLK": "clk", "D": prev, "RESET_B": "rst_n",
                          "Q": out})
        prev = out
    return b.build(), ["q"], ["clk", "din", "rst_n"]


def ref_shift(stim, n=4):
    regs, out = [False] * n, []
    for r in stim:
        if not r["rst_n"]:
            regs = [False] * n
        else:
            regs = [r["din"]] + regs[:-1]
        out.append((regs[-1],))
    return out


def inverted_clock():
    """dfrtn samples on the rising edge of the internal clock, i.e. the falling
    edge of CLK_N."""
    b = Builder()
    for p in ("clk", "d", "rst_n"):
        b.port(p)
    b.port("q")
    b.add("dfrtn_1", {"CLK_N": "clk", "D": "d", "RESET_B": "rst_n", "Q": "q"})
    return b.build(), ["q"], ["clk", "d", "rst_n"]


def ref_inverted_clock(stim):
    """Documents a KNOWN LIMITATION rather than correct behaviour.

    A clock cycle is modelled as low-then-high, so a falling edge never occurs
    and a negedge flop never captures.  This is why validate_netlist.py FAILs on
    any negedge flop; the assertion below proves that refusal actually fires.
    """
    q, out = False, []
    for r in stim:
        if not r["rst_n"]:
            q = False
        out.append((q,))
    return out


def gated_clock():
    """Clock through an AND gate: the flop only captures while the gate is on."""
    b = Builder()
    for p in ("clk", "d", "gate", "rst_n"):
        b.port(p)
    b.port("q")
    b.add("and2_1", {"A": "clk", "B": "gate", "X": "gclk"})
    b.add("dfrtp_1", {"CLK": "gclk", "D": "d", "RESET_B": "rst_n", "Q": "q"})
    return b.build(), ["q"], ["clk", "d", "gate", "rst_n"]


def ref_gated_clock(stim):
    q, out = False, []
    for r in stim:
        if not r["rst_n"]:
            q = False
        elif r["gate"]:
            q = r["d"]
        out.append((q,))
    return out


def counter2():
    """2-bit ripple-free counter: q0 toggles, q1 toggles when q0 is set."""
    b = Builder()
    for p in ("clk", "rst_n", "en"):
        b.port(p)
    b.port("q0")
    b.port("q1")
    b.add("xor2_1", {"A": "q0", "B": "en", "X": "d0"})
    b.add("and2_1", {"A": "q0", "B": "en", "X": "c1"})
    b.add("xor2_1", {"A": "q1", "B": "c1", "X": "d1"})
    b.add("dfrtp_1", {"CLK": "clk", "D": "d0", "RESET_B": "rst_n", "Q": "q0"})
    b.add("dfrtp_1", {"CLK": "clk", "D": "d1", "RESET_B": "rst_n", "Q": "q1"})
    return b.build(), ["q1", "q0"], ["clk", "rst_n", "en"]


def ref_counter2(stim):
    q0 = q1 = False
    out = []
    for r in stim:
        if not r["rst_n"]:
            q0 = q1 = False
        else:
            n0 = q0 != r["en"]
            n1 = q1 != (q0 and r["en"])
            q0, q1 = n0, n1
        out.append((q1, q0))
    return out


def lfsr3():
    """3-bit LFSR with x^3 + x^2 + 1 feedback."""
    b = Builder()
    for p in ("clk", "rst_n"):
        b.port(p)
    b.port("q2")
    b.add("xor2_1", {"A": "q2", "B": "q1i", "X": "fb"})
    b.add("dfstp_1", {"CLK": "clk", "D": "fb", "SET_B": "rst_n", "Q": "q0i"})
    b.add("dfrtp_1", {"CLK": "clk", "D": "q0i", "RESET_B": "rst_n",
                      "Q": "q1i"})
    b.add("dfrtp_1", {"CLK": "clk", "D": "q1i", "RESET_B": "rst_n", "Q": "q2"})
    return b.build(), ["q2"], ["clk", "rst_n"]


def ref_lfsr3(stim):
    q0 = q1 = q2 = False
    out = []
    for r in stim:
        if not r["rst_n"]:
            q0, q1, q2 = True, False, False
        else:
            fb = q2 != q1
            q0, q1, q2 = fb, q0, q1
        out.append((q2,))
    return out


CASES = [
    ("simple DFF", dff, ref_dff),
    ("DFF with async reset", dff_reset, ref_dff_reset),
    ("DFF with async set", dff_set, ref_dff_set),
    ("DFF with enable", dff_enable, ref_dff_enable),
    ("scan flop (scan vs data)", scan_flop, ref_scan_flop),
    ("scan+enable flop (scan overrides enable)", scan_enable_flop,
     ref_scan_enable_flop),
    ("4-bit shift register", shift_register, ref_shift),
    ("negedge flop (inverted clock)", inverted_clock, ref_inverted_clock),
    ("gated clock", gated_clock, ref_gated_clock),
    ("2-bit counter", counter2, ref_counter2),
    ("3-bit LFSR", lfsr3, ref_lfsr3),
]


def refusal_checks():
    """Cells the simulator cannot represent must be refused, not mis-simulated."""
    import contextlib, io, json, tempfile
    import validate_netlist
    out = []
    specs = [
        ("level-sensitive latch", "dlxtp_1",
         {"GATE": "g", "D": "d", "Q": "q"}, "level-sensitive-latches"),
        ("falling-edge flop", "dfrtn_1",
         {"CLK_N": "c", "D": "d", "RESET_B": "r", "Q": "q"}, "negedge-flops"),
    ]
    for label, cell, pins, check in specs:
        b = Builder()
        for p in set(pins.values()):
            b.port(p)
        b.add(cell, pins)
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        json.dump(b.build(), open(path, "w"))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            verdict = validate_netlist.validate(path)
        os.remove(path)
        fired = any(l.startswith("[FAIL]") and check in l
                    for l in buf.getvalue().splitlines())
        ok = verdict == "FAIL" and fired
        print(f"  {'ok  ' if ok else 'FAIL'}  refuses {label:38s} "
              f"verdict={verdict}")
        out.append((label, ok))
    return out


def main():
    rng = random.Random(SEED)
    fails = []
    for name, build, ref in CASES:
        data, outs, ins = build()
        stim = []
        for t in range(CYCLES):
            row = {p: bool(rng.getrandbits(1)) for p in ins if p != "clk"}
            # exercise reset at the start and once in the middle
            for rname in ("rst_n", "set_n"):
                if rname in row:
                    row[rname] = not (t == 0 or t == CYCLES // 2)
            stim.append(row)
        got = simulate(data, stim, outs)
        want = ref(stim)
        bad = [(t, g, w) for t, (g, w) in enumerate(zip(got, want)) if g != w]
        print(f"  {'ok  ' if not bad else 'FAIL'}  {name:42s} "
              f"{CYCLES} cycles"
              + ("" if not bad else f", {len(bad)} mismatches, first at "
                                    f"cycle {bad[0][0]}: got {bad[0][1]} "
                                    f"want {bad[0][2]}"))
        if bad:
            fails.append(name)

    print("\n  refusal of storage the simulator cannot represent:")
    for label, ok in refusal_checks():
        if not ok:
            fails.append(f"refusal: {label}")

    print(f"\n{len(CASES)} sequential circuits x {CYCLES} cycles")
    if fails:
        print(f"FAIL: {fails}")
        return 1
    print("PASS  simulator matches independent RTL references")
    return 0


if __name__ == "__main__":
    sys.exit(main())
