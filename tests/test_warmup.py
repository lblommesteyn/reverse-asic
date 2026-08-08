"""End-to-end test of the GDS -> netlist -> simulation pipeline.

Drives the netlist recovered from warmup/04_final.gds exactly as the warm-up
spec describes (two 8-bit serial shift registers, S high iff A_reg + B_reg ==
496) and checks the simulated S against a Python reference model.

This validates the whole chain at once: instance extraction, geometric
connectivity, cell pin identification, the boolean cell library, and the
edge-triggered simulation semantics.
"""

import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from cells import BoolOps
from sim import load_circuit, summary

HERE = os.path.dirname(__file__)
NETLIST = os.path.join(HERE, "..", "generated", "warmup.json")


def run(circuit, a_bits, b_bits):
    """Reset, then shift 8 bits of each register in MSB-first, return S."""
    ops = BoolOps()
    state = circuit.reset_state()

    # assert reset (active low) for one cycle
    state, _ = circuit.step(ops, {"rst_n": False, "en": False,
                                  "A": False, "B": False}, state)

    vals = None
    for a, b in zip(a_bits, b_bits):
        state, vals = circuit.step(
            ops, {"rst_n": True, "en": True, "A": bool(a), "B": bool(b)}, state)
    return bool(vals["S"])


def main():
    c = load_circuit(NETLIST)
    summary(c)
    assert len(c.flops) == 16, f"expected 16 flops, got {len(c.flops)}"

    random.seed(0)
    cases = []
    # a spread of random pairs, plus every pair that should trigger success
    for _ in range(300):
        cases.append((random.randrange(256), random.randrange(256)))
    for a in range(240, 256):
        b = 496 - a
        if 0 <= b < 256:
            cases.append((a, b))

    fails = 0
    hits = 0
    for a, b in cases:
        a_bits = [(a >> i) & 1 for i in range(7, -1, -1)]
        b_bits = [(b >> i) & 1 for i in range(7, -1, -1)]
        got = run(c, a_bits, b_bits)
        want = (a + b) == 496
        hits += want
        if got != want:
            fails += 1
            if fails <= 5:
                print(f"  MISMATCH a={a} b={b} sum={a+b} got S={got} want {want}")

    print(f"\n{len(cases)} vectors, {hits} of them expected to assert S")
    if fails:
        print(f"FAIL  {fails} mismatches")
        return 1
    print("PASS  recovered netlist matches the warm-up specification exactly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
