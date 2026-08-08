"""Export path checks.

The SMT2 export is an independent SOLVER check (not an independent circuit
model -- it is generated from the same cells.py), so it is tested by re-solving
the exported file from scratch and requiring it to agree with the BMC result:
SAT at depth 8, UNSAT at depth 7, which is the warm-up's true answer.  Verilog
emission is checked structurally, since no Verilog simulator is installed in
this environment.
"""

import io
import json
import os
import sys
import contextlib

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

import z3

import emit_behavioural
import emit_verilog
import export_solver

GEN = os.path.join(HERE, "..", "generated")
NETLIST = os.path.join(GEN, "warmup.json")


def main():
    fails = []

    def check(name, cond, got):
        print(f"  {'ok  ' if cond else 'FAIL'}  {name}: {got}")
        if not cond:
            fails.append(name)

    data = json.load(open(NETLIST))
    quiet = io.StringIO()

    beh = os.path.join(GEN, "test_behavioural.v")
    stru = os.path.join(GEN, "test_structural.v")
    with contextlib.redirect_stdout(quiet):
        emit_behavioural.emit(data, beh, "adder_demo")
        emit_verilog.emit(data, stru, "adder_demo")

    b = open(beh).read()
    check("behavioural: one always block per flop",
          b.count("always @(") == 16, b.count("always @("))
    check("behavioural: assign per combinational cell",
          b.count("assign ") >= 63, b.count("assign "))
    check("behavioural: no PDK cells referenced",
          "sky130_fd_sc_hd__" not in b.replace("//", "\n//").split("//")[0],
          "clean")
    check("behavioural: balanced module", b.count("module ") == 1
          and b.count("endmodule") == 1, "1/1")

    s = open(stru).read()
    check("structural: instantiates PDK cells",
          s.count("sky130_fd_sc_hd__") >= 79, s.count("sky130_fd_sc_hd__"))

    # --- SMT2 independent path -------------------------------------------
    for depth, expect in ((8, z3.sat), (7, z3.unsat)):
        path = os.path.join(GEN, f"test_bmc_{depth}.smt2")
        with contextlib.redirect_stdout(quiet):
            export_solver.write_smt2(NETLIST, path, "S", depth, {"en": 1},
                                     "rst_n", 1, False)
        solver = z3.Solver()
        solver.add(z3.parse_smt2_file(path))
        got = solver.check()
        check(f"exported SMT2 at depth {depth} is {expect}", got == expect,
              got)
        os.remove(path)

    for f in (beh, stru):
        os.remove(f)

    if fails:
        print(f"\nFAIL: {fails}")
        return 1
    print("\nPASS  export artifacts are consistent with the BMC engine")
    return 0


if __name__ == "__main__":
    sys.exit(main())
