"""Replay a solved input sequence through the FULL netlist and keep clocking.

A BMC solution ends at the cycle that asserts the target.  If the design answers
with a message, that message is emitted afterwards, by logic which is
deliberately outside the target's cone and therefore absent from the reduced
netlist.  So this replays against the full netlist and then keeps the clock
running.

Unknown primary inputs are the trap here.  Extraction can leave an internal net
with no driver -- a real primary input we could not label, or a tie-off we did
not recognise.  Silently defaulting it to zero would produce one plausible
answer with no indication that another value gives a different one, so this tool
refuses to guess: with a small number of unknowns it simulates every assignment
and reports whether the decoded output is invariant across them.

Usage:
  python scripts/replay_solution.py full.json solution.json --extend 64
"""

import argparse
import collections
import itertools
import json
import re
import sys

from cells import BoolOps
from sim import Circuit

BUS_RE = re.compile(r"^(.*?)\[(\d+)\]$")


def find_buses(ports):
    """Group ports named base[i] into buses."""
    buses = collections.defaultdict(dict)
    for p in ports:
        m = BUS_RE.match(p)
        if m:
            buses[m.group(1)][int(m.group(2))] = p
    return {k: dict(sorted(v.items())) for k, v in buses.items()}


def pack(bits_by_index, values, msb_high_index=True):
    """Pack a bus into an integer.  Index 0 is the LSB by Verilog convention."""
    v = 0
    for idx, net in bits_by_index.items():
        bit = 1 if values.get(net) else 0
        shift = idx if msb_high_index else (len(bits_by_index) - 1 - idx)
        v |= bit << shift
    return v


def printable(b):
    return 32 <= b < 127


def runs_of_text(byte_seq):
    """Consecutive nonzero printable bytes, joined into strings."""
    out, cur = [], []
    for b in byte_seq:
        if b != 0 and printable(b):
            cur.append(chr(b))
        else:
            if cur:
                out.append("".join(cur))
            cur = []
    if cur:
        out.append("".join(cur))
    return out


def dedup(seq):
    """Collapse runs of identical consecutive values.

    A held output bus repeats its value every cycle; a strobed one changes.
    Reporting both the raw and collapsed sequences avoids assuming which.
    """
    out = []
    for v in seq:
        if not out or out[-1] != v:
            out.append(v)
    return out


def simulate(circuit, cfg, trace, unknown_values):
    """Run reset prefix, then the solution, then the extension."""
    ops = BoolOps()
    state = circuit.reset_state()
    rst = cfg["reset_net"]
    active_high = cfg["reset_active_high"]

    def base_row(reset_asserted):
        row = {n: False for n in circuit.inputs}
        for n, v in unknown_values.items():
            row[n] = bool(v)
        for n, v in cfg["hold"].items():
            if n in row:
                row[n] = bool(v)
        if rst in row:
            row[rst] = bool(reset_asserted) if active_high \
                else (not reset_asserted)
        return row

    for _ in range(cfg["reset_cycles"]):
        state, _ = circuit.step(ops, base_row(True), state, cfg["clock"])

    history = []
    for row in trace:
        ins = base_row(False)
        for n, v in row.items():
            if n in ins and n not in unknown_values:
                ins[n] = bool(v)
        state, vals = circuit.step(ops, ins, state, cfg["clock"])
        history.append(vals)

    for _ in range(cfg["extend"]):
        # extension defaults: reset inactive, every other input low unless the
        # caller pinned it, unknowns at their trial assignment
        state, vals = circuit.step(ops, base_row(False), state, cfg["clock"])
        history.append(vals)

    return history


