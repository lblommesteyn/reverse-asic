"""Randomised differential testing across the whole pipeline.

For each random circuit, several independent paths must agree:

  * simulate before and after JSON round-trip
  * simulate before and after reduction, from many random states, not only
    from reset -- a reduction that is only correct from the reset state would
    otherwise pass unnoticed
  * every BMC model must survive concrete replay
  * BMC unsat at a depth must agree with exhaustive concrete search at that
    depth, when the input space is small enough to enumerate

Failing seeds are printed so any failure is reproducible.
"""

import itertools
import json
import os
import random
import sys
import time

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

import z3

from cells import BoolOps
from netlist_builder import random_netlist
from reduce import reduce_netlist
from sim import Circuit
import solve_bmc

TRIALS = int(os.environ.get("FUZZ_TRIALS", "300"))
BASE_SEED = 424242
CYCLES = 10


def sim_trace(data, stim, outputs, init=None):
    """Simulate; `init` maps INSTANCE NAME -> initial flop value.

    Keying by name matters: reduction legitimately removes flops outside the
    cone, so the two circuits have different flop counts.  Assigning initial
    values positionally would compare unrelated states and report a bug that
    is not there.
    """
    c = Circuit(data)
    ops = BoolOps()
    state = c.reset_state()
    if init:
        for i in state:
            nm = c.insts[i]["name"]
            if nm in init:
                state[i] = init[nm]
    out = []
    for row in stim:
        state, vals = c.step(ops, row, state)
        out.append(tuple(bool(vals[o]) for o in outputs if o in vals))
    return out


def exhaustive_reachable(data, target, depth, inputs, cycles_limit=8):
    """Brute-force search for the target being true at `depth`, concretely."""
    c = Circuit(data)
    ops = BoolOps()
    free = [n for n in inputs if n != "clk"]
    if len(free) * depth > 16:
        return None  # too big to enumerate
    for bits in itertools.product([False, True], repeat=len(free) * depth):
        state = c.reset_state()
        state, _ = c.step(ops, {n: False for n in c.inputs}, state)
        vals = None
        for t in range(depth):
            row = dict(zip(free, bits[t * len(free):(t + 1) * len(free)]))
            row["rst_n"] = True
            state, vals = c.step(ops, row, state)
        if vals and bool(vals.get(target)):
            return True
    return False


def main():
    rng_master = random.Random(BASE_SEED)
    fails = []
    stats = {"trials": 0, "reduce_checked": 0, "bmc_sat": 0, "bmc_unsat": 0,
             "bmc_crosschecked": 0, "cells": 0}
    t0 = time.time()

    for trial in range(TRIALS):
        seed = rng_master.randrange(1 << 30)
        rng = random.Random(seed)
        try:
            data, outs = random_netlist(
                rng,
                n_inputs=rng.randint(2, 4),
                n_comb=rng.randint(4, 14),
                n_flops=rng.randint(1, 4),
                n_outputs=1)
            target = outs[0]
            c = Circuit(data)
            stats["cells"] += len(c.insts)
            inputs = [n for n in c.inputs if n != "clk"]

            stim = []
            for _ in range(CYCLES):
                row = {n: bool(rng.getrandbits(1)) for n in inputs}
                row["rst_n"] = True
                stim.append(row)

            base = sim_trace(data, stim, [target])

            # --- JSON round-trip -----------------------------------------
            again = sim_trace(json.loads(json.dumps(data)), stim, [target])
            if again != base:
                fails.append((seed, "json round-trip changes behaviour"))
                continue

            # --- reduction, from many random initial states ---------------
            red, rstats = reduce_netlist(data, target, verbose=False)
            stats["reduce_checked"] += 1
            ok = True
            flop_names = [c.insts[i]["name"] for i in c.flops]
            for _ in range(6):
                init = {nm: bool(rng.getrandbits(1)) for nm in flop_names}
                a = sim_trace(data, stim, [target], init)
                b = sim_trace(red, stim, [target], init)
                if a != b:
                    fails.append((seed, "reduction changes behaviour from a "
                                        "non-reset initial state"))
                    ok = False
                    break
            if not ok:
                continue

            # --- BMC: every model must replay, unsat must be real ---------
            cfg = solve_bmc.build_config(solve_bmc.parse_args(
                ["x", "--output", target, "--cycles", "3"]))
            circuit = Circuit(data)
            bmc = solve_bmc.IncrementalBMC(circuit, cfg)
            depth = 3
            for _ in range(depth):
                bmc.extend()
            res, _ = bmc.check_at(depth - 1)
            if res == z3.sat:
                stats["bmc_sat"] += 1
                trace = bmc.model_trace(bmc.s.model(), depth)
                hit, cyc, hist = solve_bmc.replay(circuit, cfg, trace)
                # the solver was asked for the target at EXACTLY depth-1; it may
                # also be true earlier, so check that specific cycle rather
                # than the first occurrence
                at_depth = bool(hist[depth - 1].get(target))
                if not at_depth:
                    fails.append((seed, f"BMC model does not replay: target "
                                        f"false at cycle {depth - 1} "
                                        f"(first true at {cyc})"))
            elif res == z3.unsat:
                stats["bmc_unsat"] += 1
                truth = exhaustive_reachable(data, target, depth, inputs)
                if truth is not None:
                    stats["bmc_crosschecked"] += 1
                    if truth:
                        fails.append((seed, "BMC says unsat but exhaustive "
                                            "concrete search found a solution"))
            stats["trials"] += 1
        except Exception as e:
            fails.append((seed, f"{type(e).__name__}: {e}"))

        if fails and len(fails) >= 5:
            break

    dt = time.time() - t0
    print(f"trials run          : {stats['trials']} / {TRIALS}")
    print(f"total cells built   : {stats['cells']}")
    print(f"reduction checks    : {stats['reduce_checked']} "
          f"(x6 random initial states each)")
    print(f"BMC sat / unsat     : {stats['bmc_sat']} / {stats['bmc_unsat']}")
    print(f"unsat cross-checked : {stats['bmc_crosschecked']} "
          f"against exhaustive concrete search")
    print(f"elapsed             : {dt:.1f}s")
    if fails:
        print(f"\nFAIL: {len(fails)} failing seeds")
        for seed, why in fails[:5]:
            print(f"  seed {seed}: {why}")
        print("reproduce with: random.Random(<seed>) in random_netlist")
        return 1
    print("\nPASS  all randomised differential checks agree")
    return 0


if __name__ == "__main__":
    sys.exit(main())
