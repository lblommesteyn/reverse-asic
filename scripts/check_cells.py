"""Validate the cell library's pin sets against the pin labels present in a GDS.

The GDS carries every standard cell's real pin names as text labels, so any
mistake in the naming-convention decoder (wrong _N suffix, wrong output pin,
missing cell) shows up immediately as a pin-set mismatch.

Usage: python scripts/check_cells.py <in.gds>
"""

import sys
import collections

import klayout.db as db

import cells as cl
from gdslib import (POWER_PINS, classify_leaf, has_subcells,
                    is_functional, TEXT_LAYERS)


def main(gds_path):
    layout = db.Layout()
    layout.read(gds_path)
    top = layout.top_cell()

    used = collections.Counter()
    for inst in top.each_inst():
        c = layout.cell(inst.cell_index)
        if is_functional(c.name):
            used[c.name] += inst.cell_inst.size()

    text_layers = [layout.layer(l, d) for l, d in TEXT_LAYERS.values()]
    bad = 0
    physical = []
    for name in sorted(used):
        cell = layout.cell(layout.cell_by_name(name))
        pins = set()
        for li in text_layers:
            for sh in cell.shapes(li).each():
                if sh.is_text() and sh.text.string not in POWER_PINS:
                    pins.add(sh.text.string)
        refusal = cl.unsupported_reason(name)
        if refusal:
            print(f"UNSUPPORTED    {name:38s}")
            print(f"    REFUSED: {refusal}")
            bad += 1
            continue
        model = cl.get_model(name)
        if model is None:
            if not has_subcells(cell):
                kind, layers = classify_leaf(layout, cell)
                if kind == "physical_only":
                    lay = sorted(f"{l}/{d}" for l, d in layers)
                    print(f"ok phys       {name:38s} annotation only, "
                          f"layers {lay}")
                    physical.append(name)
                    continue
                print(f"MISSING MODEL  {name:38s} gds pins {sorted(pins)}")
                print(f"    has geometry on electrical layers: "
                      f"{sorted(f'{l}/{d}' for l, d in layers)}")
                bad += 1
                continue
            print(f"MISSING MODEL  {name:38s} (hierarchical cell)")
            bad += 1
            continue
        declared = set(model["inputs"]) | set(model["outputs"])
        if declared != pins:
            print(f"PIN MISMATCH   {name:38s}")
            print(f"    gds     {sorted(pins)}")
            print(f"    library {sorted(declared)}")
            bad += 1
        else:
            kind = "seq " if model["seq"] else "comb"
            print(f"ok {kind}       {name:38s} {sorted(pins)}")

    print(f"\n{len(used)} distinct functional cells, {bad} problems")
    return bad == 0


if __name__ == "__main__":
    sys.exit(0 if main(sys.argv[1]) else 1)
