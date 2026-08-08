"""Explain the exact condition that can assert a target signal.

The structural report says what blocks exist.  This says what has to be TRUE for
a chosen output to go high, which is the question you actually need answered
before setting up a solve.

The design principle here is to resist flattening.  A registered acceptance
signal is usually a conjunction of a few meaningful predicates -- a phase or
counter condition, a comparison, an enable -- and printing one enormous boolean
expression destroys exactly that structure.  So the target's logic is decomposed
through pure AND/OR/buffer/inverter gates first, each resulting predicate is
characterised on its own, and only then is a simplified expression offered.

Usage: python scripts/explain_target.py <netlist.json> --target success
"""

import argparse
import collections
import json
import os

import z3

import blocks
import cells as cl
from netgraph import (build, comb_cone, cone_function, count_models,
                      flop_q_nets, register_graph, sccs, seq_fanin)
from sim import Circuit

MAX_BOUNDARY_FOR_COUNTING = 22
MAX_EXPR_CHARS = 2500


# ----------------------------------------------------------------- helpers

def gate_kind(circuit, idx):
    """Classify a cell as a pure AND / OR / NOT / BUF, or None if compound."""
    r, m = circuit.insts[idx], circuit.models[idx]
    if m["seq"]:
        return None
    base = cl.base_name(r["cell"])
    if base.startswith(("buf", "clkbuf", "dly", "lpflow_clkbuf",
                        "lpflow_lsbuf", "probe")):
        return "BUF"
    if base.startswith(("inv", "clkinv")):
        return "NOT"
    for pre, kind, inverting in (("nand", "AND", True), ("nor", "OR", True),
                                 ("and", "AND", False), ("or", "OR", False)):
        if base.startswith(pre):
            rest = base[len(pre):]
            # and2 / and3b / nor4bb ...  digits then optional 'b's
            if rest and rest[0].isdigit():
                return ("N" + kind) if inverting else kind
    return None


def decompose(circuit, drv, net, depth=0, max_depth=6, seen=None):
    """Break a net into a tree of AND/OR/NOT over predicate leaves.

    Expansion stops at compound cells, flops, primary inputs and constants, so
    the leaves are the meaningful intermediate predicates rather than raw gates.
    """
    seen = seen or set()
    node = {"net": net, "op": "LEAF", "children": [], "negated": False}
    if net in seen or depth >= max_depth:
        return node
    idx = drv.get(net)
    if idx is None or circuit.models[idx]["seq"]:
        return node
    kind = gate_kind(circuit, idx)
    if kind is None:
        node["op"] = "LEAF"
        node["root_cell"] = circuit.insts[idx]["cell"]
        return node

    seen = seen | {net}
    r, m = circuit.insts[idx], circuit.models[idx]
    ins = [(p, r["pins"][p]) for p in m["inputs"] if p in r["pins"]]

    if kind in ("BUF", "NOT"):
        child = decompose(circuit, drv, ins[0][1], depth, max_depth, seen)
        if kind == "NOT":
            child = {"net": net, "op": "NOT", "children": [child],
                     "negated": True}
        return child

    node["op"] = kind.lstrip("N") if kind.startswith("N") else kind
    node["negated"] = kind.startswith("N")
    node["root_cell"] = r["cell"]
    for pin, n in ins:
        child = decompose(circuit, drv, n, depth + 1, max_depth, seen)
        child["pin"] = pin
        # active-low pins invert their contribution
        child["input_inverted"] = pin.endswith("_N")
        node["children"].append(child)
    return node


def flatten_predicates(node, out=None):
    out = [] if out is None else out
    if node["op"] == "LEAF":
        out.append(node)
    else:
        for c in node["children"]:
            flatten_predicates(c, out)
    return out


def render_tree(node, indent=0, lines=None):
    lines = [] if lines is None else lines
    pad = "    " * indent
    if node["op"] == "LEAF":
        neg = "!" if node.get("input_inverted") else ""
        lines.append(f"{pad}{neg}{node['net']}")
    else:
        neg = "NOT " if node.get("negated") else ""
        lines.append(f"{pad}{neg}{node['op']}")
        for c in node["children"]:
            render_tree(c, indent + 1, lines)
    return lines


