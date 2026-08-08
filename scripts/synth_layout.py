"""Build synthetic SKY130 layouts with known ground truth.

Places real standard cells from the PDK GDS and routes them, so extraction can
be tested against a netlist we chose rather than one we inferred.  This is what
makes adversarial testing of the geometric extractor possible: we can construct
a layout with a deliberate open, short, or missing via and check that the
extractor either still gets it right or is caught by validation.

Routing style, chosen so that nets cannot accidentally touch:
    pin (li1) -mcon- met1 pad -via- met2 vertical stub -via2- met3 trunk
Each net gets its own met3 trunk at a unique y, and each pin its own met2 stub
at the pin's x.  met2 stubs cross other nets' met3 trunks with no via, which is
itself one of the cases we want to be sure does not create a false connection.
"""

import glob
import os

import klayout.db as db

DBU = 0.001
ROW_HEIGHT = 2.72

MCON = (67, 44)
LI1 = (67, 20)
MET1 = (68, 20)
VIA = (68, 44)
MET2 = (69, 20)
VIA2 = (69, 44)
MET3 = (70, 20)
TEXT = {"li1": (67, 5), "met1": (68, 5), "met2": (69, 5), "met3": (70, 5)}

PDK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pdk",
                   "sky130hd")


class HarnessLimitation(Exception):
    """The synthetic router cannot wire this cell -- NOT an extractor fault.

    Dense cells (notably the flops) cover their li1 pin shapes with internal
    met1, leaving nowhere to drop a landing pad without shorting to another
    internal net.  A real router solves this with off-grid access and pin-shape
    awareness; this harness deliberately does not, and says so loudly rather
    than emitting a shorted layout that would look like an extraction bug.
    """


def um(v):
    return int(round(v / DBU))