def analyse(circuit, cfg, history, bus, target):
    first_high = None
    rows = []
    for t, vals in enumerate(history):
        s = bool(vals.get(target)) if target in vals else None
        if s and first_high is None:
            first_high = t
        v = pack(bus, vals, cfg["msb_high_index"]) if bus else None
        rows.append({"cycle": t, "success": s, "bus": v})

    start = first_high if first_high is not None else 0
    after = [r for r in rows if r["cycle"] >= start]
    seq = [r["bus"] for r in after if r["bus"] is not None]
    return {
        "success_first_high": first_high,
        "rows": after,
        "bytes_all": seq,
        "bytes_dedup": dedup(seq),
        "text_all": runs_of_text(seq),
        "text_dedup": runs_of_text(dedup(seq)),
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("netlist", help="FULL netlist, not the reduced one")
    ap.add_argument("solution", help="JSON written by solve_bmc.py --json")
    ap.add_argument("--extend", type=int, default=64)
    ap.add_argument("--target", default="success")
    ap.add_argument("--clock", default="clk")
    ap.add_argument("--reset-net", default="rst_n")
    ap.add_argument("--reset-cycles", type=int, default=1)
    ap.add_argument("--reset-active-high", action="store_true")
    ap.add_argument("--bus", default=None,
                    help="output bus base name (auto-detected if unique)")
    ap.add_argument("--lsb-high-index", action="store_true",
                    help="treat index 0 as the MSB instead of the LSB")
    ap.add_argument("--hold", action="append", default=[], metavar="NET=0|1",
                    help="pin an input during the extension")
    ap.add_argument("--unknown", action="append", default=[],
                    metavar="NET=0|1",
                    help="pin an unknown input instead of enumerating it")
    ap.add_argument("--max-unknown", type=int, default=4)
    ap.add_argument("--solution-index", type=int, default=0)
    ap.add_argument("--json", default=None)
    ap.add_argument("--max-print", type=int, default=200)
    a = ap.parse_args(argv)

    data = json.load(open(a.netlist))
    circuit = Circuit(data)
    sol = json.load(open(a.solution))
    sols = sol.get("solutions") or []
    if not sols:
        raise SystemExit("solution file contains no solutions")
    trace = sols[a.solution_index]["trace"]

    hold = {}
    for h in a.hold:
        k, v = h.split("=")
        hold[k] = int(v)
    pinned = {}
    for u in a.unknown:
        k, v = u.split("=")
        pinned[k] = int(v)

    cfg = {
        "clock": a.clock, "reset_net": a.reset_net,
        "reset_cycles": a.reset_cycles,
        "reset_active_high": a.reset_active_high,
        "extend": a.extend, "hold": hold,
        "msb_high_index": not a.lsb_high_index,
    }

    # ---- classify inputs --------------------------------------------------
    driven_by_trace = {n for n in trace[0]} if trace else set()
    unknown = [n for n in circuit.inputs
               if n != a.clock and n != a.reset_net
               and n not in driven_by_trace and n not in hold
               and n not in pinned]

    buses = find_buses(data.get("ports", []))
    bus_name = a.bus
    if bus_name is None:
        if len(buses) == 1:
            bus_name = next(iter(buses))
        elif buses:
            raise SystemExit(f"several buses found {sorted(buses)}; "
                             f"choose one with --bus")
    bus = buses.get(bus_name, {}) if bus_name else {}

    print(f"netlist      : {a.netlist}  ({len(circuit.insts)} cells, "
          f"{len(circuit.flops)} flops)")
    print(f"solution     : {len(trace)} cycles, extending {a.extend} more")
    print(f"target       : {a.target}")
    print(f"output bus   : {bus_name or '(none)'} "
          f"{'[' + str(max(bus)) + ':0]' if bus else ''}")
    print(f"driven by solution: {sorted(driven_by_trace)}")
    if pinned:
        print(f"pinned unknowns   : {pinned}")
    print(f"UNKNOWN inputs    : {unknown if unknown else 'none'}")

    if len(unknown) > a.max_unknown:
        print(f"\nREFUSING: {len(unknown)} unknown inputs exceeds "
              f"--max-unknown {a.max_unknown}. Pin them with --unknown "
              f"NET=0/1, or raise the limit. Guessing a value silently would "
              f"produce one answer with no sign that another exists.")
        return 2

    # ---- run every assignment of the unknowns -----------------------------
    results = []
    for combo in itertools.product([0, 1], repeat=len(unknown)):
        assign = dict(zip(unknown, combo))
        assign.update(pinned)
        history = simulate(circuit, cfg, trace, assign)
        res = analyse(circuit, cfg, history, bus, a.target)
        res["assignment"] = assign
        results.append(res)

    # ---- report -----------------------------------------------------------
    for res in results:
        label = ", ".join(f"{k}={v}" for k, v in sorted(res["assignment"].items()))
        print("\n" + "=" * 68)
        print(f"assignment {label or '(none)'}:")
        print("=" * 68)
        print(f"  success first high: cycle {res['success_first_high']}")
        if bus:
            print(f"  {'cycle':>6}  {'succ':>4}  {'binary':>{len(bus)}}  "
                  f"{'hex':>4}  ascii")
            for r in res["rows"][:a.max_print]:
                v = r["bus"]
                ch = chr(v) if v is not None and printable(v) else "."
                print(f"  {r['cycle']:6d}  {int(bool(r['success'])):>4}  "
                      f"{v:0{len(bus)}b}  {v:>4x}  {ch}")
            if len(res["rows"]) > a.max_print:
                print(f"  ... {len(res['rows']) - a.max_print} more cycles "
                      f"(full trace in --json)")
            print(f"  output bytes (raw)      : {res['bytes_all']}")
            print(f"  output bytes (collapsed): {res['bytes_dedup']}")
            print(f"  ASCII (raw)      : {res['text_all']}")
            print(f"  ASCII (collapsed): {res['text_dedup']}")
        else:
            print("  no output bus identified; pass --bus")

    if len(results) > 1:
        raw = {tuple(r["bytes_all"]) for r in results}
        ded = {tuple(r["bytes_dedup"]) for r in results}
        txt = {tuple(r["text_dedup"]) for r in results}
        print("\n" + "=" * 68)
        print(f"invariant raw byte sequence      : "
              f"{'yes' if len(raw) == 1 else 'NO'}")
        print(f"invariant collapsed byte sequence: "
              f"{'yes' if len(ded) == 1 else 'NO'}")
        print(f"invariant string                 : "
              f"{'yes' if len(txt) == 1 else 'NO'}")
        if len(txt) > 1:
            print("  differing strings:")
            for r in results:
                lbl = ", ".join(f"{k}={v}" for k, v in
                                sorted(r["assignment"].items()))
                print(f"    {lbl}: {r['text_dedup']}")
        invariant = len(txt) == 1
    else:
        print("\ninvariant string: yes (no unknown inputs to vary)")
        invariant = True

    if a.json:
        json.dump({"bus": bus_name, "unknown_inputs": unknown,
                   "invariant_string": invariant,
                   "results": results}, open(a.json, "w"), indent=1)
        print(f"\nwrote {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
