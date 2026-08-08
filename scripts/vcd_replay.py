"""Replay a VCD's stimulus through the recovered netlist.

Jane Street ships example_inputs.vcd showing the design being driven (with the
wrong inputs).  Feeding exactly those stimuli into the recovered netlist is the
best available end-to-end check on the real puzzle: if the recovered circuit
reproduces the VCD's recorded outputs cycle for cycle, the extraction is sound.
It also reveals the input protocol -- how many cycles reset is held, when the
enable rises, how many bits get shifted in.

Usage: python scripts/vcd_replay.py recovered.json inputs.vcd [--clock clk]
"""

import argparse
import json
import re
import collections

from cells import BoolOps
from sim import Circuit


def parse_vcd(path):
    """Return (signal_names, list of (time, {name: '0'|'1'|'x'})) changes."""
    ids = {}
    changes = []
    time = 0
    cur = {}
    with open(path) as f:
        text = f.read()

    for m in re.finditer(r"\$var\s+\w+\s+(\d+)\s+(\S+)\s+([^\s$]+)(?:\s*\[[^\]]*\])?\s*\$end", text):
        width, sid, name = int(m.group(1)), m.group(2), m.group(3)
        ids[sid] = name

    body = text[text.find("$enddefinitions"):]
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("$"):
            continue
        if line.startswith("#"):
            if cur:
                changes.append((time, dict(cur)))
                cur = {}
            time = int(line[1:])
        elif line[0] in "01xzXZ" and len(line) > 1:
            val, sid = line[0], line[1:]
            if sid in ids:
                cur[ids[sid]] = val
        elif line[0] in "bB":
            parts = line.split()
            if len(parts) == 2 and parts[1] in ids:
                cur[ids[parts[1]]] = parts[0][1:]
    if cur:
        changes.append((time, dict(cur)))
    return ids, changes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("netlist")
    ap.add_argument("vcd")
    ap.add_argument("--clock", default="clk")
    ap.add_argument("--max-cycles", type=int, default=2000)
    args = ap.parse_args()

    ids, changes = parse_vcd(args.vcd)
    names = sorted(set(ids.values()))
    print(f"VCD signals: {names}")
    print(f"{len(changes)} timestamps, t={changes[0][0]}..{changes[-1][0]}")

    c = Circuit(json.load(open(args.netlist)))
    print(f"circuit inputs : {c.inputs}")
    print(f"circuit outputs: {c.outputs}")

    common = [n for n in c.inputs if n in set(ids.values())]
    missing = [n for n in c.inputs if n not in set(ids.values())]
    print(f"driving from VCD: {common}")
    if missing:
        print(f"NOT in VCD (defaulting low): {missing}")

    # Rebuild a per-clock-edge stimulus: sample every rising edge of the clock
    level = {n: "0" for n in set(ids.values())}
    ops = BoolOps()
    state = c.reset_state()
    vecs = []
    prev_clk = "0"
    for t, delta in changes:
        level.update(delta)
        clk = level.get(args.clock, "0")
        if prev_clk == "0" and clk == "1":
            vecs.append((t, {n: level.get(n, "0") == "1" for n in common}))
        prev_clk = clk

    print(f"\n{len(vecs)} rising clock edges in the VCD")
    history = []
    for t, ins in vecs[:args.max_cycles]:
        state, vals = c.step(ops, ins, state, args.clock)
        history.append((t, ins, {o: bool(vals[o]) for o in c.outputs}))

    # Report only where an output changes, so long traces stay readable
    print("\ncycle  time      inputs -> outputs (changes only)")
    last = None
    for i, (t, ins, outs) in enumerate(history):
        if outs != last:
            istr = " ".join(f"{k}={int(v)}" for k, v in sorted(ins.items()))
            ostr = " ".join(f"{k}={int(v)}" for k, v in sorted(outs.items()))
            print(f"{i:5d}  {t:<9d} {istr}  ->  {ostr}")
            last = outs

    finals = history[-1][2] if history else {}
    print(f"\nfinal outputs: {finals}")
    any_high = collections.Counter()
    for _, _, outs in history:
        for k, v in outs.items():
            any_high[k] += v
    print(f"cycles each output was high: {dict(any_high)}")


if __name__ == "__main__":
    main()
