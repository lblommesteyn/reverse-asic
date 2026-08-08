"""Structural simplification of a netlist, preserving behaviour exactly.

Applied before BMC, since solver time scales with the unrolled cell count and a
placed-and-routed netlist is full of buffers inserted for timing that carry no
information.

Transforms, all conservative:
  * cone restriction   drop cells that cannot influence the target
  * buffer collapse    a non-inverting buffer's output net is an alias of its
                       input net (skipped when the output is a port)
  * inverter pairs     inv(inv(x)) collapses when the middle net has fanout 1,
                       so no other cell observes it
  * constant folding   cells with all inputs constant become constants, which
                       then propagates
  * dead cell removal  cells whose output nobody reads

Correctness is checked by `tests/test_reduce.py`, which simulates the original
and the reduced netlist on the same random vectors and requires identical
outputs.  Reduction that changes behaviour is worse than no reduction at all.

Usage: python scripts/reduce.py in.json out.json --target success
"""

import argparse
import collections
import copy
import json

import cells as cl
from blocks import family
from netgraph import build, full_cone
from sim import Circuit

NONINVERTING_BUF = {"buf", "clkbuf", "dlygate4sd1", "dlygate4sd2",
                    "dlygate4sd3", "dlymetal6s2s", "lpflow_clkbufkapwr",
                    "bufbuf"}
INVERTERS = {"inv", "clkinv", "inv_lp", "invlp"}


def reduce_netlist(data, target=None, verbose=True):
    data = copy.deepcopy(data)
    c = Circuit(data)
    stats = {"original_cells": len(c.insts), "original_flops": len(c.flops)}

    keep = set(range(len(c.insts)))
    if target:
        drv, fanin = build(c)
        cone, _ = full_cone(c, drv, fanin, [target])
        keep = set(cone)
        stats["cone_cells"] = len(keep)
    insts = [c.insts[i] for i in sorted(keep)]

    ports = set(data.get("ports", []))
    constants = dict(data.get("constants", {}))
    const_nets = {n: bool(v) for n, v in constants.items()}

    changed = True
    rounds = 0
    while changed and rounds < 40:
        changed = False
        rounds += 1

        # ---- net aliasing from buffers and inverter pairs ----------------
        alias = {}
        fanout = collections.Counter()
        driver = {}
        for r in insts:
            m = cl.get_model(r["cell"])
            outs = set(m["outputs"]) if m else set()
            for pin, net in r["pins"].items():
                if pin in outs:
                    driver[net] = r
                else:
                    fanout[net] += 1

        # Cells whose output becomes an alias must themselves be deleted: if the
        # buffer stayed, it would still "drive" the net it was aliased onto, and
        # the netlist would have two drivers on one net.
        drop = set()
        for r in insts:
            base = cl.base_name(r["cell"])
            m = cl.get_model(r["cell"])
            if not m or m["seq"]:
                continue
            if base in NONINVERTING_BUF:
                src = r["pins"].get(m["inputs"][0])
                dst = r["pins"].get(m["outputs"][0])
                if src and dst and dst not in ports and src != dst:
                    alias[dst] = src
                    drop.add(id(r))
            elif base in INVERTERS:
                mid = r["pins"].get(m["inputs"][0])
                dst = r["pins"].get(m["outputs"][0])
                up = driver.get(mid)
                if (mid and dst and dst not in ports and mid not in ports
                        and fanout[mid] == 1 and up is not None
                        and id(up) not in drop
                        and cl.base_name(up["cell"]) in INVERTERS):
                    um = cl.get_model(up["cell"])
                    src = up["pins"].get(um["inputs"][0])
                    if src and src != dst:
                        alias[dst] = src
                        drop.add(id(r))
                        drop.add(id(up))

        if drop:
            insts = [r for r in insts if id(r) not in drop]
            changed = True

        if alias:
            def resolve(n, seen=None):
                seen = seen or set()
                while n in alias and n not in seen:
                    seen.add(n)
                    n = alias[n]
                return n

            for r in insts:
                for pin, net in list(r["pins"].items()):
                    rn = resolve(net)
                    if rn != net:
                        r["pins"][pin] = rn
                        changed = True

        # ---- constant folding --------------------------------------------
        from cells import BoolOps
        ops = BoolOps()
        for r in insts:
            m = cl.get_model(r["cell"])
            if not m or m["seq"] or not m["inputs"]:
                continue
            vals = {}
            ok = True
            for pin in m["inputs"]:
                net = r["pins"].get(pin)
                if net is None or net not in const_nets:
                    ok = False
                    break
                vals[pin] = const_nets[net]
            if not ok:
                continue
            res = m["fn"](ops, vals)
            outs = res if m.get("multi") else {m["outputs"][0]: res}
            for out, v in outs.items():
                net = r["pins"].get(out)
                if net is None or net in ports:
                    continue
                if net not in const_nets:
                    const_nets[net] = bool(v)
                    changed = True

        if const_nets:
            names = {True: "VPWR", False: "VGND"}
            for r in insts:
                m = cl.get_model(r["cell"])
                outs = set(m["outputs"]) if m else set()
                for pin, net in list(r["pins"].items()):
                    if pin in outs:
                        continue
                    if net in const_nets and net not in ("VPWR", "VGND"):
                        r["pins"][pin] = names[const_nets[net]]
                        changed = True
            for n, v in list(const_nets.items()):
                const_nets.setdefault(names[v], v)
            const_nets["VPWR"] = True
            const_nets["VGND"] = False

        # ---- dead cell removal -------------------------------------------
        used = set(ports)
        for r in insts:
            m = cl.get_model(r["cell"])
            outs = set(m["outputs"]) if m else set()
            for pin, net in r["pins"].items():
                if pin not in outs:
                    used.add(net)
        alive = []
        for r in insts:
            m = cl.get_model(r["cell"])
            outs = [r["pins"][o] for o in (m["outputs"] if m else [])
                    if o in r["pins"]]
            if not outs or any(o in used for o in outs):
                alive.append(r)
            else:
                changed = True
        insts = alive

    # drop pins that now point at a constant on cells we kept
    out = dict(data)
    out["instances"] = insts
    out["constants"] = {"VPWR": 1, "VGND": 0}
    nets = collections.defaultdict(list)
    for r in insts:
        for pin, net in r["pins"].items():
            nets[net].append(f"{r['name']}.{pin}")
    out["nets"] = {k: sorted(v) for k, v in sorted(nets.items())}
    out.pop("hashes", None)

    rc = Circuit(out)
    stats.update({
        "reduced_cells": len(insts),
        "reduced_flops": len(rc.flops),
        "reduced_nets": len(out["nets"]),
        "rounds": rounds,
        "cell_reduction_pct": round(
            100.0 * (1 - len(insts) / max(1, stats["original_cells"])), 1),
    })
    if verbose:
        print(f"cells   {stats['original_cells']} -> {stats['reduced_cells']} "
              f"({stats['cell_reduction_pct']}% smaller)")
        print(f"flops   {stats['original_flops']} -> {stats['reduced_flops']}")
        print(f"nets    {stats['reduced_nets']}   rounds {rounds}")
    out["reduction"] = stats
    return out, stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("netlist")
    ap.add_argument("out")
    ap.add_argument("--target", default=None)
    a = ap.parse_args()
    data = json.load(open(a.netlist))
    red, _ = reduce_netlist(data, a.target)
    json.dump(red, open(a.out, "w"), indent=1, sort_keys=True)
    print(f"wrote {a.out}")
