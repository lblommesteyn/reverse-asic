"""Functional block detection over a recovered netlist.

Combines three sources of evidence:

  * register-graph shape (chains, feedback, strongly connected components);
  * gate-level function, checked symbolically rather than guessed from cell
    names -- an equality comparator is identified by its logic having exactly
    one satisfying assignment, which also recovers the constant it compares to;
  * physical placement, because Jane Street states the layout hints at
    function, so repeated structural motifs are correlated with position and
    reported with their bounding boxes and pitch.
"""

import collections
import math

import z3

import cells as cl
from netgraph import (
    build, comb_cone, cone_function, count_models, flop_q_nets,
    register_graph, sccs,
)


def _bbox(circuit, idxs):
    xs = [circuit.insts[i]["x"] for i in idxs]
    ys = [circuit.insts[i]["y"] for i in idxs]
    if not xs:
        return None
    return {"x0": min(xs), "y0": min(ys), "x1": max(xs), "y1": max(ys),
            "n": len(idxs)}


def family(cell, model):
    if model and model["seq"]:
        return "flop"
    b = cl.base_name(cell)
    if b.startswith(("xnor", "xor")):
        return "xor"
    if b.startswith("mux"):
        return "mux"
    for pre in ("nand", "nor", "and", "or"):
        if b.startswith(pre):
            return pre
    if b.startswith(("buf", "inv", "clkbuf", "clkinv", "dly")):
        return "buf"
    if b.startswith(("fa", "ha", "maj")):
        return "adder"
    if b and b[0] in "ao" and any(ch.isdigit() for ch in b):
        return "compound"
    return "other"


# --------------------------------------------------------------- chains ----

def shift_chains(circuit, edges, dcone, qnet, prim):
    """Maximal flop chains, ignoring the self-loops that enable-held registers
    always create through their hold mux."""
    flops = circuit.flops
    succ = {f: {t for t in edges.get(f, ()) if t != f} for f in flops}
    indeg = collections.Counter()
    for f, ts in succ.items():
        for t in ts:
            indeg[t] += 1
    pred = collections.defaultdict(set)
    for f, ts in succ.items():
        for t in ts:
            pred[t].add(f)

    heads = [f for f in flops
             if indeg[f] != 1 or len(succ[next(iter(pred[f]))]) != 1]
    chains, visited = [], set()
    for f in heads:
        if f in visited:
            continue
        cur, chain = f, [f]
        visited.add(f)
        while len(succ[cur]) == 1:
            n = next(iter(succ[cur]))
            if indeg[n] != 1 or n in visited:
                break
            chain.append(n)
            visited.add(n)
            cur = n
        if len(chain) > 1:
            chains.append(chain)

    out = []
    for ch in chains:
        head_inputs = sorted(
            b for b in dcone.get(ch[0], {}).get("boundary", ())
            if b not in qnet and b not in circuit.constants)
        # feedback edges from inside the chain back into it => LFSR-like
        member = set(ch)
        back = []
        for i, f in enumerate(ch):
            for src in pred[f]:
                if src in member and ch.index(src) > i:
                    back.append((circuit.insts[src]["name"],
                                 circuit.insts[f]["name"]))
        out.append({
            "length": len(ch),
            "flops": [circuit.insts[i]["name"] for i in ch],
            "serial_inputs": head_inputs,
            "feedback_edges": back,
            "kind": "lfsr_like" if back else "shift_register",
            "bbox": _bbox(circuit, ch),
        })
    out.sort(key=lambda d: -d["length"])
    return out


# ------------------------------------------------------------ comparators ---