def predicate_stats(circuit, drv, fanin, qnet, net, known_comparators):
    insts, _, boundary = comb_cone(circuit, drv, fanin, [net])
    free = [b for b in boundary if b not in circuit.constants]
    state = [b for b in free if b in qnet]
    idx = drv.get(net)
    entry = {
        "net": net,
        "root_cell": circuit.insts[idx]["cell"] if idx is not None else None,
        "root_op": (gate_kind(circuit, idx) or "compound")
                   if idx is not None else "primary-input/constant",
        "cone_cells": len(insts),
        "state_bits": len(state),
        "boundary": len(free),
        "primary_inputs": sorted(b for b in free if b not in qnet),
        "bbox": blocks._bbox(circuit, insts) if insts else None,
        "matches_detected_comparator": net in known_comparators,
    }
    if 2 <= len(free) <= MAX_BOUNDARY_FOR_COUNTING and insts:
        ops = cl.Z3Ops()
        cache = {}

        def var_of(n):
            if n not in cache:
                cache[n] = z3.Bool(f"b_{n}")
            return cache[n]

        expr, _ = cone_function(circuit, net, drv, fanin, ops, var_of)
        if expr is not None and not isinstance(expr, bool):
            variables = [var_of(b) for b in sorted(free)]
            n, models = count_models(expr, variables, limit=64)
            entry["satisfying_assignments"] = (f"{n}" if n < 64 else ">=64")
            entry["input_space"] = 2 ** len(free)
            if n == 1:
                bits = {b: int(models[0][var_of(b)]) for b in sorted(free)}
                entry["unique_solution_bits"] = bits
                v = 0
                for b in sorted(free):
                    v = (v << 1) | bits[b]
                entry["unique_solution_int_msb_first"] = v
    return entry


# ------------------------------------------------------------------- main

