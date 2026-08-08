"""Incremental bounded model checking over a recovered netlist.

Unrolls the circuit one cycle at a time, keeping a single z3 solver whose
assertions accumulate monotonically, so deepening the search reuses everything
already learned instead of rebuilding the formula.  That is the difference
between a search over 200 cycles being feasible and not.

Search-space control is the other half.  A design that consumes serial ASCII
has an astronomical raw input space but a tiny constrained one, so data inputs
can be grouped into words and restricted to a character class, while protocol
control signals are pinned to known values rather than left symbolic.

Every satisfying assignment is replayed through the concrete simulator before
being reported, so a solution has always been confirmed by an independent path.

Examples:
  python scripts/solve_bmc.py net.json --output success --search 8 64 --hold en=1
  python scripts/solve_bmc.py net.json --output success --cycles 64 --any-cycle \\
      --data A --group A=8 --charset A=printable --start-cycle 0
"""

import argparse
import json
import sys
import time

import z3

from cells import BoolOps, Z3Ops
from sim import Circuit

CHARSETS = {
    "printable": [(0x20, 0x7E)],
    "ascii": [(0x00, 0x7F)],
    "alnum": [(0x30, 0x39), (0x41, 0x5A), (0x61, 0x7A)],
    "lower": [(0x61, 0x7A)],
    "upper": [(0x41, 0x5A)],
    "digits": [(0x30, 0x39)],
    "hexdigits": [(0x30, 0x39), (0x41, 0x46), (0x61, 0x66)],
    "byte": [(0x00, 0xFF)],
}


def _reset_level(cfg, asserted):
    """Level to drive on the reset net.  Active-low reset asserts at 0."""
    return asserted if cfg.reset_active_high else (not asserted)


