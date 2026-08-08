"""SVG views of a recovered netlist.

  floorplan     every cell at its die coordinates, coloured by function
  cone          same, with cells outside the target's cone greyed out
  blocks        detected block bounding boxes drawn over the floorplan
  registers     the register dependency graph, laid out by physical position

Logical edges are drawn only for a selected block, because a full edge overlay
on a real design is unreadable.

Usage:
  python scripts/visualize.py recovered.json outdir --output success
"""

import argparse
import collections
import json
import os

import blocks
from netgraph import build, full_cone, register_graph
from sim import Circuit

COLOURS = {
    "flop": "#e6194b", "xor": "#4363d8", "mux": "#3cb44b",
    "and": "#f58231", "or": "#911eb4", "nand": "#f032e6",
    "nor": "#008080", "compound": "#9a6324", "buf": "#808000",
    "adder": "#46f0f0", "other": "#000075",
}
CSS = """
<style>
 text { font-family: ui-monospace, monospace; }
 .lbl { font-size: 11px; fill: #222; }
 .ttl { font-size: 15px; fill: #000; }
 @media (prefers-color-scheme: dark) {
   .bg { fill: #14161a; } .lbl { fill: #ddd; } .ttl { fill: #fff; }
 }
</style>
"""


class Canvas:
    def __init__(self, circuit, width=1600, legend_h=140):
        xs = [r["x"] for r in circuit.insts]
        ys = [r["y"] for r in circuit.insts]
        self.x0, self.x1 = min(xs), max(xs)
        self.y0, self.y1 = min(ys), max(ys)
        self.pad = 34
        span_x = max(self.x1 - self.x0, 1e-6)
        span_y = max(self.y1 - self.y0, 1e-6)
        self.scale = (width - 2 * self.pad) / span_x
        self.width = width
        self.height = int(span_y * self.scale) + 2 * self.pad + legend_h
        self.legend_y = self.height - legend_h + 20
        self.parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
            f'height="{self.height}" viewBox="0 0 {width} {self.height}">',
            CSS,
            f'<rect class="bg" width="{width}" height="{self.height}" '
            f'fill="#ffffff"/>',
        ]

    def sx(self, x):
        return self.pad + (x - self.x0) * self.scale

    def yflip(self, y):
        """Die coordinates increase upwards; SVG y increases downwards."""
        bottom = self.legend_y - 24
        return bottom - (y - self.y0) * self.scale

    def add(self, s):
        self.parts.append(s)

    def title(self, text):
        self.add(f'<text class="ttl" x="{self.pad}" y="20">{text}</text>')

    def legend(self, items):
        cx, cy = self.pad, self.legend_y
        for label, colour in items:
            self.add(f'<rect x="{cx}" y="{cy}" width="12" height="12" '
                     f'fill="{colour}"/>')
            self.add(f'<text class="lbl" x="{cx + 16}" y="{cy + 11}">'
                     f'{label}</text>')
            cx += 34 + 7.2 * len(label)
            if cx > self.width - 220:
                cx, cy = self.pad, cy + 18
        return self

    def save(self, path):
        self.parts.append("</svg>")
        open(path, "w").write("\n".join(self.parts))
        print(f"wrote {path}")


def _cell_boxes(cv, circuit, colour_of, size=None):
    box = size or max(2.0, cv.scale * 1.1)
    for i, r in enumerate(circuit.insts):
        fill, op = colour_of(i)
        cv.add(f'<rect x="{cv.sx(r["x"]):.1f}" y="{cv.yflip(r["y"]):.1f}" '
               f'width="{box:.1f}" height="{box:.1f}" fill="{fill}" '
               f'opacity="{op}"><title>{r["name"]} {r["cell"]} '
               f'({r["x"]}, {r["y"]})</title></rect>')


def floorplan(circuit, path, greyed=None, title="floorplan"):
    greyed = greyed or set()
    cv = Canvas(circuit)
    counts = collections.Counter()

    def colour_of(i):
        fam = blocks.family(circuit.insts[i]["cell"], circuit.models[i])
        counts[fam] += 1
        if i in greyed:
            return "#b9b9b9", 0.45
        return COLOURS.get(fam, "#000075"), 0.9

    _cell_boxes(cv, circuit, colour_of)
    cv.title(f"{title}: {len(circuit.insts)} cells, "
             f"{len(circuit.flops)} flops"
             + (f", {len(greyed)} greyed (outside cone)" if greyed else ""))
    cv.legend([(f"{k} {v}", COLOURS.get(k, "#000075"))
               for k, v in counts.most_common()])
    cv.save(path)


def block_map(circuit, rep, path):
    """Floorplan with detected block bounding boxes drawn on top."""
    cv = Canvas(circuit)

    def colour_of(i):
        fam = blocks.family(circuit.insts[i]["cell"], circuit.models[i])
        return COLOURS.get(fam, "#000075"), 0.30

    _cell_boxes(cv, circuit, colour_of)

    layers = [
        ("shift_chains", "#e6194b", lambda d: f"shift x{d['length']}"),
        ("counters", "#f58231", lambda d: f"state x{d['bits']}"),
        ("carry_chains", "#4363d8", lambda d: f"carry x{d['length']}"),
        ("comparators", "#008080", lambda d: f"cmp {d['n_solutions']}sol"),
        ("mux_banks", "#3cb44b", lambda d: f"mux x{d['width']}"),
    ]
    legend = []
    for key, colour, label in layers:
        rows = rep.get(key) or []
        if rows:
            legend.append((key, colour))
        for d in rows[:12]:
            b = d.get("bbox")
            if not b:
                continue
            x, y = cv.sx(b["x0"]), cv.yflip(b["y1"])
            w = max(4.0, (b["x1"] - b["x0"]) * cv.scale)
            h = max(4.0, (b["y1"] - b["y0"]) * cv.scale)
            cv.add(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" '
                   f'height="{h:.1f}" fill="none" stroke="{colour}" '
                   f'stroke-width="1.6" opacity="0.95"><title>{key}: '
                   f'{label(d)}</title></rect>')
            cv.add(f'<text class="lbl" x="{x:.1f}" y="{y - 3:.1f}" '
                   f'fill="{colour}">{label(d)}</text>')
    cv.title("detected functional blocks")
    cv.legend(legend)
    cv.save(path)