def explain(data, target):
    c = Circuit(data)
    drv, fanin = build(c)
    qnet = flop_q_nets(c)
    edges, prim, dcone, _ = register_graph(c, drv, fanin)

    rep = {"target": target, "top": data.get("top")}

    # ---- 1. target driver -------------------------------------------------
    if target not in c.driver:
        raise SystemExit(f"'{target}' is not driven by any cell; "
                         f"driven ports: {c.outputs}")
    tidx, tpin = c.driver[target]
    tmodel, tinst = c.models[tidx], c.insts[tidx]
    driver = {"instance": tinst["name"], "cell": tinst["cell"],
              "x": tinst["x"], "y": tinst["y"]}
    if tmodel["seq"]:
        s = tmodel["seq"]
        driver.update({
            "kind": "registered",
            "q_net": tinst["pins"].get(s["q"]),
            "d_net": tinst["pins"].get(s["d"]),
            "clock_net": tinst["pins"].get(s["clk"]),
            "clock_edge": "negedge" if s.get("negedge") else "posedge",
        })
        for key, label in (("rst", "reset"), ("set", "set")):
            if key in s and s[key][0] in tinst["pins"]:
                driver[f"{label}_net"] = tinst["pins"][s[key][0]]
                driver[f"{label}_polarity"] = ("active_low" if s[key][1]
                                               else "active_high")
        logic_net = driver["d_net"]
    else:
        driver["kind"] = "combinational"
        logic_net = target
    rep["driver"] = driver
    rep["logic_net"] = logic_net

    # ---- 2/3. fanin, decomposition, expression ----------------------------
    insts, nets, boundary = comb_cone(c, drv, fanin, [logic_net])
    free = [b for b in boundary if b not in c.constants]
    state_bits = [b for b in free if b in qnet]
    rep["cone"] = {
        "cells": len(insts),
        "nets": len(nets),
        "state_bits": len(state_bits),
        "primary_inputs": sorted(b for b in free if b not in qnet),
        "constants": sorted(b for b in boundary if b in c.constants),
        "bbox": blocks._bbox(c, insts) if insts else None,
    }

    # How selective is the whole deciding condition?  If exactly one assignment
    # of its boundary satisfies it, the target is a comparison against a single
    # constant and that constant is recoverable directly.
    if 2 <= len(free) <= MAX_BOUNDARY_FOR_COUNTING and insts:
        ops0 = cl.Z3Ops()
        cache0 = {}

        def var0(n):
            if n not in cache0:
                cache0[n] = z3.Bool(f"c_{n}")
            return cache0[n]

        e0, _ = cone_function(c, logic_net, drv, fanin, ops0, var0)
        if e0 is not None and not isinstance(e0, bool):
            vars0 = [var0(b) for b in sorted(free)]
            n0, m0 = count_models(e0, vars0, limit=64)
            rep["cone"]["satisfying_assignments"] = (str(n0) if n0 < 64
                                                     else ">=64")
            rep["cone"]["input_space"] = 2 ** len(free)
            if n0 == 1:
                bits = {b: int(m0[0][var0(b)]) for b in sorted(free)}
                rep["cone"]["unique_solution_bits"] = bits
                v = 0
                for b in sorted(free):
                    v = (v << 1) | bits[b]
                rep["cone"]["unique_solution_int_msb_first"] = v

    # The IMMEDIATE operands of the deciding gate are the thing worth seeing
    # first: "target_D = phase_ok AND eq" is the shape of the answer.  The full
    # decomposition below expands through those operands, which is useful for
    # depth but destroys exactly that top-level structure, so both are reported.
    root_idx = drv.get(logic_net)
    rep["immediate_operands"] = []
    if root_idx is not None and not c.models[root_idx]["seq"]:
        rm, rr = c.models[root_idx], c.insts[root_idx]
        rep["root_gate"] = {"instance": rr["name"], "cell": rr["cell"],
                            "op": gate_kind(c, root_idx) or "compound"}
        for pin in rm["inputs"]:
            n = rr["pins"].get(pin)
            if n is None:
                continue
            st = predicate_stats(c, drv, fanin, qnet, n, set())
            st["pin"] = pin
            st["active_low_pin"] = pin.endswith("_N")
            rep["immediate_operands"].append(st)

    tree = decompose(c, drv, logic_net)
    rep["decomposition_tree"] = render_tree(tree)
    preds = flatten_predicates(tree)
    rep["top_level_operator"] = tree["op"]

    # dependency counts per intermediate net inside the cone
    usage = collections.Counter()
    for i in insts:
        for n in fanin[i]:
            if n in nets:
                usage[n] += 1
    rep["dominant_intermediate_nets"] = [
        {"net": n, "used_by_cells": u} for n, u in usage.most_common(15)]

    known_cmp = {d["net"] for d in
                 blocks.wide_equality(c, drv, fanin, qnet, min_state=3)}
    seen_pred = set()
    rep["predicates"] = []
    for p in preds:
        if p["net"] in seen_pred:
            continue
        seen_pred.add(p["net"])
        st = predicate_stats(c, drv, fanin, qnet, p["net"], known_cmp)
        st["contributes_inverted"] = bool(p.get("input_inverted"))
        rep["predicates"].append(st)
    rep["predicates"].sort(key=lambda d: (-d["state_bits"], -d["cone_cells"]))

    # simplified expression, only if it stays readable
    rep["expression"] = None
    if 0 < len(free) <= MAX_BOUNDARY_FOR_COUNTING + 8 and insts:
        ops = cl.Z3Ops()
        cache = {}

        def var_of(n):
            if n not in cache:
                cache[n] = z3.Bool(n)
            return cache[n]

        expr, _ = cone_function(c, logic_net, drv, fanin, ops, var_of)
        if expr is not None and not isinstance(expr, bool):
            simp = z3.simplify(expr)
            text = str(simp).replace("\n", " ")
            text = " ".join(text.split())
            if len(text) <= MAX_EXPR_CHARS:
                rep["expression"] = text
            else:
                rep["expression"] = (f"<{len(text)} chars: too large to be "
                                     f"useful; see the decomposition instead>")

    # ---- 5. state-bit table ----------------------------------------------
    comps = sccs(edges, c.flops)
    scc_of = {}
    for k, comp in enumerate(comps):
        for f in comp:
            scc_of[f] = k
    scc_size = {k: len(comp) for k, comp in enumerate(comps)}

    # sequential distance from each flop to the target
    dist = {}
    frontier = {qnet[b] for b in state_bits if b in qnet}
    for f in frontier:
        dist[f] = 1
    d = 1
    while frontier:
        nxt = set()
        for f in frontier:
            for netn in seq_fanin(c, f):
                for b in comb_cone(c, drv, fanin, [netn])[2]:
                    g = qnet.get(b)
                    if g is not None and g not in dist:
                        dist[g] = d + 1
                        nxt.add(g)
        frontier = nxt
        d += 1
        if d > len(c.flops) + 2:
            break

    fanout = collections.Counter()
    mux_sel = collections.Counter()
    for i, r in enumerate(c.insts):
        m = c.models[i]
        outs = set(m["outputs"])
        for pin, net in r["pins"].items():
            if pin in outs:
                continue
            fanout[net] += 1
            if pin in ("S", "S0", "S1"):
                mux_sel[net] += 1

    serial_inputs = {pi for pi, tgts in prim.items() if len(tgts) <= 4}
    broadcast_inputs = {pi for pi, tgts in prim.items() if len(tgts) > 4}

    rows = []
    for f in sorted(dist, key=lambda f: (dist[f], c.insts[f]["name"])):
        r, m = c.insts[f], c.models[f]
        s = m["seq"]
        q = r["pins"].get(s["q"])
        dn = r["pins"].get(s["d"])
        b = dcone.get(f, {}).get("boundary", set())
        pis = sorted(x for x in b if x not in qnet and x not in c.constants)
        rows.append({
            "instance": r["name"], "cell": r["cell"],
            "q_net": q, "d_net": dn,
            "scc": scc_of.get(f), "scc_size": scc_size.get(scc_of.get(f), 1),
            "sequential_distance": dist[f],
            "primary_inputs_in_d_cone": pis,
            "consumes_serial_input": bool(set(pis) & serial_inputs),
            "consumes_broadcast_input": bool(set(pis) & broadcast_inputs),
            "q_fanout": fanout.get(q, 0),
            "drives_mux_selects": mux_sel.get(q, 0),
            "x": r["x"], "y": r["y"],
        })
    rep["state_bits"] = rows

    # ---- 6. control vs datapath -------------------------------------------
    if rows:
        fanouts = sorted(r["q_fanout"] for r in rows)
        hi_cut = fanouts[int(len(fanouts) * 0.75)] if fanouts else 0
        control, datapath, scored = [], [], []
        # which flops feed which other flops' next state
        name_of = {i: c.insts[i]["name"] for i in c.flops}
        feeds_map = collections.defaultdict(set)
        for g in c.flops:
            for b in dcone.get(g, {}).get("boundary", set()):
                src = qnet.get(b)
                if src is not None and src != g:
                    feeds_map[name_of[src]].add(name_of[g])
        for r in rows:
            score = 0
            reasons = []
            if r["q_fanout"] > max(hi_cut, 4):
                score += 2
                reasons.append(f"high fanout {r['q_fanout']}")
            if r["drives_mux_selects"]:
                score += 2
                reasons.append(f"drives {r['drives_mux_selects']} mux selects")
            if not r["consumes_serial_input"]:
                score += 1
                reasons.append("does not consume serial input")
            if r["scc_size"] and r["scc_size"] <= 4:
                score += 1
                reasons.append(f"small SCC (size {r['scc_size']})")
            if r["consumes_broadcast_input"]:
                score += 1
                reasons.append("consumes a broadcast control input")
            scored.append(dict(r, control_score=score,
                               control_evidence=reasons))

        # FSM state bits come in coupled groups: a counter's low bit may drive
        # nothing but the next bit's D, so it scores low on its own evidence
        # while plainly being control.  Promote any bit that feeds a control
        # bit's next state and does not consume serial data.
        by_name = {r["instance"]: r for r in scored}
        ctrl_names = {r["instance"] for r in scored if r["control_score"] >= 4}
        for _ in range(3):
            promoted = set()
            for r in scored:
                if r["instance"] in ctrl_names:
                    continue
                # NB: deliberately not gated on "consumes serial input".  A
                # control input that happens to feed only a couple of flops
                # looks serial to the fanout heuristic, which would then block
                # promotion of genuine FSM bits.  Feeding a control bit's next
                # state is strong enough evidence by itself, and datapath bits
                # feed the next datapath stage rather than control.
                feeds = feeds_map.get(r["instance"], set())
                if feeds & ctrl_names:
                    # FSM bits are a coupled group, so feeding a control bit's
                    # next state is treated as decisive rather than as one more
                    # weak signal: a counter's low bit may have no other
                    # evidence at all while plainly being control.
                    r["control_score"] = max(r["control_score"], 4)
                    r["control_evidence"].append(
                        "feeds control state " +
                        ",".join(sorted(feeds & ctrl_names)[:3]))
                    promoted.add(r["instance"])
            if not promoted:
                break
            ctrl_names |= promoted

        for r in scored:
            (control if r["control_score"] >= 4 else datapath).append(r)
        control.sort(key=lambda d: -d["control_score"])
        rep["candidate_control_bits"] = control
        rep["datapath_bit_count"] = len(datapath)
    else:
        rep["candidate_control_bits"] = []
        rep["datapath_bit_count"] = 0

    # ---- 7. transition slice ----------------------------------------------
    slice_rows = []
    for r in rows:
        f = next(i for i in c.flops if c.insts[i]["name"] == r["instance"])
        b = dcone.get(f, {}).get("boundary", set())
        preds_q = sorted({c.insts[qnet[x]]["name"] for x in b if x in qnet})
        pis = sorted(x for x in b if x not in qnet and x not in c.constants)
        slice_rows.append({
            "flop": r["instance"],
            "next_state_depends_on_flops": preds_q,
            "next_state_depends_on_inputs": pis,
            "self_dependent": r["instance"] in preds_q,
        })
    rep["transition_slice"] = slice_rows
    return rep