class IncrementalBMC:
    """Unroll on demand; the solver is never rebuilt."""

    def __init__(self, circuit, cfg):
        self.c = circuit
        self.cfg = cfg
        self.ops = Z3Ops()
        self.s = z3.Solver()
        self.assertions = []
        self.uid = 0
        self.cycle_inputs = []      # per cycle: {net: z3 expr}
        self.success = []           # per cycle: z3 expr for the target
        self.state = {i: z3.BoolVal(False) for i in circuit.flops}
        self._reset_prefix()

    # ---------------------------------------------------------------- utils
    def _add(self, e):
        self.s.add(e)
        self.assertions.append(e)

    def _wrap(self, name, expr):
        if z3.is_true(expr) or z3.is_false(expr) or z3.is_const(expr):
            return expr
        self.uid += 1
        v = z3.Bool(f"w{self.uid}")
        self._add(v == expr)
        return v

    def _const_inputs(self, rst_asserted):
        cfg = self.cfg
        ins = {}
        for n in self.c.inputs:
            if n == cfg.clock:
                continue
            if n == cfg.reset_net:
                ins[n] = z3.BoolVal(_reset_level(cfg, rst_asserted))
            else:
                ins[n] = z3.BoolVal(bool(cfg.hold.get(n, 0)))
        return ins

    def _reset_prefix(self):
        for _ in range(self.cfg.reset_cycles):
            ins = self._const_inputs(rst_asserted=True)
            self.state, _ = self.c.step(self.ops, ins, self.state,
                                        self.cfg.clock, self._wrap)

    # --------------------------------------------------------------- extend
    def extend(self):
        """Add one more cycle; returns its index."""
        t = len(self.cycle_inputs)
        cfg = self.cfg
        ins = {}
        for n in self.c.inputs:
            if n == cfg.clock:
                continue
            if n == cfg.reset_net:
                ins[n] = z3.BoolVal(_reset_level(cfg, False))
                continue
            if n in cfg.pattern:
                bits = cfg.pattern[n]
                ch = bits[t] if t < len(bits) else bits[-1]
                ins[n] = z3.BoolVal(ch == "1")
                continue
            if n in cfg.hold:
                ins[n] = z3.BoolVal(bool(cfg.hold[n]))
                continue
            if cfg.data and n not in cfg.data:
                ins[n] = z3.BoolVal(bool(cfg.default))
                continue
            ins[n] = z3.Bool(f"{n}@{t}")
        self.cycle_inputs.append(ins)

        self.state, vals = self.c.step(self.ops, ins, self.state,
                                       cfg.clock, self._wrap)
        if cfg.output not in vals:
            raise SystemExit(f"output '{cfg.output}' not driven; "
                             f"available: {self.c.outputs}")
        self.success.append(vals[cfg.output])
        self._apply_group_constraints(t)
        return t

    def _apply_group_constraints(self, t):
        """Once a group of `width` cycles is complete, constrain its value."""
        cfg = self.cfg
        for net, width in cfg.group.items():
            if net not in self.c.inputs:
                continue
            start = cfg.start_cycle
            if t < start:
                continue
            k = t - start + 1
            if k % width:
                continue
            bits = [self.cycle_inputs[start + k - width + j].get(net)
                    for j in range(width)]
            if any(b is None or not z3.is_const(b)
                   or b.decl().kind() != z3.Z3_OP_UNINTERPRETED for b in bits):
                continue
            val = z3.Sum([z3.If(b, 1 << (width - 1 - j), 0)
                          for j, b in enumerate(bits)])
            terms = []
            for lo, hi in cfg.charset.get(net, []):
                terms.append(z3.And(val >= lo, val <= hi))
            for lo, hi in cfg.ranges.get(net, []):
                terms.append(z3.And(val >= lo, val <= hi))
            if terms:
                self._add(z3.Or(terms) if len(terms) > 1 else terms[0])

    # ---------------------------------------------------------------- solve
    def check_at(self, t, any_cycle=False, extra=()):
        """Check success at cycle t, or at any cycle up to t."""
        goal = z3.Bool(f"goal_{t}_{int(any_cycle)}")
        target = (z3.Or(self.success[:t + 1]) if any_cycle
                  else self.success[t])
        self._add(z3.Implies(goal, target))
        assumptions = [goal] + list(extra)
        return self.s.check(*assumptions), goal

    def model_trace(self, model, n_cycles):
        trace = []
        for t in range(n_cycles):
            row = {}
            for n, v in self.cycle_inputs[t].items():
                if (z3.is_const(v)
                        and v.decl().kind() == z3.Z3_OP_UNINTERPRETED):
                    row[n] = z3.is_true(model.eval(v, model_completion=True))
                else:
                    row[n] = z3.is_true(v)
            trace.append(row)
        return trace

    def block(self, trace, nets):
        """Forbid this exact assignment, for solution enumeration."""
        lits = []
        for t, row in enumerate(trace):
            for n in nets:
                v = self.cycle_inputs[t].get(n)
                if (v is not None and z3.is_const(v)
                        and v.decl().kind() == z3.Z3_OP_UNINTERPRETED):
                    lits.append(v != z3.BoolVal(row[n]))
        if lits:
            self._add(z3.Or(lits))


# ------------------------------------------------------------------ replay

def replay(circuit, cfg, trace, verbose=False):
    """Re-run a trace concretely; returns (success_seen, cycle, outputs)."""
    ops = BoolOps()
    state = circuit.reset_state()
    for _ in range(cfg.reset_cycles):
        ins = {n: bool(cfg.hold.get(n, 0)) for n in circuit.inputs}
        if cfg.reset_net in circuit.inputs:
            ins[cfg.reset_net] = _reset_level(cfg, True)
        state, _ = circuit.step(ops, ins, state, cfg.clock)

    hist = []
    hit = None
    for t, row in enumerate(trace):
        ins = dict(row)
        if cfg.reset_net in circuit.inputs:
            ins.setdefault(cfg.reset_net, _reset_level(cfg, False))
        state, vals = circuit.step(ops, ins, state, cfg.clock)
        outs = {o: bool(vals[o]) for o in circuit.outputs}
        hist.append(outs)
        if outs.get(cfg.output) and hit is None:
            hit = t
    return hit is not None, hit, hist


