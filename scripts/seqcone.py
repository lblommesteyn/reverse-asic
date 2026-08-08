"""Sequential cone of influence: what can affect a signal, and how long it takes.

A purely combinational cone answers "what feeds this net right now", which is
the wrong question for a stateful puzzle circuit.  What matters is which state
bits and which primary inputs can ever affect the target, and after how many
clock edges.  That number is also the lower bound on the BMC depth: if the
furthest state bit is 24 edges from `success`, no input sequence shorter than
that can possibly assert it.
"""

import collections

from netgraph import build, comb_cone, flop_q_nets, seq_fanin


def analyze(circuit, target_net):
    drv, fanin = build(circuit)
    qnet = flop_q_nets(circuit)

    # level 0: combinational cone of the target
    c_insts, c_nets, boundary = comb_cone(circuit, drv, fanin, [target_net])
    level = {}
    frontier = set()
    direct_inputs = set()
    for b in boundary:
        if b in qnet:
            f = qnet[b]
            level.setdefault(f, 1)
            frontier.add(f)
        elif b not in circuit.constants:
            direct_inputs.add(b)

    one_step = set(frontier)
    inputs_at = collections.defaultdict(set)
    seen_inputs = set()
    for pi in direct_inputs:
        inputs_at[0].add(pi)
        seen_inputs.add(pi)

    # Precompute each flop's full fan-in boundary once.  Doing it inside the
    # BFS instead would recompute the same cones O(depth) times, which is the
    # difference between seconds and minutes on a large design.
    fboundary = {}
    for f in circuit.flops:
        b = set()
        for net in seq_fanin(circuit, f):
            b |= comb_cone(circuit, drv, fanin, [net])[2]
        fboundary[f] = b

    # walk backwards through flops, one clock edge per level
    d = 1
    all_flops = set(frontier)
    while frontier:
        nxt = set()
        for f in frontier:
            for b in fboundary[f]:
                if b in qnet:
                    g = qnet[b]
                    if g not in level:
                        level[g] = d + 1
                        nxt.add(g)
                        all_flops.add(g)
                elif b not in circuit.constants and b not in seen_inputs:
                    inputs_at[d].add(b)
                    seen_inputs.add(b)
        frontier = nxt
        d += 1
        if d > len(circuit.flops) + 2:
            break

    reachable_inputs = set()
    for v in inputs_at.values():
        reachable_inputs |= v

    unreachable_flops = [f for f in circuit.flops if f not in all_flops]
    unreachable_inputs = [n for n in circuit.inputs if n not in reachable_inputs]

    by_level = collections.defaultdict(list)
    for f, lv in level.items():
        by_level[lv].append(circuit.insts[f]["name"])

    # Distance-to-target undercounts how long a run must be: in a shift
    # register every bit feeds the comparator directly, so all of them sit at
    # distance 1, yet the register still takes its full length to fill.  The
    # useful depth hint is the longest path *through* the register graph from a
    # flop fed by a primary input, which is that fill time.
    reg_depth, reg_path = _longest_register_path(circuit, fboundary, qnet,
                                                 all_flops)

    return {
        "target": target_net,
        "combinational_cone_cells": len(c_insts),
        "combinational_boundary": sorted(boundary),
        "one_step_state_bits": sorted(
            circuit.insts[f]["name"] for f in one_step),
        "transitive_state_bits": len(all_flops),
        "total_state_bits": len(circuit.flops),
        "state_bits_outside_cone": len(unreachable_flops),
        "max_sequential_depth": max(level.values()) if level else 0,
        "register_graph_depth": reg_depth,
        "suggested_min_bmc_cycles": max(reg_depth,
                                        max(level.values()) if level else 0),
        "longest_register_path": [circuit.insts[f]["name"] for f in reg_path],
        "state_bits_by_sequential_distance": {
            str(k): {"count": len(v), "examples": sorted(v)[:8]}
            for k, v in sorted(by_level.items())},
        "inputs_that_can_influence": sorted(reachable_inputs),
        "inputs_that_cannot_influence": sorted(unreachable_inputs),
        "input_first_influence_distance": {
            k: sorted(v) for k, v in sorted(inputs_at.items())},
        "flops_outside_cone": [circuit.insts[f]["name"]
                               for f in unreachable_flops[:200]],
    }


def _longest_register_path(circuit, fboundary, qnet, cone_flops):
    """Longest path through the register graph, restricted to flops that can
    influence the target.  Self-loops are ignored, and cycles are broken by
    memoising on the recursion stack, so an LFSR reports its chain length
    rather than diverging.
    """
    pred = {f: set() for f in cone_flops}
    for f in cone_flops:
        for b in fboundary.get(f, ()):
            g = qnet.get(b)
            if g is not None and g != f and g in pred:
                pred[f].add(g)

    memo, onstack = {}, set()

    def depth(f):
        if f in memo:
            return memo[f]
        if f in onstack:
            return 0
        onstack.add(f)
        best, arg = 1, None
        for g in pred[f]:
            d = depth(g) + 1
            if d > best:
                best, arg = d, g
        onstack.discard(f)
        memo[f] = best
        return best

    best_f, best_d = None, 0
    for f in cone_flops:
        d = depth(f)
        if d > best_d:
            best_f, best_d = f, d

    path, cur = [], best_f
    seen = set()
    while cur is not None and cur not in seen:
        path.append(cur)
        seen.add(cur)
        nxt, nd = None, 0
        for g in pred[cur]:
            if g not in seen and depth(g) > nd:
                nxt, nd = g, depth(g)
        cur = nxt
    return best_d, list(reversed(path))
