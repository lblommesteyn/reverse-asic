"""Graph utilities shared by the analysis, block-detection and reduction passes.

Three views of the same netlist:

  * the gate graph, used for combinational cones;
  * the register graph, where combinational logic is collapsed so only flop ->
    flop dependencies remain, which is what exposes shift chains, counters and
    feedback;
  * the sequential cone, which follows flop D pins backwards through time.
"""

import collections

import z3

import cells as cl


def build(circuit):
    """net -> driving instance index, and instance index -> input nets."""
    drv = {net: i for net, (i, _) in circuit.driver.items()}
    fanin = {}
    for i, (r, m) in enumerate(zip(circuit.insts, circuit.models)):
        fanin[i] = [r["pins"][p] for p in m["inputs"] if p in r["pins"]]
    return drv, fanin


def comb_cone(circuit, drv, fanin, start_nets):
    """Backward cone stopping at flop outputs, constants and primary inputs.

    Returns (instances, nets, boundary_nets)."""
    seen_inst, seen_net, boundary = set(), set(), set()
    stack = list(start_nets)
    while stack:
        net = stack.pop()
        if net in seen_net:
            continue
        seen_net.add(net)
        i = drv.get(net)
        if i is None:
            boundary.add(net)
            continue
        if circuit.models[i]["seq"]:
            boundary.add(net)
            continue
        seen_inst.add(i)
        stack.extend(fanin[i])
    return seen_inst, seen_net, boundary


def full_cone(circuit, drv, fanin, start_nets):
    """Backward cone crossing flops through D/CLK/reset: everything that can
    ever influence the start nets."""
    seen_inst, seen_net = set(), set()
    stack = list(start_nets)
    while stack:
        net = stack.pop()
        if net in seen_net:
            continue
        seen_net.add(net)
        i = drv.get(net)
        if i is None or i in seen_inst:
            continue
        seen_inst.add(i)
        stack.extend(seq_fanin(circuit, i) if circuit.models[i]["seq"]
                     else fanin[i])
    return seen_inst, seen_net


def seq_fanin(circuit, i):
    """All nets feeding a sequential cell, including control pins."""
    m, r = circuit.models[i], circuit.insts[i]
    s = m["seq"]
    nets = []
    for key in ("d", "clk", "en"):
        if s.get(key) in r["pins"]:
            nets.append(r["pins"][s[key]])
    for key in ("rst", "set"):
        if key in s and s[key][0] in r["pins"]:
            nets.append(r["pins"][s[key][0]])
    if "scan" in s:
        nets.extend(r["pins"][p] for p in s["scan"] if p in r["pins"])
    return nets


def flop_q_nets(circuit):
    """net -> flop index, for both Q and Q_N outputs."""
    q = {}
    for i in circuit.flops:
        s, r = circuit.models[i]["seq"], circuit.insts[i]
        for key in ("q", "qn"):
            if s.get(key) in r["pins"]:
                q[r["pins"][s[key]]] = i
    return q


def register_graph(circuit, drv, fanin):
    """Edges f -> g when flop f's output reaches flop g's D combinationally.

    Also returns, per flop, the primary inputs reaching its D cone and the cone
    itself, since the detectors all need those.
    """
    qnet = flop_q_nets(circuit)
    edges = collections.defaultdict(set)
    prim = collections.defaultdict(set)
    dcone = {}
    for g in circuit.flops:
        s, r = circuit.models[g]["seq"], circuit.insts[g]
        dnet = r["pins"].get(s["d"])
        if dnet is None:
            continue
        insts, nets, boundary = comb_cone(circuit, drv, fanin, [dnet])
        dcone[g] = {"insts": insts, "nets": nets, "boundary": boundary}
        for b in boundary:
            if b in qnet:
                edges[qnet[b]].add(g)
            else:
                prim[b].add(g)
    return edges, prim, dcone, qnet


def sccs(edges, nodes):
    """Tarjan strongly connected components, iterative."""
    index = {}
    low = {}
    on = set()
    stack = []
    out = []
    counter = [0]

    for root in nodes:
        if root in index:
            continue
        work = [(root, iter(sorted(edges.get(root, ()))))]
        index[root] = low[root] = counter[0]
        counter[0] += 1
        stack.append(root)
        on.add(root)
        while work:
            v, it = work[-1]
            advanced = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter[0]
                    counter[0] += 1
                    stack.append(w)
                    on.add(w)
                    work.append((w, iter(sorted(edges.get(w, ())))))
                    advanced = True
                    break
                if w in on:
                    low[v] = min(low[v], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                out.append(sorted(comp))
    return out


def cone_function(circuit, net, drv, fanin, ops, var_of):
    """Build a symbolic expression for `net` over its cone boundary.

    `var_of` maps a boundary net name to a symbolic variable.
    """
    insts, _, boundary = comb_cone(circuit, drv, fanin, [net])
    values = {}
    for b in boundary:
        if b in circuit.constants:
            values[b] = circuit.constants[b]
        else:
            values[b] = var_of(b)
    order = [i for i in circuit.order if i in insts]
    for i in order:
        r, m = circuit.insts[i], circuit.models[i]
        p = {}
        for pin in m["inputs"]:
            n = r["pins"].get(pin)
            p[pin] = values.get(n, False) if n is not None else False
        res = m["fn"](ops, p)
        if m.get("multi"):
            for out, val in res.items():
                if out in r["pins"]:
                    values[r["pins"][out]] = val
        else:
            o = m["outputs"][0]
            if o in r["pins"]:
                values[r["pins"][o]] = val = res
    return values.get(net), sorted(boundary)


def count_models(expr, variables, limit=8):
    """Count satisfying assignments of expr over `variables`, up to `limit`.

    Returns (count, models).  A count of exactly 1 means the expression is a
    comparison against a single constant, and the model *is* that constant.
    """
    s = z3.Solver()
    s.add(expr == True)
    models = []
    while len(models) < limit:
        if s.check() != z3.sat:
            break
        m = s.model()
        assign = {v: z3.is_true(m.eval(v, model_completion=True))
                  for v in variables}
        models.append(assign)
        s.add(z3.Or([v != z3.BoolVal(assign[v]) for v in variables]))
    return len(models), models
