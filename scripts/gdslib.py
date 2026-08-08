"""Shared helpers for recovering a gate-level netlist from a placed-and-routed GDS.

The approach assumes the standard-cell hierarchy survived in the GDS (it does for
both the Jane Street warm-up and, as far as the cell list goes, the real puzzle):
each functional gate is a reference to a named sky130_fd_sc_hd cell, and each of
those cells carries its pin names as li1 text labels.

Connectivity is recovered geometrically:

    li1 -mcon- met1 -via- met2 -via2- met3 -via3- met4 -via4- met5

Every conductor layer is flattened and merged, so each merged polygon is by
construction one connected component *within* that layer.  Cross-layer joins come
only from cut shapes: a cut is small and fully enclosed by the metal above and
below, so its centre point identifies exactly one polygon on each side.  A
union-find over (layer, polygon) nodes then yields the nets.
"""

import klayout.db as db

# (conductor layer, conductor datatype) in stack order, bottom to top.
CONDUCTORS = [
    ("li1", 67, 20),
    ("met1", 68, 20),
    ("met2", 69, 20),
    ("met3", 70, 20),
    ("met4", 71, 20),
    ("met5", 72, 20),
]

# Cut layers joining conductor i to conductor i+1.
CUTS = [
    ("mcon", 67, 44),
    ("via", 68, 44),
    ("via2", 69, 44),
    ("via3", 70, 44),
    ("via4", 71, 44),
]

# Text layers carrying pin / port names, paired with the conductor they sit on.
TEXT_LAYERS = {
    "li1": (67, 5),
    "met1": (68, 5),
    "met2": (69, 5),
    "met3": (70, 5),
    "met4": (71, 5),
    "met5": (72, 5),
}

POWER_PINS = {"VPWR", "VGND", "VPB", "VNB", "VDD", "VSS"}

# Cells that exist for physical reasons only and carry no logic.
NON_FUNCTIONAL_PREFIXES = (
    "VIA_",
    "sky130_fd_sc_hd__decap",
    "sky130_fd_sc_hd__tap",
    "sky130_fd_sc_hd__fill",
    "sky130_fd_sc_hd__diode",
    "sky130_fd_sc_hd__lpflow_decapkapwr",
    "sky130_fd_sc_hd__lpflow_bleeder",
)


# Layers on which geometry can carry current or form a device.  Anything a cell
# uses to connect to a net, or to build a transistor, is in here.  Everything
# else (annotation, boundaries, text, DRC markers) cannot affect connectivity.
DEVICE_LAYERS = [
    ("nwell", 64, 20),
    ("diff", 65, 20),
    ("tap", 65, 44),
    ("poly", 66, 20),
    ("licon1", 66, 44),
]

ELECTRICAL_LAYERS = {(l, d) for _, l, d in CONDUCTORS} \
    | {(l, d) for _, l, d in CUTS} \
    | {(l, d) for _, l, d in DEVICE_LAYERS}

ELECTRICAL_NAMES = {(l, d): n for n, l, d in CONDUCTORS + CUTS + DEVICE_LAYERS}


def is_functional(cell_name):
    return not cell_name.startswith(NON_FUNCTIONAL_PREFIXES)


def cell_layers(layout, cell):
    """(layer, datatype) -> shape count for a cell's own geometry."""
    out = {}
    for li in layout.layer_indexes():
        n = cell.shapes(li).size()
        if n:
            info = layout.get_info(li)
            out[(info.layer, info.datatype)] = n
    return out


def cell_pin_labels(layout, cell):
    names = set()
    for ly, dt in TEXT_LAYERS.values():
        li = layout.layer(ly, dt)
        for sh in cell.shapes(li).each():
            if sh.is_text():
                names.add(sh.text.string)
    return names


def has_subcells(cell):
    for _ in cell.each_inst():
        return True
    return False


def classify_leaf(layout, cell):
    """Decide what an unmodelled leaf cell is.

    Returns (kind, layers) where kind is one of:

      "physical_only"      cannot attach to any net: no logical model, no pin
                           labels, no sub-instances, and no geometry on any
                           electrical layer.  Safe to ignore.
      "unknown_electrical" has geometry on a conductor, cut or device layer, so
                           it can carry signal.  Must never be ignored.

    The rule is deliberately about geometry, not about names or pin counts: a
    cell called INTERNAL_3 with a transistor in it is dangerous, and a cell with
    no pins but a met1 shape can still short two nets together.
    """
    layers = cell_layers(layout, cell)
    electrical = {ld: n for ld, n in layers.items() if ld in ELECTRICAL_LAYERS}
    if (not electrical and not cell_pin_labels(layout, cell)
            and not has_subcells(cell)):
        return "physical_only", layers
    return "unknown_electrical", layers


class UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        p = self.parent
        if x not in p:
            p[x] = x
            return x
        root = x
        while p[root] != root:
            root = p[root]
        while p[x] != root:
            p[x], x = root, p[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra
        return ra


class PolygonIndex:
    """Grid-bucketed point lookup over a set of disjoint merged polygons."""

    GRID = 4000  # dbu; ~4um buckets

    def __init__(self, polygons):
        self.polys = polygons
        self.bboxes = [p.bbox() for p in polygons]
        self.buckets = {}
        g = self.GRID
        for i, bb in enumerate(self.bboxes):
            for gx in range(bb.left // g, bb.right // g + 1):
                for gy in range(bb.bottom // g, bb.top // g + 1):
                    self.buckets.setdefault((gx, gy), []).append(i)

    def lookup(self, pt):
        """Return index of the polygon containing pt, or None."""
        g = self.GRID
        cands = self.buckets.get((pt.x // g, pt.y // g))
        if not cands:
            return None
        hits = [i for i in cands if self.bboxes[i].contains(pt)]
        if len(hits) == 1:
            return hits[0]
        for i in hits:
            if self.polys[i].inside(pt):
                return i
        return hits[0] if hits else None


def load(gds_path):
    layout = db.Layout()
    layout.read(gds_path)
    return layout, layout.top_cell()


def build_connectivity(layout, top, verbose=True):
    """Flatten + merge conductors, stitch through cuts.

    Returns (indexes, union-find, diagnostics).  Unresolved cuts are the single
    most important extraction health metric: every one of them is a wire the
    router drew that we failed to follow, which silently splits a net in two.
    """
    indexes = {}
    diag = {"layers": {}, "cuts": {}, "unresolved_cut_examples": []}
    for name, ly, dt in CONDUCTORS:
        li = layout.layer(ly, dt)
        region = db.Region(top.begin_shapes_rec(li))
        region.merge()
        polys = list(region.each())
        indexes[name] = PolygonIndex(polys)
        diag["layers"][name] = len(polys)
        if verbose:
            print(f"  {name:5s} {len(polys):7d} merged polygons")

    uf = UnionFind()
    for name, _, _ in CONDUCTORS:
        for i in range(len(indexes[name].polys)):
            uf.find((name, i))

    total_unresolved = 0
    for k, (cname, ly, dt) in enumerate(CUTS):
        lower = CONDUCTORS[k][0]
        upper = CONDUCTORS[k + 1][0]
        li = layout.layer(ly, dt)
        region = db.Region(top.begin_shapes_rec(li))
        region.merge()
        joined = dropped = 0
        for poly in region.each():
            c = poly.bbox().center()
            a = indexes[lower].lookup(c)
            b = indexes[upper].lookup(c)
            if a is None or b is None:
                dropped += 1
                if len(diag["unresolved_cut_examples"]) < 40:
                    diag["unresolved_cut_examples"].append({
                        "cut": cname,
                        "x": round(c.x * layout.dbu, 4),
                        "y": round(c.y * layout.dbu, 4),
                        "missing": lower if a is None else upper,
                    })
                continue
            uf.union((lower, a), (upper, b))
            joined += 1
        total_unresolved += dropped
        diag["cuts"][cname] = {"joined": joined, "unresolved": dropped}
        if verbose:
            print(f"  {cname:5s} {joined:7d} cuts joined, {dropped} unresolved")
    diag["total_unresolved_cuts"] = total_unresolved
    return indexes, uf, diag


def collect_texts(cell, layout):
    """Yield (text_string, klayout.db.Point, conductor_name) for one cell's own shapes."""
    for cname, (ly, dt) in TEXT_LAYERS.items():
        li = layout.layer(ly, dt)
        for sh in cell.shapes(li).each():
            if sh.is_text():
                t = sh.text
                yield t.string, db.Point(t.trans.disp.x, t.trans.disp.y), cname