def register_map(circuit, path, target=None, max_edges=1200):
    """Register dependency graph drawn at physical positions."""
    drv, fanin = build(circuit)
    edges, prim, dcone, qnet = register_graph(circuit, drv, fanin)
    cv = Canvas(circuit)

    in_cone = None
    if target:
        cone, _ = full_cone(circuit, drv, fanin, [target])
        in_cone = cone

    drawn = 0
    for a, targets in edges.items():
        for b in targets:
            if a == b or drawn >= max_edges:
                continue
            ra, rb = circuit.insts[a], circuit.insts[b]
            colour = "#4363d8"
            if in_cone is not None and (a not in in_cone or b not in in_cone):
                colour = "#c8c8c8"
            cv.add(f'<line x1="{cv.sx(ra["x"]):.1f}" y1="{cv.yflip(ra["y"]):.1f}" '
                   f'x2="{cv.sx(rb["x"]):.1f}" y2="{cv.yflip(rb["y"]):.1f}" '
                   f'stroke="{colour}" stroke-width="0.7" opacity="0.55"/>')
            drawn += 1

    for i in circuit.flops:
        r = circuit.insts[i]
        fill = "#e6194b"
        if in_cone is not None and i not in in_cone:
            fill = "#b9b9b9"
        cv.add(f'<circle cx="{cv.sx(r["x"]):.1f}" cy="{cv.yflip(r["y"]):.1f}" '
               f'r="3" fill="{fill}"><title>{r["name"]}</title></circle>')

    cv.title(f"register graph: {len(circuit.flops)} flops, {drawn} edges drawn"
             + (f" (grey = outside {target} cone)" if target else ""))
    cv.legend([("flop in cone", "#e6194b"), ("flop outside", "#b9b9b9"),
               ("Q -> D edge", "#4363d8")])
    cv.save(path)


def block_detail(circuit, path, cell_names, title="block detail"):
    """Floorplan restricted to one block, with its logical edges drawn."""
    idx = {r["name"]: i for i, r in enumerate(circuit.insts)}
    members = [idx[n] for n in cell_names if n in idx]
    if not members:
        print("block_detail: no matching cells")
        return
    drv, fanin = build(circuit)
    mset = set(members)
    cv = Canvas(circuit)

    def colour_of(i):
        fam = blocks.family(circuit.insts[i]["cell"], circuit.models[i])
        if i in mset:
            return COLOURS.get(fam, "#000075"), 0.95
        return "#dddddd", 0.35

    _cell_boxes(cv, circuit, colour_of)
    for i in members:
        for net in fanin[i]:
            j = drv.get(net)
            if j in mset and j != i:
                ra, rb = circuit.insts[j], circuit.insts[i]
                cv.add(f'<line x1="{cv.sx(ra["x"]):.1f}" '
                       f'y1="{cv.yflip(ra["y"]):.1f}" '
                       f'x2="{cv.sx(rb["x"]):.1f}" '
                       f'y2="{cv.yflip(rb["y"]):.1f}" stroke="#222" '
                       f'stroke-width="0.8" opacity="0.7"/>')
    cv.title(f"{title}: {len(members)} cells")
    cv.legend([("in block", "#e6194b"), ("other", "#dddddd")])
    cv.save(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("netlist")
    ap.add_argument("outdir")
    ap.add_argument("--output", default=None)
    ap.add_argument("--report", default=None,
                    help="report JSON from puzzle_report.py, for block boxes")
    a = ap.parse_args()

    data = json.load(open(a.netlist))
    c = Circuit(data)
    os.makedirs(a.outdir, exist_ok=True)
    base = os.path.join(a.outdir, os.path.splitext(
        os.path.basename(a.netlist))[0])

    target = a.output
    if target is None:
        for cand in ("success", "S", "out"):
            if cand in c.outputs:
                target = cand
                break

    floorplan(c, base + "_floorplan.svg", title="floorplan")

    if target:
        drv, fanin = build(c)
        cone, _ = full_cone(c, drv, fanin, [target])
        greyed = {i for i in range(len(c.insts)) if i not in cone}
        floorplan(c, base + "_cone.svg", greyed,
                  title=f"cone of '{target}'")
        register_map(c, base + "_registers.svg", target)
    else:
        register_map(c, base + "_registers.svg")

    if a.report and os.path.exists(a.report):
        rep = json.load(open(a.report))
        block_map(c, rep, base + "_blocks.svg")
        chains = rep.get("shift_chains") or []
        if chains:
            block_detail(c, base + "_chain0.svg", chains[0]["flops"],
                         title=f"longest chain ({chains[0]['length']} flops)")


if __name__ == "__main__":
    main()