def comparators(circuit, drv, fanin, qnet, max_boundary=24, limit=64,
                min_state=3, min_cone=4):
    """Nets whose combinational function is a comparison.

    Model counting over the cone boundary distinguishes the cases:
      exactly 1 satisfying assignment  -> comparison against a constant
      exactly 2^(n/2)                  -> equality between two n/2-bit groups
    Both are the shapes a puzzle 'did you enter the magic value' check takes.
    """
    found = []
    ops = cl.Z3Ops()
    for net, i in sorted(drv.items()):
        if circuit.models[i]["seq"]:
            continue
        insts, _, boundary = comb_cone(circuit, drv, fanin, [net])
        if len(insts) < min_cone:
            continue
        free = [b for b in boundary if b not in circuit.constants]
        if not (2 <= len(free) <= max_boundary):
            continue
        state_bits = [b for b in free if b in qnet]
        # require the cone to be mostly state-driven and non-trivial, or every
        # two-input gate in the design shows up as a "comparator"
        if len(state_bits) < min_state or len(state_bits) < len(free) - 2:
            continue

        cache = {}

        def var_of(n, cache=cache):
            if n not in cache:
                cache[n] = z3.Bool(f"b_{n}")
            return cache[n]

        expr, bnd = cone_function(circuit, net, drv, fanin, ops, var_of)
        if expr is None or isinstance(expr, bool):
            continue
        variables = [var_of(b) for b in sorted(free)]
        n, models = count_models(expr, variables, limit=limit)
        if n == 0 or n >= limit:
            continue
        # a function satisfied by most of its input space is not a comparison
        if n > 2 ** max(0, len(variables) - 2):
            continue
        entry = {
            "net": net,
            "driver": circuit.insts[i]["name"],
            "cone_cells": len(insts),
            "boundary": sorted(free),
            "state_bits": len(state_bits),
            "n_solutions": n,
            "bbox": _bbox(circuit, insts),
        }
        entry["selectivity"] = f"{n}/2^{len(variables)}"
        if n == 1:
            m = models[0]
            bits = {b: int(m[var_of(b)]) for b in sorted(free)}
            entry["kind"] = "constant_comparator"
            entry["constant_bits"] = bits
            entry["constant_int_msb_first"] = _as_int(
                [bits[b] for b in sorted(free)])
        else:
            entry["kind"] = "narrow_comparator"
            entry["solutions"] = [
                {b: int(m[var_of(b)]) for b in sorted(free)}
                for m in models[:16]]
            ints = sorted(_as_int([m[var_of(b)] for b in sorted(free)])
                          for m in models)
            entry["solution_ints_msb_first"] = ints[:16]
        found.append(entry)
    found.sort(key=lambda d: -d["cone_cells"])
    return found


def _as_int(bits):
    v = 0
    for b in bits:
        v = (v << 1) | int(b)
    return v


def wide_equality(circuit, drv, fanin, qnet, min_state=6, max_boundary=26):
    """Wide AND/NOR convergence points fed by many state bits: the shape of an
    equality or magnitude comparison too wide to model-count cheaply."""
    out = []
    for net, i in sorted(drv.items()):
        m = circuit.models[i]
        if m["seq"]:
            continue
        if family(circuit.insts[i]["cell"], m) not in ("and", "nor", "nand",
                                                       "or", "compound"):
            continue
        insts, _, boundary = comb_cone(circuit, drv, fanin, [net])
        free = [b for b in boundary if b not in circuit.constants]
        state = [b for b in free if b in qnet]
        if len(state) < min_state or len(free) > max_boundary:
            continue
        fams = collections.Counter(
            family(circuit.insts[j]["cell"], circuit.models[j]) for j in insts)
        out.append({
            "net": net, "driver": circuit.insts[i]["name"],
            "cone_cells": len(insts), "state_bits": len(state),
            "boundary": len(free),
            "families": dict(fams),
            "bbox": _bbox(circuit, insts),
        })
    out.sort(key=lambda d: (-d["state_bits"], -d["cone_cells"]))
    return out


# ---------------------------------------------------------------- adders ----

CARRY_FAMS = {"compound", "and", "or", "nand", "nor", "adder", "buf"}


def carry_chains(circuit, drv, fanin, min_length=3, min_taps=2):
    """Deep chains of carry-style gates with XOR cells tapping them.

    This is what a synthesised adder actually looks like after technology
    mapping: the carry propagates through a ripple of AOI/OAI compound cells,
    and each sum bit hangs off that ripple as an XOR.  Looking for XOR feeding
    XOR directly finds nothing, because the carry path between two sum bits
    contains no XOR at all.
    """
    fam = {i: family(circuit.insts[i]["cell"], circuit.models[i])
           for i in range(len(circuit.insts))}
    carry_nodes = [i for i, f in fam.items() if f in CARRY_FAMS]
    cset = set(carry_nodes)

    pred = {i: [] for i in carry_nodes}
    for i in carry_nodes:
        for net in fanin[i]:
            j = drv.get(net)
            if j in cset:
                pred[i].append(j)

    depth, best_pred = {}, {}
    for i in _topo_order(carry_nodes, pred):
        d, bp = 1, None
        for j in pred[i]:
            if depth.get(j, 0) + 1 > d:
                d, bp = depth[j] + 1, j
        depth[i], best_pred[i] = d, bp

    # taps: xor cells consuming a net driven by a carry node
    taps = collections.defaultdict(list)
    for i, f in fam.items():
        if f != "xor":
            continue
        for net in fanin[i]:
            j = drv.get(net)
            if j in cset:
                taps[j].append(i)

    chains, used = [], set()
    for i in sorted(carry_nodes, key=lambda k: -depth.get(k, 0)):
        if i in used or depth.get(i, 0) < min_length:
            continue
        path, cur = [], i
        while cur is not None and cur not in used:
            path.append(cur)
            used.add(cur)
            cur = best_pred.get(cur)
        if len(path) < min_length:
            continue
        tapped = sorted({t for m in path for t in taps.get(m, ())})
        if len(tapped) < min_taps:
            continue
        chains.append({
            "length": len(path),
            "carry_cells": len(path),
            "xor_taps": len(tapped),
            "head": circuit.insts[path[0]]["name"],
            "kind": ("adder_or_subtractor_carry_chain"
                     if len(tapped) >= min_length else "carry_like_chain"),
            "bbox": _bbox(circuit, path + tapped),
        })
    chains.sort(key=lambda c: (-c["xor_taps"], -c["length"]))
    return chains