class Design:
    def __init__(self, name="synth", pdk=PDK):
        self.pdk = pdk
        self.layout = db.Layout()
        self.layout.dbu = DBU
        self.top = self.layout.create_cell(name)
        self.cells = {}
        self.insts = []          # (cell_name, trans, id)
        self.nets = {}           # net -> [(inst_id, pin)]
        self.ports = set()
        self._trunk_y = ROW_HEIGHT + 1.0
        self.rows = 0
        self._contacts = {}
        self._used_x = set()
        self._trunks = {}
        self._spans = {}

    # ------------------------------------------------------------- loading
    def _load_cell(self, cell_name):
        if cell_name in self.cells:
            return self.cells[cell_name]
        base = cell_name[len("sky130_fd_sc_hd__"):].rsplit("_", 1)[0]
        path = os.path.join(self.pdk, "cells", base, cell_name + ".gds")
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        tmp = db.Layout()
        tmp.read(path)
        src = tmp.cell(tmp.cell_by_name(cell_name))
        dst = self.layout.create_cell(cell_name)
        dst.copy_tree(src)
        self.cells[cell_name] = dst
        return dst

    def cell_width(self, cell_name):
        c = self._load_cell(cell_name)
        return c.bbox().width() * DBU

    # ------------------------------------------------------------ placing
    def place(self, cell_name, x, row=0, orient="R0"):
        """Place a cell with its lower-left corner at (x, row*ROW_HEIGHT)."""
        cell = self._load_cell(cell_name)
        rot = {"R0": 0, "R90": 1, "R180": 2, "R270": 3,
               "MX": 0, "MY": 2, "MX90": 1, "MY90": 3}[orient]
        mirror = orient.startswith("M")
        t = db.ICplxTrans(1.0, rot * 90.0, mirror, 0, 0)
        bb = cell.bbox().transformed(t)
        dx = um(x) - bb.left
        dy = um(row * ROW_HEIGHT) - bb.bottom
        t = db.ICplxTrans(1.0, rot * 90.0, mirror, dx, dy)
        self.top.insert(db.CellInstArray(cell.cell_index(), t))
        iid = len(self.insts)
        self.insts.append((cell_name, t, iid))
        self.rows = max(self.rows, row + 1)
        return iid

    def place_row(self, cell_names, row=0, orient="R0", gap=0.0, x0=0.0):
        ids, x = [], x0
        for cn in cell_names:
            ids.append(self.place(cn, x, row, orient))
            x += self.cell_width(cn) + gap
        return ids

    # ------------------------------------------------------------- pin geometry
    def pin_points(self, iid, pin):
        """All label positions for a pin, in top-level coordinates."""
        cell_name, t, _ = self.insts[iid]
        cell = self.cells[cell_name]
        out = []
        for layer, (ly, dt) in TEXT.items():
            li = self.layout.layer(ly, dt)
            for sh in cell.shapes(li).each():
                if sh.is_text() and sh.text.string == pin:
                    p = db.Point(sh.text.trans.disp.x, sh.text.trans.disp.y)
                    out.append((t * p, layer))
        return out

    def _shapes_region(self, iid, ld):
        """A cell's shapes on one layer, transformed into top coordinates."""
        cell_name, t, _ = self.insts[iid]
        cell = self.cells[cell_name]
        r = db.Region()
        for sh in cell.shapes(self._layer(ld)).each():
            if sh.is_box():
                r.insert(db.Polygon(sh.box))
            elif sh.is_polygon():
                r.insert(sh.polygon)
            elif sh.is_path():
                r.insert(sh.polygon)
        return r.transformed(t)

    def contact_point(self, iid, pin):
        """Pick a safe place to drop the via stack for a pin.

        Contacting at the label point is what a naive harness does, and it goes
        wrong two ways: the label can sit near the cell's met1 power rail, so the
        met1 landing pad shorts the pin to VPWR, and two pins of the same cell
        can share an x, so their met2 escape stubs merge into one net.  Both
        produce a layout that is genuinely shorted, which would then look like an
        extractor bug.

        So instead we search the pin's own li1 polygon for a point that is clear
        of every met1 rail and whose x is unused by any other escape.
        """
        key = (iid, pin)
        if key in self._contacts:
            return self._contacts[key]

        pts = self.pin_points(iid, pin)
        if not pts:
            raise ValueError(f"no label for pin {pin} on instance {iid}")
        label = pts[0][0]

        li1 = self._shapes_region(iid, LI1)
        target = None
        for poly in li1.each():
            if poly.inside(label):
                target = poly
                break
        if target is None:
            target = min(li1.each(),
                         key=lambda p: p.bbox().center().distance(label))

        forbidden = self._shapes_region(iid, MET1).sized(um(0.12))
        bb = target.bbox()
        step = um(0.05)
        best = None
        for x in range(bb.left + step, bb.right - step + 1, step):
            if any(abs(x - ux) < um(0.26) for ux in self._used_x):
                continue
            for y in range(bb.bottom + step, bb.top - step + 1, step):
                p = db.Point(x, y)
                if not target.inside(p):
                    continue
                probe = db.Region(db.Box(x - um(0.12), y - um(0.12),
                                         x + um(0.12), y + um(0.12)))
                if not (probe & forbidden).is_empty():
                    continue
                d = abs(y - target.bbox().center().y)
                if best is None or d < best[0]:
                    best = (d, p)
            if best is not None:
                break
        if best is None:
            raise HarnessLimitation(
                f"no safe met1 landing pad for {pin} on instance {iid} "
                f"({self.insts[iid][0]}): the cell's internal met1 covers its "
                f"li1 pin shape")
        pt = best[1]
        self._used_x.add(pt.x)
        self._contacts[key] = pt
        return pt

    # ------------------------------------------------------------- routing
    def connect(self, net, pins, port=False, skip_via=None, break_trunk=False):
        """Route a net across the given (inst_id, pin) pairs.

        skip_via: index into `pins` whose via2 is omitted, simulating a missing
                  via that silently disconnects one leaf.
        break_trunk: split the met3 trunk into two pieces with a gap, simulating
                  an open metal segment.
        """
        self.nets[net] = list(pins)
        if port:
            self.ports.add(net)

        trunk_y = self._trunk_y
        self._trunk_y += 0.6
        self._trunks[net] = trunk_y

        xs = []
        for k, (iid, pin) in enumerate(pins):
            pt = self.contact_point(iid, pin)
            px, py = pt.x * DBU, pt.y * DBU
            xs.append(px)
            self._box(MCON, px, py, 0.17)
            self._box(MET1, px, py, 0.20)
            self._box(VIA, px, py, 0.15)
            # met2 stub from the pin up to the trunk
            self._vwire(MET2, px, min(py, trunk_y), max(py, trunk_y), 0.14)
            if skip_via != k:
                self._box(VIA2, px, trunk_y, 0.15)
                self._box(MET2, px, trunk_y, 0.20)
                self._box(MET3, px, trunk_y, 0.20)

        if xs:
            lo, hi = min(xs), max(xs)
            self._spans[net] = (lo, hi)
            if break_trunk and hi - lo > 1.0:
                mid = (lo + hi) / 2
                self._hwire(MET3, lo, mid - 0.4, trunk_y, 0.20)
                self._hwire(MET3, mid + 0.4, hi, trunk_y, 0.20)
            else:
                self._hwire(MET3, lo, hi, trunk_y, 0.20)

        if port:
            self._label(TEXT["met3"], net, xs[0] if xs else 0, trunk_y)
        return trunk_y

    def short_nets(self, net_a, net_b, y_a, y_b, x=None):
        """Deliberately bridge two met3 trunks with a vertical met3 wire.

        The bridge x must lie inside BOTH trunks' spans or it touches nothing,
        which would make the test silently vacuous, so it is derived from the
        spans rather than passed in blind.
        """
        sa, sb = self._spans.get(net_a), self._spans.get(net_b)
        if sa is None or sb is None:
            raise ValueError("both nets must be routed before shorting")
        lo, hi = max(sa[0], sb[0]), min(sa[1], sb[1])
        if lo > hi:
            raise ValueError(
                f"trunks of {net_a} and {net_b} do not overlap in x "
                f"({sa} vs {sb}); cannot build the short")
        if x is None or not (lo <= x <= hi):
            x = (lo + hi) / 2
        self._vwire(MET3, x, min(y_a, y_b), max(y_a, y_b), 0.20)
        return x

    def tie_to_rail(self, iid, pin, rail="VPWR"):
        """Wire a signal pin directly to the row's power rail."""
        pt = self.contact_point(iid, pin)
        px, py = pt.x * DBU, pt.y * DBU
        cell_name, t, _ = self.insts[iid]
        bb = self.cells[cell_name].bbox().transformed(t)
        rail_y = (bb.top if rail == "VPWR" else bb.bottom) * DBU
        self._box(MCON, px, py, 0.17)
        self._box(MET1, px, py, 0.20)
        self._vwire(MET1, px, min(py, rail_y), max(py, rail_y), 0.20)
        self.nets.setdefault(rail, []).append((iid, pin))

    def label_rails(self):
        """Put VPWR/VGND labels on the met1 rails of row 0."""
        if not self.insts:
            return
        cell_name, t, _ = self.insts[0]
        bb = self.cells[cell_name].bbox().transformed(t)
        x = (bb.left + bb.right) / 2 * DBU
        self._label(TEXT["met1"], "VPWR", x, bb.top * DBU)
        self._label(TEXT["met1"], "VGND", x, bb.bottom * DBU)

    # --------------------------------------------------------------- shapes
    def _layer(self, ld):
        return self.layout.layer(ld[0], ld[1])

    def _box(self, ld, cx, cy, size):
        h = size / 2
        self.top.shapes(self._layer(ld)).insert(
            db.Box(um(cx - h), um(cy - h), um(cx + h), um(cy + h)))

    def trunk_y_of(self, net):
        return self._trunks.get(net)

    def _hwire(self, ld, x0, x1, y, w):
        h = w / 2
        self.top.shapes(self._layer(ld)).insert(
            db.Box(um(x0 - h), um(y - h), um(x1 + h), um(y + h)))

    def _vwire(self, ld, x, y0, y1, w):
        h = w / 2
        self.top.shapes(self._layer(ld)).insert(
            db.Box(um(x - h), um(y0 - h), um(x + h), um(y1 + h)))

    def _label(self, ld, text, x, y):
        self.top.shapes(self._layer(ld)).insert(
            db.Text(text, db.Trans(db.Vector(um(x), um(y)))))

    def group_cells(self, container_name, dx=0.0, dy=0.0):
        """Move every placed standard cell into an intermediate cell.

        Absolute positions are preserved, so extraction must produce exactly the
        same netlist as the flat version.  Used to test that the extractor
        descends through hierarchy instead of treating a container as an opaque
        leaf.
        """
        cont = self.layout.create_cell(container_name)
        ctrans = db.ICplxTrans(1.0, 0.0, False, um(dx), um(dy))
        inv = ctrans.inverted()
        erase = []
        # move standard cells AND any container we made earlier, so repeated
        # calls actually build depth instead of finding nothing left to move
        movable = getattr(self, "_containers", set())
        for inst in self.top.each_inst():
            child = self.layout.cell(inst.cell_index)
            if not (child.name.startswith("sky130_fd_sc_hd__")
                    or child.name in movable):
                continue
            for t in inst.cell_inst.each_cplx_trans():
                cont.insert(db.CellInstArray(child.cell_index(), inv * t))
            erase.append(inst)
        for inst in erase:
            self.top.erase(inst)
        self.top.insert(db.CellInstArray(cont.cell_index(), ctrans))
        if not hasattr(self, "_containers"):
            self._containers = set()
        self._containers.add(container_name)
        return cont

    def add_custom_leaf(self, name, layers, x=0.0, row=0, size=0.5):
        """Create and place a custom leaf cell with geometry on given layers.

        `layers` is a list of (layer, datatype).  Used to test how the extractor
        classifies unknown leaf cells: annotation-only ones are ignorable,
        anything with electrical geometry is not.
        """
        try:
            cell = self.layout.cell(self.layout.cell_by_name(name))
        except RuntimeError:
            cell = self.layout.create_cell(name)
            for ld in layers:
                cell.shapes(self._layer(ld)).insert(
                    db.Box(0, 0, um(size), um(size)))
        t = db.ICplxTrans(1.0, 0.0, False, um(x), um(row * ROW_HEIGHT))
        self.top.insert(db.CellInstArray(cell.cell_index(), t))
        return cell

    # ---------------------------------------------------------------- output
    def write(self, path):
        self.layout.write(path)
        return path

    def ground_truth(self):
        """The netlist we intended, in the same shape extract_netlist emits."""
        pins_by_inst = {}
        for net, pins in self.nets.items():
            for iid, pin in pins:
                pins_by_inst.setdefault(iid, {})[pin] = net
        insts = []
        for cell_name, t, iid in self.insts:
            insts.append({"id": iid, "cell": cell_name,
                          "pins": pins_by_inst.get(iid, {})})
        return {"instances": insts, "ports": sorted(self.ports)}