# ---------------------------------------------------------------- rendering

def to_markdown(rep):
    L = []
    a = L.append
    t = rep["target"]
    a(f"# What makes `{t}` assert, in `{rep['top']}`")
    a("")
    d = rep["driver"]
    a("## 1. Target driver")
    a("")
    a(f"- driven by **{d['instance']}** (`{d['cell']}`) at "
      f"({d['x']}, {d['y']})")
    a(f"- kind: **{d['kind']}**")
    if d["kind"] == "registered":
        a(f"- Q net `{d['q_net']}`, D net `{d['d_net']}`")
        a(f"- clock `{d['clock_net']}` ({d['clock_edge']})")
        for k in ("reset", "set"):
            if f"{k}_net" in d:
                a(f"- {k} `{d[f'{k}_net']}` ({d[f'{k}_polarity']})")
        a("")
        a(f"So `{t}` is whatever `{d['d_net']}` was at the previous clock edge.")
    else:
        a("")
        a(f"`{t}` is combinational: it tracks its inputs with no clock delay.")
    a("")

    c = rep["cone"]
    a("## 2. Fan-in cone of the deciding net")
    a("")
    a(f"- combinational cells: **{c['cells']}**")
    a(f"- state bits it depends on: **{c['state_bits']}**")
    a(f"- primary inputs: `{c['primary_inputs']}`")
    if "satisfying_assignments" in c:
        a(f"- satisfying assignments of the deciding condition: "
          f"**{c['satisfying_assignments']}** of {c['input_space']}")
    if "unique_solution_int_msb_first" in c:
        a(f"- **the deciding condition has exactly one solution**: value "
          f"`{c['unique_solution_int_msb_first']}` over "
          f"{len(c['unique_solution_bits'])} bits")
    if c["bbox"]:
        b = c["bbox"]
        a(f"- bounding box: x {b['x0']:.1f}..{b['x1']:.1f}, "
          f"y {b['y0']:.1f}..{b['y1']:.1f}")
    a("")
    a("### Structure (decomposed through AND/OR/buffers)")
    a("")
    a("```")
    for line in rep["decomposition_tree"]:
        a(line)
    a("```")
    a("")
    a(f"Top-level operator: **{rep['top_level_operator']}**")
    a("")

    a("## 3a. Immediate operands of the deciding gate")
    a("")
    if rep.get("root_gate"):
        g = rep["root_gate"]
        a(f"Root gate **{g['instance']}** (`{g['cell']}`), operator "
          f"**{g['op']}**, so the deciding condition is that operator applied "
          f"to:")
        a("")
        a("| pin | net | op | cells | state bits | sat count | space |")
        a("|---|---|---|---|---|---|---|")
        for p in rep["immediate_operands"]:
            a(f"| {p['pin']}{'  (active low)' if p['active_low_pin'] else ''} "
              f"| `{p['net']}` | {p['root_op']} | {p['cone_cells']} "
              f"| {p['state_bits']} "
              f"| {p.get('satisfying_assignments', '-')} "
              f"| {p.get('input_space', '-')} |")
        a("")
        for p in rep["immediate_operands"]:
            if "unique_solution_int_msb_first" in p:
                a(f"- `{p['net']}` is satisfied by exactly one assignment: "
                  f"value **{p['unique_solution_int_msb_first']}**")
    else:
        a("_target is driven directly by a flop or primary input_")
    a("")

    a("## 3b. Dominant predicates (deep leaves)")
    a("")
    if not rep["predicates"]:
        a("_none: the target's logic did not decompose_")
    else:
        a("| net | root | op | cells | state bits | sat count | space | cmp? |")
        a("|---|---|---|---|---|---|---|---|")
        for p in rep["predicates"]:
            a(f"| `{p['net']}` | {p.get('root_cell') or '-'} | {p['root_op']} "
              f"| {p['cone_cells']} | {p['state_bits']} "
              f"| {p.get('satisfying_assignments', '-')} "
              f"| {p.get('input_space', '-')} "
              f"| {'yes' if p['matches_detected_comparator'] else ''} |")
        a("")
        for p in rep["predicates"]:
            if "unique_solution_int_msb_first" in p:
                a(f"- **`{p['net']}` is a comparison against a single "
                  f"constant**: value "
                  f"{p['unique_solution_int_msb_first']} over "
                  f"{p['boundary']} bits")
        for p in rep["predicates"]:
            if p["bbox"]:
                b = p["bbox"]
                a(f"- `{p['net']}` occupies x {b['x0']:.1f}..{b['x1']:.1f}, "
                  f"y {b['y0']:.1f}..{b['y1']:.1f}")
    a("")

    a("## 4. Simplified expression")
    a("")
    if rep["expression"]:
        a("```")
        a(rep["expression"])
        a("```")
    else:
        a("_cone too wide to express usefully; use the decomposition above_")
    a("")

    a("## 5. State bits the target depends on")
    a("")
    a("| flop | cell | Q | D | SCC | SCC size | seq dist | PIs in D cone "
      "| serial? | fanout | mux sel | x | y |")
    a("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rep["state_bits"][:80]:
        a(f"| {r['instance']} | {r['cell'].split('__')[-1]} | `{r['q_net']}` "
          f"| `{r['d_net']}` | {r['scc']} | {r['scc_size']} "
          f"| {r['sequential_distance']} | {r['primary_inputs_in_d_cone']} "
          f"| {'yes' if r['consumes_serial_input'] else ''} "
          f"| {r['q_fanout']} | {r['drives_mux_selects']} "
          f"| {r['x']} | {r['y']} |")
    if len(rep["state_bits"]) > 80:
        a(f"\n_{len(rep['state_bits']) - 80} more rows in the JSON_")
    a("")

    a("## 6. Candidate control / FSM bits")
    a("")
    ctl = rep["candidate_control_bits"]
    a(f"{len(ctl)} control-like of {len(rep['state_bits'])} relevant state "
      f"bits ({rep['datapath_bit_count']} look like datapath)")
    a("")
    if ctl:
        a("| flop | score | evidence | fanout | mux sel | x | y |")
        a("|---|---|---|---|---|---|---|")
        for r in ctl[:40]:
            a(f"| {r['instance']} | {r['control_score']} "
              f"| {'; '.join(r['control_evidence'])} | {r['q_fanout']} "
              f"| {r['drives_mux_selects']} | {r['x']} | {r['y']} |")
    a("")

    a("## 7. Transition slice")
    a("")
    a("```")
    for r in rep["transition_slice"][:60]:
        srcs = r["next_state_depends_on_flops"]
        pis = r["next_state_depends_on_inputs"]
        parts = []
        if srcs:
            parts.append(", ".join(srcs[:8]) + (" ..." if len(srcs) > 8 else ""))
        if pis:
            parts.append("inputs: " + ", ".join(pis))
        a(f"{r['flop']}_next <- {' | '.join(parts) if parts else '(constant)'}"
          f"{'   [self]' if r['self_dependent'] else ''}")
    a("```")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("netlist")
    ap.add_argument("--target", required=True)
    ap.add_argument("--outdir", default=None)
    a = ap.parse_args()

    data = json.load(open(a.netlist))
    rep = explain(data, a.target)

    base = os.path.splitext(os.path.basename(a.netlist))[0]
    outdir = a.outdir or os.path.dirname(os.path.abspath(a.netlist))
    md_path = os.path.join(outdir, f"{base}_target_{a.target}.md")
    js_path = os.path.join(outdir, f"{base}_target_{a.target}.json")
    md = to_markdown(rep)
    open(md_path, "w").write(md)
    json.dump(rep, open(js_path, "w"), indent=1)

    d = rep["driver"]
    print(f"target '{a.target}' is {d['kind']}, driven by {d['instance']} "
          f"({d['cell']})")
    if d["kind"] == "registered":
        print(f"  D net {d['d_net']}, clock {d['clock_net']} "
              f"({d['clock_edge']})")
        for k in ("reset", "set"):
            if f"{k}_net" in d:
                print(f"  {k} {d[f'{k}_net']} ({d[f'{k}_polarity']})")
    c = rep["cone"]
    print(f"  deciding cone: {c['cells']} cells, {c['state_bits']} state bits, "
          f"inputs {c['primary_inputs']}")
    if "satisfying_assignments" in c:
        print(f"  deciding condition satisfied by "
              f"{c['satisfying_assignments']} of {c['input_space']} assignments")
    if "unique_solution_int_msb_first" in c:
        print(f"  UNIQUE SOLUTION: value "
              f"{c['unique_solution_int_msb_first']}")
    print(f"  top-level operator: {rep['top_level_operator']}")
    if rep.get("root_gate"):
        g = rep["root_gate"]
        print(f"  root gate {g['instance']} ({g['op']}) over "
              f"{len(rep['immediate_operands'])} operands:")
        for p in rep["immediate_operands"]:
            extra = ""
            if "unique_solution_int_msb_first" in p:
                extra = (f"  <== unique solution "
                         f"{p['unique_solution_int_msb_first']}")
            print(f"    {p['pin']:6s} {p['net']:12s} cells={p['cone_cells']:4d} "
                  f"state={p['state_bits']:3d} "
                  f"sat={p.get('satisfying_assignments', '-')}{extra}")
    print(f"  predicates: {len(rep['predicates'])}")
    for p in rep["predicates"][:8]:
        extra = ""
        if "unique_solution_int_msb_first" in p:
            extra = (f"  <== CONSTANT COMPARISON, value "
                     f"{p['unique_solution_int_msb_first']}")
        print(f"    {p['net']:12s} {p['root_op']:10s} cells={p['cone_cells']:4d} "
              f"state={p['state_bits']:3d} "
              f"sat={p.get('satisfying_assignments', '-')}{extra}")
    print(f"  control-like state bits: {len(rep['candidate_control_bits'])}"
          f" of {len(rep['state_bits'])}")
    print(f"\nwrote {md_path}")
    print(f"wrote {js_path}")


if __name__ == "__main__":
    main()
