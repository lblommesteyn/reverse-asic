"""Emit structural Verilog from a recovered netlist JSON.

Output instantiates sky130_fd_sc_hd cells by name with named port connections,
so it can be simulated with iverilog/Verilator against the PDK's behavioural
models, or read by yosys for further analysis.

Usage: python scripts/emit_verilog.py recovered.json out.v [module_name]
"""

import json
import re
import sys

import cells as cl


def ident(n):
    return n if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", n) else f"\\{n} "


def emit(data, path, module=None, power=False):
    module = module or data.get("top", "recovered")
    insts = data["instances"]

    driven = set()
    used = set()
    for r in insts:
        m = cl.get_model(r["cell"])
        outs = set(m["outputs"]) if m else set()
        for pin, net in r["pins"].items():
            used.add(net)
            if pin in outs:
                driven.add(net)

    ports = [p for p in data["ports"] if p not in ("VPWR", "VGND")]
    inputs = [p for p in ports if p not in driven]
    outputs = [p for p in ports if p in driven]
    wires = sorted(used - set(ports))

    L = []
    L.append(f"// Recovered from GDS layout by asic-re")
    L.append(f"// {len(insts)} standard cells, {len(used)} nets")
    L.append("")
    L.append(f"module {ident(module)} (")
    L.append("    " + ",\n    ".join(ident(p) for p in ports))
    L.append(");")
    for p in inputs:
        L.append(f"  input {ident(p)};")
    for p in outputs:
        L.append(f"  output {ident(p)};")
    L.append("")
    for w in wires:
        L.append(f"  wire {ident(w)};")
    L.append("")
    for r in insts:
        conns = ", ".join(
            f".{pin}({ident(net)})" for pin, net in sorted(r["pins"].items()))
        if power:
            conns += ", .VPWR(1'b1), .VGND(1'b0), .VPB(1'b1), .VNB(1'b0)"
        L.append(f"  {r['cell']} {ident(r['name'])} ({conns});")
    L.append("")
    L.append("endmodule")

    with open(path, "w") as f:
        f.write("\n".join(L) + "\n")
    print(f"wrote {path}: {len(insts)} cells, {len(inputs)} inputs, "
          f"{len(outputs)} outputs, {len(wires)} wires")


if __name__ == "__main__":
    data = json.load(open(sys.argv[1]))
    emit(data, sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
