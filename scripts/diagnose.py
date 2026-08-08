"""Deep investigation of the warnings validate_netlist.py raises.

`validate_netlist.py` is a fast gate: it says something is odd. This says what,
so a warning can be resolved as legitimate or escalated as an extraction defect
rather than being suppressed.

An undriven internal net matters far more than an unused output: an unused
output is normal in synthesised logic, whereas a net that drives inputs but has
no driver means either a real primary input we failed to label, a tie-off we
failed to recognise, or a broken connection that silently changes behaviour.

Usage: python scripts/diagnose.py recovered.json --output success [--json out]
"""

import argparse
import collections
import json

import cells as cl
from netgraph import build, full_cone
from sim import Circuit


def net_roles(c):
    drivers, sinks = collections.defaultdict(list), collections.defaultdict(list)
    for i, r in enumerate(c.insts):
        m = c.models[i]
        outs = set(m["outputs"])
        for pin, net in r["pins"].items():
            entry = {"inst": r["name"], "pin": pin, "cell": r["cell"],
                     "x": r["x"], "y": r["y"], "idx": i}
            (drivers if pin in outs else sinks)[net].append(entry)
    return drivers, sinks


def trace_clock(c, drivers, net, max_depth=40):
    """Walk backwards through buffers/inverters to the ultimate clock source."""
    path = []
    seen = set()
    cur = net
    inverted = False
    for _ in range(max_depth):
        if cur in seen:
            return cur, path, inverted, "loop"
        seen.add(cur)
        d = drivers.get(cur)
        if not d:
            return cur, path, inverted, ("port" if cur in c.data["ports"]
                                         else "undriven")
        if len(d) != 1:
            return cur, path, inverted, "multi-driver"
        e = d[0]
        base = cl.base_name(e["cell"])
        m = c.models[e["idx"]]
        if m["seq"]:
            return cur, path, inverted, "flop output (gated/divided clock)"
        if base.startswith(("clkbuf", "buf", "dly", "clkdly", "lpflow_clkbuf")):
            pass
        elif base.startswith(("inv", "clkinv")):
            inverted = not inverted
        else:
            return cur, path, inverted, f"combinational cell {base}"
        path.append(f"{e['inst']}({base})")
        src = None
        for pin in m["inputs"]:
            src = c.insts[e["idx"]]["pins"].get(pin)
            if src:
                break
        if src is None:
            return cur, path, inverted, "no input"
        cur = src
    return cur, path, inverted, "depth limit"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("netlist")
    ap.add_argument("--output", default="success")
    ap.add_argument("--json", default=None)
    a = ap.parse_args()

    data = json.load(open(a.netlist))
    c = Circuit(data)
    c.data = data
    drv_map, fanin = build(c)
    drivers, sinks = net_roles(c)
    ports = set(data.get("ports", []))
    consts = set(c.constants)

    report = {}

    cone_insts = set()
    if a.output in c.driver:
        cone_insts, _ = full_cone(c, drv_map, fanin, [a.output])
    out_ports = [p for p in ports if p != a.output and p in c.driver]
    out_cone = set()
    if out_ports:
        out_cone, _ = full_cone(c, drv_map, fanin, out_ports)

    def where(idx):
        tags = []
        if idx in cone_insts:
            tags.append("success-cone")
        if idx in out_cone and idx not in cone_insts:
            tags.append("output-only-cone")
        if not tags:
            tags.append("outside-both-cones")
        return ",".join(tags)

    # ---------------------------------------------------------- undriven nets
    print("=" * 72)
    print("UNDRIVEN NETS (drive inputs but have no driver)")
    print("=" * 72)
    undriven = sorted(n for n in sinks
                      if n not in drivers and n not in consts and n not in ports)
    report["undriven"] = []
    if not undriven:
        print("  none")
    for net in undriven:
        entry = {"net": net, "sinks": [], "in_success_cone": False,
                 "in_output_cone": False}
        print(f"\n  net '{net}': {len(sinks[net])} sink(s)")
        for e in sinks[net]:
            tag = where(e["idx"])
            entry["sinks"].append({**{k: e[k] for k in
                                      ("inst", "pin", "cell", "x", "y")},
                                   "cone": tag})
            entry["in_success_cone"] |= "success-cone" in tag
            entry["in_output_cone"] |= "output-only-cone" in tag
            print(f"      {e['inst']}.{e['pin']:8s} {e['cell']:34s} "
                  f"at ({e['x']:.2f}, {e['y']:.2f})  [{tag}]")
            # immediate logical fanout of that sink cell
            m = c.models[e["idx"]]
            outs = [c.insts[e["idx"]]["pins"].get(o) for o in m["outputs"]]
            outs = [o for o in outs if o]
            fo = []
            for o in outs:
                fo += [f"{s['inst']}.{s['pin']}" for s in sinks.get(o, [])]
            print(f"        drives {outs} -> {fo[:6]}"
                  f"{' ...' if len(fo) > 6 else ''}")
        entry["connected_to_power"] = net in consts
        print(f"      connected to VPWR/VGND: {net in consts}")
        print(f"      in success cone: {entry['in_success_cone']}   "
              f"in output-only cone: {entry['in_output_cone']}")
        print(f"      VERDICT: {'likely a real primary input (rename/label it)' if len(sinks[net]) > 3 else 'inspect: few sinks, could be a tie-off or a broken connection'}")
        report["undriven"].append(entry)

    # -------------------------------------------------------- dangling outputs
    print("\n" + "=" * 72)
    print("DANGLING OUTPUTS (driven nets with no sink and not a port)")
    print("=" * 72)
    dangling = sorted(n for n in drivers if n not in sinks and n not in ports)
    cats = collections.Counter()
    report["dangling"] = []
    for net in dangling:
        e = drivers[net][0]
        m = c.models[e["idx"]]
        multi = len(m["outputs"]) > 1
        other_used = False
        if multi:
            for o in m["outputs"]:
                on = c.insts[e["idx"]]["pins"].get(o)
                if on and on != net and (on in sinks or on in ports):
                    other_used = True
        if other_used:
            cat = "unused second output of a multi-output cell (normal)"
        elif e["idx"] in cone_insts:
            cat = "in success cone (suspicious: driven but unread)"
        elif e["idx"] in out_cone:
            cat = "output-generator logic only"
        else:
            cat = "outside both cones (dead logic)"
        cats[cat] += 1
        report["dangling"].append({"net": net, "inst": e["inst"],
                                   "cell": e["cell"], "pin": e["pin"],
                                   "x": e["x"], "y": e["y"], "category": cat})
    for cat, n in cats.most_common():
        print(f"  {n:4d}  {cat}")
    if not dangling:
        print("  none")
    susp = [d for d in report["dangling"] if "suspicious" in d["category"]]
    if susp:
        print("\n  suspicious ones:")
        for d in susp[:12]:
            print(f"      {d['inst']}.{d['pin']} {d['cell']} "
                  f"at ({d['x']:.2f}, {d['y']:.2f}) net {d['net']}")

    # ------------------------------------------------------------- clock tree
    print("\n" + "=" * 72)
    print("CLOCK TREE (each flop's CLK traced back through buffers)")
    print("=" * 72)
    roots = collections.Counter()
    inverted_count = 0
    details = []
    for i in c.flops:
        s, r = c.models[i]["seq"], c.insts[i]
        cnet = r["pins"].get(s["clk"])
        if cnet is None:
            roots["<no clock pin>"] += 1
            continue
        root, path, inverted, why = trace_clock(c, drivers, cnet)
        roots[f"{root} ({why})"] += 1
        inverted_count += bool(inverted)
        details.append({"flop": r["name"], "clk_net": cnet, "root": root,
                        "reason": why, "inverted": inverted,
                        "depth": len(path)})
    print(f"  {len(c.flops)} flops resolve to {len(roots)} clock source(s):")
    for k, v in roots.most_common():
        print(f"      {v:5d} flops  <- {k}")
    print(f"  flops on an inverted clock phase: {inverted_count}")
    depths = [d["depth"] for d in details]
    if depths:
        print(f"  buffer depth: min {min(depths)}, max {max(depths)}")
    single = len(roots) == 1
    print(f"  VERDICT: {'all flops share one clock source' if single else 'MULTIPLE clock sources -- check the non-primary ones'}")
    report["clock_roots"] = {k: v for k, v in roots.items()}
    report["clock_inverted_flops"] = inverted_count

    if a.json:
        json.dump(report, open(a.json, "w"), indent=1)
        print(f"\nwrote {a.json}")


if __name__ == "__main__":
    main()
