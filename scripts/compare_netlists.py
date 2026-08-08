"""Structurally compare a recovered netlist (JSON) against a gold Verilog netlist.

Names are not expected to match, so equality is checked by Weisfeiler-Lehman
colour refinement over the cell/net bipartite graph, with cell types as node
labels and pin names as edge labels.  Top-level ports are seeded with their own
names, which anchors the refinement and makes a matching colour multiset very
strong evidence of isomorphism.

Usage: python scripts/compare_netlists.py recovered.json gold_netlist.v
"""

import hashlib
import json
import re
import sys
import collections

from gdslib import POWER_PINS, is_functional

INST_RE = re.compile(
    r"(sky130_fd_sc_hd__\w+)\s+(\\?\S+?)\s*\((.*?)\)\s*;", re.S
)
CONN_RE = re.compile(r"\.(\w+)\s*\(\s*(\\?[^)]*?)\s*\)")


def parse_verilog(path):
    text = open(path).read()
    text = re.sub(r"//.*", "", text)
    insts = []
    for m in INST_RE.finditer(text):
        cell, name, body = m.group(1), m.group(2), m.group(3)
        if not is_functional(cell):
            continue
        pins = {}
        for pm in CONN_RE.finditer(body):
            pin, net = pm.group(1), pm.group(2).strip()
            if pin in POWER_PINS or net in POWER_PINS or not net:
                continue
            pins[pin] = net
        insts.append({"name": name, "cell": cell, "pins": pins})
    ports = re.search(r"module\s+\w+\s*\((.*?)\)\s*;", text, re.S)
    ports = [p.strip() for p in ports.group(1).split(",")] if ports else []
    return insts, ports


def h(s):
    return hashlib.blake2b(s.encode(), digest_size=8).hexdigest()


def wl_signature(insts, ports, rounds=6):
    """Return (cell colour multiset, net colour multiset) after WL refinement."""
    port_set = set(ports)
    cell_col = {i["name"]: h(i["cell"]) for i in insts}
    net_col = {}
    for i in insts:
        for net in i["pins"].values():
            net_col.setdefault(net, h("PORT:" + net) if net in port_set else h("net"))

    net_pins = collections.defaultdict(list)
    for i in insts:
        for pin, net in i["pins"].items():
            net_pins[net].append((pin, i["name"]))

    for _ in range(rounds):
        new_cell = {}
        for i in insts:
            parts = sorted(f"{p}:{net_col[n]}" for p, n in i["pins"].items())
            new_cell[i["name"]] = h(cell_col[i["name"]] + "|" + "|".join(parts))
        new_net = {}
        for net, refs in net_pins.items():
            parts = sorted(f"{p}:{cell_col[c]}" for p, c in refs)
            new_net[net] = h(net_col[net] + "|" + "|".join(parts))
        cell_col, net_col = new_cell, new_net

    return (
        collections.Counter(cell_col.values()),
        collections.Counter(net_col.values()),
        cell_col,
        net_col,
    )


def main(json_path, verilog_path):
    rec = json.load(open(json_path))
    rec_insts = rec["instances"]
    rec_ports = [p for p in rec["ports"] if p not in POWER_PINS]
    gold_insts, gold_ports = parse_verilog(verilog_path)
    gold_ports = [p for p in gold_ports if p not in POWER_PINS]

    print(f"recovered: {len(rec_insts)} cells, ports {sorted(rec_ports)}")
    print(f"gold:      {len(gold_insts)} cells, ports {sorted(gold_ports)}")

    rc = collections.Counter(i["cell"] for i in rec_insts)
    gc = collections.Counter(i["cell"] for i in gold_insts)
    if rc == gc:
        print("PASS  cell-type histograms identical")
    else:
        print("FAIL  cell-type histogram mismatch:")
        for k in sorted(set(rc) | set(gc)):
            if rc[k] != gc[k]:
                print(f"   {k}: recovered {rc[k]} vs gold {gc[k]}")

    r_cells, r_nets, r_cmap, _ = wl_signature(rec_insts, rec_ports)
    g_cells, g_nets, g_cmap, _ = wl_signature(gold_insts, gold_ports)

    ok = True
    if r_cells == g_cells:
        print("PASS  WL cell colour multisets identical")
    else:
        ok = False
        print("FAIL  WL cell colours differ "
              f"({len(r_cells - g_cells)} recovered-only, {len(g_cells - r_cells)} gold-only)")
    if r_nets == g_nets:
        print("PASS  WL net colour multisets identical")
    else:
        ok = False
        print("FAIL  WL net colours differ")

    if ok:
        # Report the induced instance-name mapping where colours are unique.
        inv = collections.defaultdict(list)
        for n, c in g_cmap.items():
            inv[c].append(n)
        uniq = sum(1 for c, v in inv.items() if len(v) == 1)
        print(f"\nnetlists are structurally isomorphic; "
              f"{uniq}/{len(g_cmap)} cells individually identified by colour")
    return ok


if __name__ == "__main__":
    sys.exit(0 if main(sys.argv[1], sys.argv[2]) else 1)
