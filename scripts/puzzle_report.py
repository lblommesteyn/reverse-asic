"""One command: recovered netlist -> derived structural report (Markdown + JSON).

Emits only information derived from the netlist -- counts, graph shapes,
detected blocks, bounding boxes.  Nothing here reproduces the layout file
itself, so the report is the artifact to read (and to share when asking for
help reasoning about the circuit).

Usage:
  python scripts/puzzle_report.py recovered.json --output success \
      --md report.md --json report.json
"""

import argparse
import collections
import json
import time

import blocks
import cells as cl
import seqcone
from netgraph import build, register_graph, sccs
from sim import Circuit


def gather(data, out_port=None, deep=True):
    c = Circuit(data)
    drv, fanin = build(c)
    t0 = time.time()

    fam = collections.Counter(
        blocks.family(r["cell"], m) for r, m in zip(c.insts, c.models))
    cell_types = collections.Counter(r["cell"] for r in c.insts)
    flop_types = collections.Counter(
        c.insts[i]["cell"] for i in c.flops)

    fanout = collections.Counter()
    for r in c.insts:
        m = cl.get_model(r["cell"])
        outs = set(m["outputs"]) if m else set()
        for pin, net in r["pins"].items():
            if pin not in outs:
                fanout[net] += 1

    if out_port is None:
        for cand in ("success", "S", "out", "valid", "done"):
            if cand in c.outputs:
                out_port = cand
                break
        else:
            out_port = c.outputs[0] if c.outputs else None

    rep = {
        "top": data.get("top"),
        "hashes": data.get("hashes", {}),
        "counts": {
            "cells": len(c.insts),
            "nets": len(c.nets),
            "flops": len(c.flops),
            "combinational": len(c.comb),
            "primary_inputs": len(c.inputs),
        },
        "cell_families": dict(fam.most_common()),
        "cell_types": dict(cell_types.most_common(40)),
        "flop_types": dict(flop_types.most_common()),
        "primary_inputs": c.inputs,
        "driven_outputs": c.outputs,
        "constants": sorted(c.constants),
        "high_fanout_nets": [{"net": n, "fanout": f}
                             for n, f in fanout.most_common(25)],
        "target_output": out_port,
    }

    edges, prim, dcone, qnet = register_graph(c, drv, fanin)
    rep["register_graph"] = {
        "edges": sum(len(v) for v in edges.values()),
        "primary_input_fanin": {k: len(v) for k, v in sorted(prim.items())},
    }

    comps = [s for s in sccs(edges, c.flops) if len(s) > 1]
    comps.sort(key=len, reverse=True)
    rep["feedback_sccs"] = [{
        "size": len(s),
        "flops": [c.insts[i]["name"] for i in s[:12]],
        "bbox": blocks._bbox(c, s),
    } for s in comps[:15]]

    rep["shift_chains"] = blocks.shift_chains(c, edges, dcone, qnet, prim)
    rep["counters"] = blocks.counters(c, edges, dcone, qnet)
    rep["mux_banks"] = blocks.mux_banks(c, drv, fanin)[:15]
    rep["mux_trees"] = blocks.mux_trees(c, drv, fanin)[:15]
    rep["decoders"] = blocks.decoders(c, drv, fanin)[:15]
    rep["carry_chains"] = blocks.carry_chains(c, drv, fanin)[:15]
    rep["structural_motifs"] = blocks.structural_motifs(c, drv, fanin)[:20]
    rep["wide_equality_candidates"] = blocks.wide_equality(c, drv, fanin,
                                                           qnet)[:15]
    if deep:
        rep["comparators"] = blocks.comparators(c, drv, fanin, qnet)[:25]
    else:
        rep["comparators"] = []

    if out_port:
        rep["sequential_cone"] = seqcone.analyze(c, out_port)
        ci = rep["sequential_cone"]
        drvmap, fanin2 = drv, fanin
        from netgraph import full_cone
        cone_insts, _ = full_cone(c, drvmap, fanin2, [out_port])
        outside = [i for i in range(len(c.insts)) if i not in cone_insts]
        rep["cone_cells"] = len(cone_insts)
        rep["cells_outside_cone"] = len(outside)
        rep["outside_cone_bbox"] = blocks._bbox(c, outside)
        rep["outside_cone_families"] = dict(collections.Counter(
            blocks.family(c.insts[i]["cell"], c.models[i])
            for i in outside).most_common())
        rep["outside_cone_clusters"] = blocks.spatial_clusters(c, outside)[:12]

    # likely control nets
    clk = collections.Counter()
    rstn = collections.Counter()
    for i in c.flops:
        r, s = c.insts[i], c.models[i]["seq"]
        if s.get("clk") in r["pins"]:
            clk[r["pins"][s["clk"]]] += 1
        for key in ("rst", "set"):
            if key in s and s[key][0] in r["pins"]:
                rstn[r["pins"][s[key][0]]] += 1
    rep["likely_clock_nets"] = [{"net": n, "flops": v} for n, v in clk.most_common(10)]
    rep["likely_reset_nets"] = [{"net": n, "flops": v} for n, v in rstn.most_common(10)]

    # serial input paths: primary inputs that reach only a few flops
    serial = []
    for pi, targets in sorted(prim.items()):
        if pi in c.constants:
            continue
        serial.append({"input": pi, "flops_fed": len(targets),
                       "flops": [c.insts[t]["name"] for t in sorted(targets)][:6]})
    serial.sort(key=lambda d: d["flops_fed"])
    rep["candidate_serial_inputs"] = [s for s in serial if s["flops_fed"] <= 4][:15]
    rep["broadcast_control_inputs"] = [s for s in serial if s["flops_fed"] > 4][:15]

    rep["elapsed_seconds"] = round(time.time() - t0, 1)
    return rep


