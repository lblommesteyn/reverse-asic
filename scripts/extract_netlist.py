"""GDS -> gate-level netlist JSON, with extraction diagnostics.

Deterministic: instances are sorted by physical position and cells/nets are
named from that canonical order, so two runs over the same GDS produce
byte-identical JSON and comparable hashes.

Power handling matters for correctness on real designs: a signal pin that lands
on the VPWR or VGND rail is a tie-off, not a primary input, so power roots are
identified explicitly and such pins are named VPWR/VGND and treated downstream
as constants.

Usage:  python scripts/extract_netlist.py <in.gds> <out_prefix>
"""

import collections
import hashlib
import json
import sys

import klayout.db as db

import cells as cl
from gdslib import (
    build_connectivity,
    classify_leaf,
    collect_texts,
    ELECTRICAL_NAMES,
    has_subcells,
    is_functional,
    load,
    POWER_PINS,
)

SCHEMA = 2


def _canon_json(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def extract(gds_path, prefix, verbose=True):
    def say(*a):
        if verbose:
            print(*a)

    say(f"reading {gds_path}")
    layout, top = load(gds_path)
    say(f"top cell: {top.name}  dbu={layout.dbu}")

    say("building connectivity...")
    indexes, uf, conn_diag = build_connectivity(layout, top, verbose=verbose)

    # --- instances, in canonical order -----------------------------------
    # Walk the hierarchy rather than only the top level.  A design may wrap
    # groups of standard cells in intermediate cells; treating those as opaque
    # leaves would lose every gate inside them.  Recursion stops at anything
    # that has a logical model or has no sub-instances of its own.
    raw = []
    hier = {"containers": collections.Counter(), "max_depth": 0}
    physical_only = {}
    leaf_kind = {}

    def kind_of(child):
        if child.name in leaf_kind:
            return leaf_kind[child.name]
        if cl.get_model(child.name) is not None:
            k, layers = "modelled", None
        else:
            k, layers = classify_leaf(layout, child)
        leaf_kind[child.name] = (k, layers)
        return leaf_kind[child.name]

    def walk(cell, trans, depth):
        hier["max_depth"] = max(hier["max_depth"], depth)
        for inst in cell.each_inst():
            child = layout.cell(inst.cell_index)
            if not is_functional(child.name):
                continue
            n_copies = inst.cell_inst.size()
            if cl.get_model(child.name) is None and has_subcells(child):
                hier["containers"][child.name] += n_copies
                for t in inst.cell_inst.each_cplx_trans():
                    walk(child, trans * t, depth + 1)
                continue
            k, layers = kind_of(child)
            if k == "physical_only":
                rec = physical_only.setdefault(
                    child.name, {"count": 0, "layers": {}})
                rec["count"] += n_copies
                rec["layers"] = {f"{l}/{d}": n for (l, d), n in
                                 (layers or {}).items()}
                continue
            for t in inst.cell_inst.each_cplx_trans():
                full = trans * t
                d = full.disp
                raw.append((d.x, d.y, child.name, full, child))

    walk(top, db.ICplxTrans(), 0)
    raw.sort(key=lambda r: (r[0], r[1], r[2], str(r[3])))
    insts = [(r[4], r[3]) for r in raw]
    say(f"functional instances: {len(insts)}")
    if physical_only:
        say("physical / annotation leaf cells (ignored, carry no signal):")
        for k, v in sorted(physical_only.items()):
            say(f"   {v['count']:6d}  {k}  layers "
                f"{sorted(v['layers'])}")
    if hier["containers"]:
        say(f"descended through {sum(hier['containers'].values())} "
            f"hierarchical cells (depth {hier['max_depth']}):")
        for k, v in hier["containers"].most_common(10):
            say(f"   {v:6d}  {k}")

    counts = collections.Counter(c.name for c, _ in insts)
    for k, v in counts.most_common():
        say(f"  {v:5d}  {k}")

    # --- unsupported cells, reported up front ----------------------------
    unsupported = {}
    refused = {}
    for name in sorted(counts):
        r = cl.unsupported_reason(name)
        if r:
            refused[name] = {"count": counts[name], "reason": r}
        if cl.get_model(name) is None:
            cell = layout.cell(layout.cell_by_name(name))
            pins = sorted({sh.text.string
                           for li in _text_layer_indexes(layout)
                           for sh in cell.shapes(li).each()
                           if sh.is_text() and sh.text.string not in POWER_PINS})
            unsupported[name] = {"count": counts[name], "pins": pins}
    if unsupported:
        say("\n!! UNSUPPORTED CELLS (add models to cells.py before simulating):")
        for k, v in unsupported.items():
            say(f"   {v['count']:6d}  {k}  pins {v['pins']}")

    # --- power roots ------------------------------------------------------
    power_roots = {}
    for text, pt, conductor in collect_texts(top, layout):
        if text in POWER_PINS:
            node = indexes[conductor].lookup(pt)
            if node is not None:
                power_roots[uf.find((conductor, node))] = _power_name(text)
    # also from each cell's own power pins, which is how tie-offs are found
    for cell, trans in insts:
        for text, pt, conductor in collect_texts(cell, layout):
            if text not in POWER_PINS:
                continue
            node = indexes[conductor].lookup(trans * pt)
            if node is not None:
                power_roots.setdefault(uf.find((conductor, node)),
                                       _power_name(text))
    say(f"power roots identified: {len(power_roots)}")

    # --- attach instance pins to nets ------------------------------------
    pin_nodes = []
    unresolved_pins = []
    for idx, (cell, trans) in enumerate(insts):
        seen = set()
        for text, pt, conductor in collect_texts(cell, layout):
            if text in POWER_PINS:
                continue
            key = (text, pt.x, pt.y)
            if key in seen:
                continue
            seen.add(key)
            node = indexes[conductor].lookup(trans * pt)
            if node is None:
                unresolved_pins.append(
                    {"inst": idx, "cell": cell.name, "pin": text,
                     "layer": conductor})
                continue
            pin_nodes.append((idx, text, uf.find((conductor, node))))
    say(f"pin label lookups unresolved: {len(unresolved_pins)}")

    # A pin usually carries several labels, all on the same internal shape.  If
    # they resolve to DIFFERENT nets the cell's pin is internally open, or the
    # router only reached one of them -- either way the pin's net would
    # otherwise be decided by whichever label happened to be visited last, which
    # is a silent, order-dependent wrong answer.
    by_pin = collections.defaultdict(set)
    for idx, pin, root in pin_nodes:
        by_pin[(idx, pin)].add(root)
    label_disagreements = [
        {"inst": f"U{idx}", "cell": insts[idx][0].name, "pin": pin,
         "distinct_nets": len(roots)}
        for (idx, pin), roots in sorted(by_pin.items()) if len(roots) > 1]
    if label_disagreements:
        say(f"!! {len(label_disagreements)} pins whose labels resolve to "
            f"different nets")

    # --- top-level ports ---------------------------------------------------
    ports = {}
    port_problems = []
    for text, pt, conductor in collect_texts(top, layout):
        if text in POWER_PINS:
            continue
        node = indexes[conductor].lookup(pt)
        if node is None:
            port_problems.append({"port": text, "layer": conductor,
                                  "reason": "label not on conductor"})
            continue
        ports.setdefault(text, set()).add(uf.find((conductor, node)))
    for p, roots in ports.items():
        if len(roots) > 1:
            port_problems.append(
                {"port": p, "reason": f"label appears on {len(roots)} "
                                      f"distinct nets (possible open)"})
    say(f"ports: {sorted(ports)}")

    # --- name the nets ----------------------------------------------------
    root_name = dict(power_roots)
    for pname in sorted(ports):
        for r in sorted(ports[pname], key=repr):
            root_name.setdefault(r, pname)

    counter = 0
    for _, _, r in pin_nodes:
        if r not in root_name:
            root_name[r] = f"n{counter}"
            counter += 1

    # --- assemble ---------------------------------------------------------
    inst_records = []
    for idx, (cell, trans) in enumerate(insts):
        d = trans.disp
        inst_records.append({
            "name": f"U{idx}",
            "cell": cell.name,
            "x": round(d.x * layout.dbu, 4),
            "y": round(d.y * layout.dbu, 4),
            "orient": f"{trans.rot()}{'M' if trans.is_mirror() else ''}",
            "pins": {},
        })
    for idx, pin, root in pin_nodes:
        inst_records[idx]["pins"][pin] = root_name[root]
    for r in inst_records:
        r["pins"] = dict(sorted(r["pins"].items()))

    nets = collections.defaultdict(list)
    for r in inst_records:
        for pin, net in r["pins"].items():
            nets[net].append(f"{r['name']}.{pin}")

    # --- pin coverage against the cell library ----------------------------
    floating = []
    for r in inst_records:
        m = cl.get_model(r["cell"])
        if m is None:
            continue
        expected = set(m["inputs"]) | set(m["outputs"])
        missing = expected - set(r["pins"])
        if missing:
            floating.append({"inst": r["name"], "cell": r["cell"],
                             "missing": sorted(missing),
                             "x": r["x"], "y": r["y"]})

    diagnostics = {
        "connectivity": conn_diag,
        "unresolved_pins": unresolved_pins[:200],
        "unresolved_pin_count": len(unresolved_pins),
        "port_problems": port_problems,
        "unsupported_cells": unsupported,
        "refused_cells": refused,
        "pins_missing_from_layout": floating[:200],
        "pins_missing_count": len(floating),
        "power_root_count": len(power_roots),
        "physical_only_cells": physical_only,
        "hierarchy_containers": dict(hier["containers"]),
        "hierarchy_max_depth": hier["max_depth"],
        "pin_label_disagreements": label_disagreements[:100],
        "pin_label_disagreement_count": len(label_disagreements),
    }

    data = {
        "schema": SCHEMA,
        "top": top.name,
        "dbu": layout.dbu,
        "constants": {"VPWR": 1, "VGND": 0},
        "instances": inst_records,
        "ports": sorted(ports),
        "nets": {k: sorted(v) for k, v in sorted(nets.items())},
        "diagnostics": diagnostics,
    }

    structural = _canon_json({
        "instances": [{"cell": r["cell"], "pins": r["pins"]}
                      for r in inst_records],
        "ports": data["ports"],
    })
    data["hashes"] = {
        "structural_sha256": hashlib.sha256(structural.encode()).hexdigest(),
        "full_sha256": hashlib.sha256(
            _canon_json({k: v for k, v in data.items()
                         if k != "diagnostics"}).encode()).hexdigest(),
    }

    with open(f"{prefix}.json", "w") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    say(f"wrote {prefix}.json  ({len(inst_records)} cells, {len(nets)} nets)")
    say(f"structural hash: {data['hashes']['structural_sha256'][:16]}")
    return data


def _power_name(text):
    return "VGND" if text in ("VGND", "VNB", "VSS") else "VPWR"


def _text_layer_indexes(layout):
    from gdslib import TEXT_LAYERS
    return [layout.layer(l, d) for l, d in TEXT_LAYERS.values()]


if __name__ == "__main__":
    extract(sys.argv[1], sys.argv[2])