def compare_to_truth(recovered, truth, ignore_pins=()):
    """Compare a recovered netlist against ground truth up to net renaming.

    Nets are matched by the set of (cell, pin) endpoints they touch, since the
    extractor invents its own net names.
    """
    def signature(instances, pins_of):
        by_net = {}
        for idx, r in enumerate(instances):
            for pin, net in pins_of(r).items():
                if pin in ignore_pins:
                    continue
                by_net.setdefault(net, set()).add((idx, r["cell"], pin))
        return {frozenset(v) for v in by_net.values()}

    # instances are ordered by position in both, so index alignment holds only
    # if the counts match
    problems = []
    if len(recovered["instances"]) != len(truth["instances"]):
        problems.append(f"instance count {len(recovered['instances'])} != "
                        f"{len(truth['instances'])}")
        return problems

    rec_cells = [r["cell"] for r in recovered["instances"]]
    tru_cells = [r["cell"] for r in truth["instances"]]
    if sorted(rec_cells) != sorted(tru_cells):
        problems.append(f"cell multiset differs: {sorted(rec_cells)} != "
                        f"{sorted(tru_cells)}")
        return problems

    rec = signature(recovered["instances"], lambda r: r["pins"])
    tru = signature(truth["instances"], lambda r: r["pins"])
    only_rec = rec - tru
    only_tru = tru - rec
    for s in sorted(only_tru, key=repr)[:6]:
        problems.append(f"expected net grouping missing: {sorted(s)}")
    for s in sorted(only_rec, key=repr)[:6]:
        problems.append(f"unexpected net grouping: {sorted(s)}")
    return problems
