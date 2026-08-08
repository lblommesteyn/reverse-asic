"""Reduction must not change behaviour.

Simulates the original and the reduced netlist on the same random stimulus and
requires identical outputs on every cycle.  A reduction that is merely smaller
is worthless if it is not equivalent.
"""

import json
import os
import random
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

from cells import BoolOps
from reduce import reduce_netlist
from sim import Circuit

NETLIST = os.path.join(HERE, "..", "generated", "warmup.json")
TARGET = "S"
VECTORS = 120
CYCLES = 12


def run(circuit, stim, target):
    ops = BoolOps()
    state = circuit.reset_state()
    outs = []
    for row in stim:
        state, vals = circuit.step(ops, row, state)
        outs.append(bool(vals[target]))
    return outs


def main():
    data = json.load(open(NETLIST))
    orig = Circuit(data)
    reduced_data, stats = reduce_netlist(data, TARGET)
    red = Circuit(reduced_data)

    print(f"inputs original {orig.inputs}")
    print(f"inputs reduced  {red.inputs}")

    random.seed(1234)
    mismatches = 0
    for v in range(VECTORS):
        stim = []
        for t in range(CYCLES):
            row = {n: bool(random.getrandbits(1)) for n in orig.inputs}
            row["rst_n"] = t > 0
            stim.append(row)
        a = run(orig, stim, TARGET)
        b = run(red, [{k: v for k, v in row.items() if k in red.inputs}
                      for row in stim], TARGET)
        if a != b:
            mismatches += 1
            if mismatches <= 3:
                print(f"  MISMATCH vector {v}")
                print(f"    original {[int(x) for x in a]}")
                print(f"    reduced  {[int(x) for x in b]}")

    print(f"\n{VECTORS} random vectors x {CYCLES} cycles")
    print(f"cells {stats['original_cells']} -> {stats['reduced_cells']}, "
          f"flops {stats['original_flops']} -> {stats['reduced_flops']}")
    if mismatches:
        print(f"FAIL  {mismatches} vectors differ")
        return 1
    print("PASS  reduced netlist is behaviourally identical")
    return 0


if __name__ == "__main__":
    sys.exit(main())