# ------------------------------------------------------------------ report

def decode(trace, nets, width=8, start=0):
    out = {}
    for n in nets:
        bits = "".join(str(int(row.get(n, False))) for row in trace)
        entry = {"bits": bits}
        body = bits[start:]
        usable = len(body) - (len(body) % width)
        if usable > 0:
            groups = [body[i:i + width] for i in range(0, usable, width)]
            vals = [int(g, 2) for g in groups]
            entry["words"] = vals
            entry["hex"] = " ".join(f"{v:0{(width + 3) // 4}x}" for v in vals)
            if width == 8:
                entry["ascii"] = "".join(
                    chr(v) if 32 <= v < 127 else "." for v in vals)
        out[n] = entry
    return out


class Config:
    pass


def parse_args(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("netlist")
    ap.add_argument("--output", default="success")
    ap.add_argument("--clock", default="clk")
    ap.add_argument("--cycles", type=int)
    ap.add_argument("--search", nargs=2, type=int, metavar=("LO", "HI"))
    ap.add_argument("--any-cycle", action="store_true",
                    help="satisfy the output at ANY cycle <= depth")
    ap.add_argument("--reset-net", default="rst_n")
    ap.add_argument("--reset-cycles", type=int, default=1)
    ap.add_argument("--reset-active-high", action="store_true")
    ap.add_argument("--hold", action="append", default=[],
                    metavar="NET=0|1")
    ap.add_argument("--pattern", action="append", default=[],
                    metavar="NET=BITS", help="per-cycle bits, last char repeats")
    ap.add_argument("--data", action="append", default=[],
                    metavar="NET", help="symbolic data inputs (others default)")
    ap.add_argument("--default", type=int, default=0,
                    help="value for unlisted inputs when --data is used")
    ap.add_argument("--group", action="append", default=[],
                    metavar="NET=WIDTH")
    ap.add_argument("--charset", action="append", default=[],
                    metavar="NET=CLASS", help=f"one of {sorted(CHARSETS)}")
    ap.add_argument("--range", action="append", default=[],
                    metavar="NET=LO:HI", dest="ranges")
    ap.add_argument("--start-cycle", type=int, default=0)
    ap.add_argument("--enumerate", type=int, default=1,
                    metavar="K", dest="enumerate_k")
    ap.add_argument("--timeout", type=int, default=0,
                    help="per-check timeout in seconds (0 = none)")
    ap.add_argument("--json", default=None)
    ap.add_argument("--vcd", default=None)
    return ap.parse_args(argv)


def build_config(a):
    cfg = Config()
    cfg.output = a.output
    cfg.clock = a.clock
    cfg.reset_net = a.reset_net
    cfg.reset_cycles = a.reset_cycles
    cfg.reset_active_high = a.reset_active_high
    cfg.default = a.default
    cfg.start_cycle = a.start_cycle
    cfg.data = set(a.data)
    cfg.hold = {}
    for h in a.hold:
        k, v = h.split("=")
        cfg.hold[k] = int(v)
    cfg.pattern = {}
    for p in a.pattern:
        k, v = p.split("=")
        cfg.pattern[k] = v
    cfg.group = {}
    for g in a.group:
        k, v = g.split("=")
        cfg.group[k] = int(v)
    cfg.charset = {}
    for cs in a.charset:
        k, v = cs.split("=")
        if v not in CHARSETS:
            raise SystemExit(f"unknown charset {v}; pick from {sorted(CHARSETS)}")
        cfg.charset[k] = CHARSETS[v]
        cfg.group.setdefault(k, 8)
    cfg.ranges = {}
    for r in a.ranges:
        k, v = r.split("=")
        lo, hi = v.split(":")
        cfg.ranges.setdefault(k, []).append((int(lo), int(hi)))
        cfg.group.setdefault(k, 8)
    return cfg


def main(argv=None):
    a = parse_args(argv)
    cfg = build_config(a)
    circuit = Circuit(json.load(open(a.netlist)))

    print(f"cells {len(circuit.insts)}  flops {len(circuit.flops)}  "
          f"inputs {circuit.inputs}")
    print(f"target '{cfg.output}'  reset {cfg.reset_net} for "
          f"{cfg.reset_cycles} cycle(s)  "
          f"{'active high' if cfg.reset_active_high else 'active low'}")
    symbolic = [n for n in circuit.inputs
                if n != cfg.clock and n != cfg.reset_net
                and n not in cfg.hold and n not in cfg.pattern
                and (not cfg.data or n in cfg.data)]
    print(f"symbolic inputs: {symbolic}")
    if cfg.group:
        print(f"grouping: {cfg.group}  start cycle {cfg.start_cycle}")

    depths = ([a.cycles] if a.cycles
              else list(range(a.search[0], a.search[1] + 1)) if a.search
              else [32])
    lo, hi = depths[0], depths[-1]

    bmc = IncrementalBMC(circuit, cfg)
    if a.timeout:
        bmc.s.set("timeout", a.timeout * 1000)

    t_start = time.time()
    found = None
    for t in range(hi):
        bmc.extend()
        depth = t + 1
        if depth < lo:
            continue
        t0 = time.time()
        res, _ = bmc.check_at(t, any_cycle=a.any_cycle)
        dt = time.time() - t0
        print(f"  depth {depth:4d}: {res}  ({dt:.2f}s, "
              f"{len(bmc.assertions)} assertions)")
        if res == z3.sat:
            found = (depth, bmc.s.model())
            break
        if res == z3.unknown:
            print("    solver returned unknown (timeout?); continuing")

    if not found:
        print(f"\nno solution up to depth {hi} "
              f"({time.time() - t_start:.1f}s total)")
        return 1

    depth, model = found
    results = []
    for k in range(a.enumerate_k):
        if k:
            res, _ = bmc.check_at(depth - 1, any_cycle=a.any_cycle)
            if res != z3.sat:
                print(f"\nonly {k} distinct solution(s) exist at depth {depth}")
                break
            model = bmc.s.model()
        trace = bmc.model_trace(model, depth)
        ok, hit, hist = replay(circuit, cfg, trace)
        dec = decode(trace, symbolic,
                     width=max(cfg.group.values()) if cfg.group else 8,
                     start=cfg.start_cycle)
        results.append({"depth": depth, "success_cycle": hit,
                        "replay_confirms": ok, "decoded": dec,
                        "trace": [{k2: int(v) for k2, v in row.items()}
                                  for row in trace]})
        print(f"\n--- solution {k + 1} at depth {depth} ---")
        print(f"concrete replay asserts {cfg.output}: {ok} "
              f"(first at cycle {hit})")
        for n, d in dec.items():
            print(f"  {n}: {d['bits']}")
            if "words" in d:
                print(f"      words {d['words']}")
                print(f"      hex   {d['hex']}")
                if "ascii" in d:
                    print(f"      ascii {d['ascii']!r}")
        if not ok:
            print("  !! replay disagrees with the solver -- the symbolic model "
                  "and the simulator differ; do not trust this result")
        bmc.block(trace, symbolic)

    if a.vcd and results:
        from vcd_write import write_vcd
        rows = []
        for _ in range(cfg.reset_cycles):
            r = {n: int(cfg.hold.get(n, 0)) for n in circuit.inputs}
            r[cfg.reset_net] = int(cfg.reset_active_high)
            r["clk"] = 0
            rows.append(r)
        for row in results[0]["trace"]:
            r = dict(row)
            r.setdefault(cfg.reset_net, int(not cfg.reset_active_high))
            r["clk"] = 0
            rows.append(r)
        names = sorted({k for r in rows for k in r})
        write_vcd(a.vcd, names, rows)
        print(f"\nwrote {a.vcd}")

    if a.json:
        json.dump({"depth": depth, "solutions": results},
                  open(a.json, "w"), indent=1)
        print(f"wrote {a.json}")

    print(f"\ntotal {time.time() - t_start:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
