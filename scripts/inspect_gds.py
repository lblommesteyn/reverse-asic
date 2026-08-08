"""First-look report on a GDS: hierarchy, cell inventory, layers, port labels.

Run this before anything else on a new layout.  It answers the one question the
whole approach depends on: did the standard-cell hierarchy survive?  If cell
references carry sky130 names, the netlist can be recovered from placement plus
routing.  If the design is flattened into raw polygons, fall back to matching
cell geometry fingerprints against the PDK GDS instead.

Usage: python scripts/inspect_gds.py <in.gds>
"""

import collections
import sys

import klayout.db as db

from gdslib import CONDUCTORS, CUTS, TEXT_LAYERS, is_functional


def main(path):
    layout = db.Layout()
    layout.read(path)
    top = layout.top_cell()
    print(f"file       : {path}")
    print(f"dbu        : {layout.dbu}")
    print(f"top cell   : {top.name}")
    bb = top.bbox()
    print(f"die bbox   : {bb.width() * layout.dbu:.1f} x "
          f"{bb.height() * layout.dbu:.1f} um")
    print(f"cells in library: {layout.cells()}")

    inst_counts = collections.Counter()
    for inst in top.each_inst():
        inst_counts[layout.cell(inst.cell_index).name] += inst.cell_inst.size()
    total = sum(inst_counts.values())
    print(f"top-level instances: {total}")

    if not inst_counts:
        print("\n!! layout appears FLAT -- no cell references at top level.")
        print("   The hierarchy-based flow will not work; you would need to")
        print("   fingerprint cell geometry against the PDK instead.")
    else:
        func = {k: v for k, v in inst_counts.items() if is_functional(k)}
        phys = {k: v for k, v in inst_counts.items() if not is_functional(k)}
        print(f"  functional cells : {sum(func.values())} "
              f"({len(func)} distinct)")
        print(f"  physical/via only: {sum(phys.values())}")
        print("\nfunctional cell inventory:")
        for k, v in sorted(func.items(), key=lambda kv: -kv[1]):
            print(f"  {v:6d}  {k}")

    print("\nlayer inventory (whole library):")
    counts = collections.Counter()
    for ci in layout.each_cell():
        for li in layout.layer_indexes():
            n = ci.shapes(li).size()
            if n:
                info = layout.get_info(li)
                counts[(info.layer, info.datatype)] += n
    known = {(l, d): n for n, l, d in CONDUCTORS}
    known.update({(l, d): n for n, l, d in CUTS})
    for (l, d), n in sorted(counts.items()):
        tag = known.get((l, d), "")
        print(f"  {l:4d}/{d:<3d} {n:8d}  {tag}")

    print("\ntop-level labels (candidate ports):")
    for name, (l, d) in TEXT_LAYERS.items():
        li = layout.layer(l, d)
        for sh in top.shapes(li).each():
            if sh.is_text():
                t = sh.text
                print(f"  {t.string:20s} on {name:5s} at "
                      f"({t.trans.disp.x * layout.dbu:.2f}, "
                      f"{t.trans.disp.y * layout.dbu:.2f})")


if __name__ == "__main__":
    main(sys.argv[1])