def to_markdown(rep):
    L = []
    a = L.append
    a(f"# Structural report: `{rep['top']}`")
    a("")
    a(f"structural hash `{rep['hashes'].get('structural_sha256','?')[:16]}`  ")
    a(f"generated in {rep['elapsed_seconds']}s")
    a("")
    a("## Size")
    a("")
    a("| metric | value |")
    a("|---|---|")
    for k, v in rep["counts"].items():
        a(f"| {k} | {v} |")
    a("")
    a("## Cell families")
    a("")
    a("| family | count |")
    a("|---|---|")
    for k, v in rep["cell_families"].items():
        a(f"| {k} | {v} |")
    a("")
    a("## Interface")
    a("")
    a(f"- primary inputs: `{rep['primary_inputs']}`")
    a(f"- driven outputs: `{rep['driven_outputs']}`")
    a(f"- constants: `{rep['constants']}`")
    def netlist_line(rows):
        return ", ".join("`{}` ({} flops)".format(d["net"], d["flops"])
                         for d in rows[:5]) or "none"

    a(f"- likely clock nets: {netlist_line(rep['likely_clock_nets'])}")
    a(f"- likely reset nets: {netlist_line(rep['likely_reset_nets'])}")
    a("")
    a("### Candidate serial data inputs (feed few flops)")
    a("")
    for s in rep["candidate_serial_inputs"]:
        a(f"- `{s['input']}` -> {s['flops_fed']} flop(s) {s['flops']}")
    a("")
    a("### Broadcast/control inputs (feed many flops)")
    a("")
    for s in rep["broadcast_control_inputs"]:
        a(f"- `{s['input']}` -> {s['flops_fed']} flops")
    a("")

    if "sequential_cone" in rep:
        sc = rep["sequential_cone"]
        a(f"## Sequential cone for `{rep['target_output']}`")
        a("")
        a(f"- combinational cone: {sc['combinational_cone_cells']} cells")
        a(f"- state bits one edge away: {len(sc['one_step_state_bits'])}")
        a(f"- state bits reachable at all: {sc['transitive_state_bits']} "
          f"of {sc['total_state_bits']}")
        a(f"- **state bits outside the cone: {sc['state_bits_outside_cone']}**")
        a(f"- max sequential distance to target: {sc['max_sequential_depth']}")
        a(f"- register-graph depth (longest flop chain): "
          f"{sc['register_graph_depth']}")
        a(f"- **suggested minimum BMC cycles: "
          f"{sc['suggested_min_bmc_cycles']}**")
        a(f"- inputs that can influence it: `{sc['inputs_that_can_influence']}`")
        a(f"- inputs that cannot: `{sc['inputs_that_cannot_influence']}`")
        a("")
        a("| sequential distance | state bits | examples |")
        a("|---|---|---|")
        for k, v in sc["state_bits_by_sequential_distance"].items():
            a(f"| {k} | {v['count']} | {', '.join(v['examples'][:6])} |")
        a("")
        a(f"- cells in cone: {rep['cone_cells']}, "
          f"**outside: {rep['cells_outside_cone']}** "
          f"(the output-generator candidate)")
        if rep.get("outside_cone_bbox"):
            b = rep["outside_cone_bbox"]
            a(f"- outside-cone bounding box: "
              f"x {b['x0']:.1f}..{b['x1']:.1f}, y {b['y0']:.1f}..{b['y1']:.1f}")
        a("")

    def block_table(title, rows, cols):
        a(f"## {title}")
        a("")
        if not rows:
            a("_none detected_")
            a("")
            return
        a("| " + " | ".join(cols) + " |")
        a("|" + "---|" * len(cols))
        for r in rows:
            cells = []
            for cparam in cols:
                v = r.get(cparam)
                if isinstance(v, dict) and "x0" in v:
                    v = f"x {v['x0']:.1f}..{v['x1']:.1f} y {v['y0']:.1f}..{v['y1']:.1f}"
                elif isinstance(v, (list, dict)):
                    v = json.dumps(v)[:60]
                cells.append(str(v))
            a("| " + " | ".join(cells) + " |")
        a("")

    block_table("Shift-register chains", rep["shift_chains"][:20],
                ["length", "kind", "serial_inputs", "bbox"])
    block_table("Counters / self-dependent state groups", rep["counters"][:15],
                ["bits", "kind", "staircase", "xor_cells_in_cones", "bbox"])
    block_table("Feedback SCCs", rep["feedback_sccs"],
                ["size", "flops", "bbox"])
    block_table("Carry chains (adder/subtractor)", rep["carry_chains"],
                ["length", "xor_taps", "kind", "head", "bbox"])
    block_table("Constant / narrow comparators", rep["comparators"],
                ["kind", "net", "n_solutions", "selectivity", "state_bits",
                 "constant_int_msb_first", "bbox"])
    block_table("Wide equality candidates", rep["wide_equality_candidates"],
                ["net", "state_bits", "cone_cells", "bbox"])
    block_table("Mux banks (shared select = datapath width)", rep["mux_banks"],
                ["width", "select_net", "byte_aligned", "bbox"])
    block_table("Mux trees (cascaded)", rep["mux_trees"],
                ["muxes", "depth_estimate", "select_nets", "bbox"])
    block_table("Decoder-like fanout", rep["decoders"],
                ["outputs", "inputs", "bbox"])
    block_table("Repeated structural motifs", rep["structural_motifs"],
                ["repeats", "byte_aligned", "x_pitch", "cell_types", "bbox"])

    a("## High-fanout nets")
    a("")
    a("| net | fanout |")
    a("|---|---|")
    for d in rep["high_fanout_nets"][:15]:
        a(f"| `{d['net']}` | {d['fanout']} |")
    a("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("netlist")
    ap.add_argument("--output", default=None)
    ap.add_argument("--md", default=None)
    ap.add_argument("--json", default=None)
    ap.add_argument("--fast", action="store_true",
                    help="skip symbolic comparator detection")
    args = ap.parse_args()

    data = json.load(open(args.netlist))
    rep = gather(data, args.output, deep=not args.fast)

    md = to_markdown(rep)
    if args.md:
        open(args.md, "w").write(md)
        print(f"wrote {args.md}")
    else:
        print(md)
    if args.json:
        json.dump(rep, open(args.json, "w"), indent=1)
        print(f"wrote {args.json}")


if __name__ == "__main__":
    main()