def _topo_order(nodes, pred):
    """Topological order of a combinational subgraph given predecessor lists."""
    succ = collections.defaultdict(list)
    indeg = {i: 0 for i in nodes}
    for i in nodes:
        for j in pred[i]:
            succ[j].append(i)
            indeg[i] += 1
    ready = [i for i in nodes if indeg[i] == 0]
    out = []
    while ready:
        i = ready.pop()
        out.append(i)
        for k in succ[i]:
            indeg[k] -= 1
            if indeg[k] == 0:
                ready.append(k)
    return out


# -------------------------------------------------------------- counters ----

def counters(circuit, edges, dcone, qnet):
    """Flop groups whose next state depends on themselves through an
    incrementer: each bit's D cone contains its own Q plus the lower bits."""
    groups = []
    self_dep = [f for f in circuit.flops
                if f in dcone
                and any(qnet.get(b) == f for b in dcone[f]["boundary"])]
    if not self_dep:
        return groups

    # cluster self-dependent flops that share cone state bits
    parent = {f: f for f in self_dep}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for f in self_dep:
        for b in dcone[f]["boundary"]:
            g = qnet.get(b)
            if g in parent:
                union(f, g)

    clusters = collections.defaultdict(list)
    for f in self_dep:
        clusters[find(f)].append(f)

    for members in clusters.values():
        if len(members) < 3:
            continue
        widths = sorted(
            len([b for b in dcone[f]["boundary"] if b in qnet])
            for f in members)
        # an incrementer has a staircase: bit k depends on ~k lower bits
        staircase = all(widths[i] <= widths[i + 1] for i in range(len(widths) - 1))
        xor_like = sum(
            1 for f in members
            for j in dcone[f]["insts"]
            if family(circuit.insts[j]["cell"], circuit.models[j]) == "xor")
        groups.append({
            "bits": len(members),
            "flops": [circuit.insts[f]["name"] for f in sorted(members)],
            "cone_state_widths": widths,
            "staircase": staircase,
            "xor_cells_in_cones": xor_like,
            "kind": "counter_like" if staircase and xor_like else "state_group",
            "bbox": _bbox(circuit, members),
        })
    groups.sort(key=lambda g: -g["bits"])
    return groups


# ----------------------------------------------------------- mux/decoders ---

def mux_banks(circuit, drv, fanin, min_width=2):
    """Muxes sharing a select net: a datapath-wide bank (hold/load, shift/load,
    or a 2:1 operand select), which is far more informative than reporting each
    isolated mux separately."""
    sel = collections.defaultdict(list)
    for i in range(len(circuit.insts)):
        if family(circuit.insts[i]["cell"], circuit.models[i]) != "mux":
            continue
        r = circuit.insts[i]
        for pin in ("S", "S0", "S1"):
            if pin in r["pins"]:
                sel[r["pins"][pin]].append(i)
    out = []
    for net, members in sel.items():
        if len(members) < min_width:
            continue
        out.append({
            "select_net": net,
            "width": len(members),
            "byte_aligned": len(members) % 8 == 0,
            "bbox": _bbox(circuit, members),
        })
    out.sort(key=lambda d: -d["width"])
    return out


