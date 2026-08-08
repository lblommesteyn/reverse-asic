"""Emit self-contained behavioural Verilog from a recovered netlist.

Unlike the structural netlist, this needs no PDK library: every gate is expanded
into an assign statement and every flop into an always block, using the same
cell definitions the simulator and the SAT engine use.

Because it is generated from cells.py it checks another EXECUTION ENGINE, not
the cell definitions themselves: a wrong cell model would be reproduced here
faithfully.  Cell correctness is established only by audit_cell_semantics.py
against the SKY130 Liberty data.

Usage: python scripts/emit_behavioural.py recovered.json out.v [module]
"""

import json
import re
import sys

import cells as cl
from netgraph import build
from sim import Circuit


def ident(n):
    return n if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", n) else f"\\{n} "


def emit(data, path, module=None):
    c = Circuit(data)
    module = module or (data.get("top") or "recovered")
    ops = cl.VerilogOps()

    ports = [p for p in data["ports"] if p not in ("VPWR", "VGND")]
    inputs = [p for p in ports if p not in c.driver]
    outputs = [p for p in ports if p in c.driver]
    # any undriven, non-constant net is a primary input even if unlabelled
    for n in c.inputs:
        if n not in inputs:
            inputs.append(n)

    wires = [n for n in c.nets
             if n not in inputs and n not in outputs and n not in c.constants]

    L = []
    a = L.append
    a("// Behavioural Verilog generated from a GDS-recovered netlist.")
    a("// Self-contained: no standard-cell library required.")
    a(f"module {ident(module)} (")
    a("    " + ",\n    ".join(ident(p) for p in inputs + outputs))
    a(");")
    for p in inputs:
        a(f"  input {ident(p)};")
    for p in outputs:
        a(f"  output {ident(p)};")
    a("")
    for n in sorted(c.constants):
        a(f"  wire {ident(n)} = 1'b{int(c.constants[n])};")
    for w in sorted(wires):
        a(f"  wire {ident(w)};")
    a("")

    # combinational cells, in topological order so the file reads as a dataflow
    for i in c.order:
        r, m = c.insts[i], c.models[i]
        p = {}
        for pin in m["inputs"]:
            net = r["pins"].get(pin)
            p[pin] = ident(net) if net else "1'b0"
        res = m["fn"](ops, p)
        outs = res if m.get("multi") else {m["outputs"][0]: res}
        for out, expr in outs.items():
            net = r["pins"].get(out)
            if net:
                a(f"  assign {ident(net)} = {expr};  // {r['name']} {r['cell']}")
    a("")

    # sequential cells
    for i in c.flops:
        r, m = c.insts[i], c.models[i]
        s = m["seq"]
        q = r["pins"].get(s["q"])
        if not q:
            continue
        clk = r["pins"].get(s["clk"])
        d = r["pins"].get(s["d"], None)
        dexpr = ident(d) if d else "1'b0"
        # Enable first, scan second: Liberty's next_state for a scan-enabled
        # flop is (D & DE & !SCE) | (IQ & !DE & !SCE) | (SCD & SCE), so the scan
        # mux OVERRIDES the enable.  The opposite order would let DE=0 defeat
        # SCE=1.  sim.py applies the same order.
        if "en" in s and s["en"] in r["pins"]:
            en = ident(r["pins"][s["en"]])
            dexpr = f"({en} ? {dexpr} : {ident(q)})"
        if "scan" in s and s["scan"][1] in r["pins"]:
            scd = ident(r["pins"][s["scan"][0]])
            sce = ident(r["pins"][s["scan"][1]])
            dexpr = f"({sce} ? {scd} : {dexpr})"

        edge = "negedge" if s.get("negedge") else "posedge"
        sens = [f"{edge} {ident(clk)}"] if clk else []
        body = []
        for key, val in (("rst", "1'b0"), ("set", "1'b1")):
            if key in s and s[key][0] in r["pins"]:
                pin, active_low = s[key]
                net = ident(r["pins"][pin])
                sens.append(f"{'negedge' if active_low else 'posedge'} {net}")
                cond = f"{'!' if active_low else ''}{net}"
                body.append((cond, val))

        a(f"  reg {ident(q)}_r;  // {r['name']} {r['cell']}")
        a(f"  always @({' or '.join(sens)}) begin")
        first = True
        for cond, val in body:
            a(f"    {'if' if first else 'else if'} ({cond}) {ident(q)}_r <= {val};")
            first = False
        a(f"    {'else ' if body else ''}{ident(q)}_r <= {dexpr};")
        a("  end")
        a(f"  assign {ident(q)} = {ident(q)}_r;")
        if "qn" in s and s["qn"] in r["pins"]:
            a(f"  assign {ident(r['pins'][s['qn']])} = ~{ident(q)}_r;")
        a("")

    a("endmodule")
    open(path, "w").write("\n".join(L) + "\n")
    print(f"wrote {path}: {len(c.insts)} cells expanded "
          f"({len(c.comb)} assigns, {len(c.flops)} always blocks)")


if __name__ == "__main__":
    emit(json.load(open(sys.argv[1])), sys.argv[2],
         sys.argv[3] if len(sys.argv) > 3 else None)
