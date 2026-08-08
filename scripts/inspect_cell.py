"""Dump one cell's contents: sub-instances, layers, labels, bbox.

Use this when extraction reports a cell it cannot model, to find out whether it
is a hierarchical container holding standard cells (in which case extraction
should recurse into it) or a genuine custom leaf built from raw geometry.

Usage: python scripts/inspect_cell.py <in.gds> <CELL_NAME> [more names...]
"""

import collections
import sys

import klayout.db as db

from gdslib import CONDUCTORS, CUTS, TEXT_LAYERS


def describe(layout, name):
    idx = layout.cell_by_name(name)
    if idx < 0:
        print(f"{name}: NOT FOUND in this layout")
        return
    cell = layout.cell(idx)
    bb = cell.bbox()
    print(f"=== {name} ===")
    print(f"  bbox      : {bb.width() * layout.dbu:.3f} x "
          f"{bb.height() * layout.dbu:.3f} um")

    subs = collections.Counter()
    total = 0
    for inst in cell.each_inst():
        child = layout.cell(inst.cell_index)
        n = inst.cell_inst.size()
        subs[child.name] += n
        total += n
    print(f"  sub-instances: {total} ({len(subs)} distinct)")
    for k, v in subs.most_common(30):
        print(f"      {v:5d}  {k}")
    if total == 0:
        print("      (none: this is a LEAF cell built from raw geometry)")

    print("  own shapes by layer:")
    known = {(l, d): n for n, l, d in CONDUCTORS}
    known.update({(l, d): n for n, l, d in CUTS})
    known.update({(l, d): f"{n}.text" for n, (l, d) in TEXT_LAYERS.items()})
    counts = collections.Counter()
    for li in layout.layer_indexes():
        n = cell.shapes(li).size()
        if n:
            info = layout.get_info(li)
            counts[(info.layer, info.datatype)] = n
    for (l, d), n in sorted(counts.items()):
        print(f"      {l:4d}/{d:<3d} {n:7d}  {known.get((l, d), '')}")
    if not counts:
        print("      (no own geometry: pure hierarchy)")

    texts = []
    for lname, (l, d) in TEXT_LAYERS.items():
        li = layout.layer(l, d)
        for sh in cell.shapes(li).each():
            if sh.is_text():
                texts.append((sh.text.string, lname))
    print(f"  labels    : {sorted(set(texts)) if texts else 'none'}")
    print()


def main():
    layout = db.Layout()
    layout.read(sys.argv[1])
    print(f"top cell: {layout.top_cell().name}")
    print(f"cells in library: {layout.cells()}\n")
    for name in sys.argv[2:]:
        describe(layout, name)


if __name__ == "__main__":
    main()