def mux_trees(circuit, drv, fanin, min_size=2):
    """Connected clusters of mux cells, and the selects that drive them."""
    mux = [i for i in range(len(circuit.insts))
           if family(circuit.insts[i]["cell"], circuit.models[i]) == "mux"]
    if not mux:
        return []
    muxset = set(mux)
    parent = {i: i for i in mux}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for i in mux:
        for net in fanin[i]:
            j = drv.get(net)
            if j in muxset:
                ra, rb = find(i), find(j)
                if ra != rb:
                    parent[rb] = ra

    clusters = collections.defaultdict(list)
    for i in mux:
        clusters[find(i)].append(i)

    out = []
    for members in clusters.values():
        if len(members) < min_size:
            continue
        sels = collections.Counter()
        for i in members:
            r, m = circuit.insts[i], circuit.models[i]
            for pin in ("S", "S0", "S1"):
                if pin in r["pins"]:
                    sels[r["pins"][pin]] += 1
        out.append({
            "muxes": len(members),
            "select_nets": [{"net": n, "muxes": c} for n, c in sels.most_common(6)],
            "depth_estimate": int(math.ceil(math.log2(len(members) + 1))),
            "bbox": _bbox(circuit, members),
        })
    out.sort(key=lambda d: -d["muxes"])
    return out


def decoders(circuit, drv, fanin, min_fanout=4):
    """Small input set fanning out to many one-hot-looking AND/NOR outputs."""
    by_boundary = collections.defaultdict(list)
    for net, i in sorted(drv.items()):
        m = circuit.models[i]
        if m["seq"]:
            continue
        if family(circuit.insts[i]["cell"], m) not in ("and", "nand", "nor",
                                                       "or", "compound"):
            continue
        insts, _, boundary = comb_cone(circuit, drv, fanin, [net])
        free = frozenset(b for b in boundary if b not in circuit.constants)
        if 2 <= len(free) <= 6:
            by_boundary[free].append((net, i))
    out = []
    for boundary, members in by_boundary.items():
        if len(members) < min_fanout:
            continue
        out.append({
            "inputs": sorted(boundary),
            "outputs": len(members),
            "output_nets": [n for n, _ in members[:12]],
            "kind": "decoder_like",
            "bbox": _bbox(circuit, [i for _, i in members]),
        })
    out.sort(key=lambda d: -d["outputs"])
    return out


# --------------------------------------------------------- repeated motifs --

def structural_motifs(circuit, drv, fanin, radius=2, min_repeats=3):
    """Hash each cell's local neighbourhood and report motifs that repeat.

    Byte-wide datapaths and per-character blocks show up here as a motif with a
    repeat count that is a multiple of 8, and the spatial pitch between repeats
    tells you how the datapath is laid out.
    """
    sig = {}
    for i in range(len(circuit.insts)):
        sig[i] = family(circuit.insts[i]["cell"], circuit.models[i])
    for _ in range(radius):
        new = {}
        for i in range(len(circuit.insts)):
            ins = sorted(sig.get(drv[n], "PI") if n in drv else "PI"
                         for n in fanin[i])
            new[i] = f"{sig[i]}({','.join(ins)})"
        sig = {k: str(hash(v) & 0xFFFFFFFF) for k, v in new.items()}

    groups = collections.defaultdict(list)
    for i, s in sig.items():
        groups[s].append(i)

    out = []
    for s, members in groups.items():
        if len(members) < min_repeats:
            continue
        xs = sorted(circuit.insts[i]["x"] for i in members)
        ys = sorted(circuit.insts[i]["y"] for i in members)
        dx = [round(b - a, 3) for a, b in zip(xs, xs[1:]) if b - a > 1e-6]
        pitch = collections.Counter(dx).most_common(1)
        out.append({
            "repeats": len(members),
            "example_cells": [circuit.insts[i]["name"] for i in members[:6]],
            "cell_types": dict(collections.Counter(
                circuit.insts[i]["cell"] for i in members).most_common(3)),
            "x_pitch": pitch[0][0] if pitch else None,
            "byte_aligned": len(members) % 8 == 0,
            "bbox": _bbox(circuit, members),
        })
    out.sort(key=lambda d: -d["repeats"])
    return out


def spatial_clusters(circuit, idxs, gap=6.0):
    """Single-linkage clustering on placement, for reporting block extents."""
    pts = sorted(idxs, key=lambda i: (circuit.insts[i]["x"],
                                      circuit.insts[i]["y"]))
    clusters, cur = [], []
    last = None
    for i in pts:
        x = circuit.insts[i]["x"]
        if last is not None and x - last > gap:
            clusters.append(cur)
            cur = []
        cur.append(i)
        last = x
    if cur:
        clusters.append(cur)
    return [{"bbox": _bbox(circuit, c),
             "cells": len(c)} for c in clusters if c]
