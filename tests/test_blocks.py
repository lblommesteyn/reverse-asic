"""Regression tests for the functional block detectors.

The warm-up is two 8-bit shift registers feeding an 8-bit adder and a comparison
against 496, so the detectors must find exactly that structure -- and must find
it from the netlist alone, with no hint about what the design is.
"""

import json
import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

import blocks
import puzzle_report
from netgraph import build, register_graph
from sim import Circuit

NETLIST = os.path.join(HERE, "..", "generated", "warmup.json")


def main():
    data = json.load(open(NETLIST))
    c = Circuit(data)
    drv, fanin = build(c)
    edges, prim, dcone, qnet = register_graph(c, drv, fanin)
    fails = []

    def check(name, cond, got):
        print(f"  {'ok  ' if cond else 'FAIL'}  {name}: {got}")
        if not cond:
            fails.append(name)

    # --- the two 8-bit shift registers ------------------------------------
    chains = blocks.shift_chains(c, edges, dcone, qnet, prim)
    eights = [ch for ch in chains if ch["length"] == 8]
    check("two 8-bit chains found", len(eights) == 2,
          [ch["length"] for ch in chains])
    check("both are plain shift registers",
          all(ch["kind"] == "shift_register" for ch in eights),
          [ch["kind"] for ch in eights])
    serial = sorted({s for ch in eights for s in ch["serial_inputs"]
                     if s in ("A", "B")})
    check("serial inputs are A and B", serial == ["A", "B"], serial)
    check("chains are disjoint",
          not (set(eights[0]["flops"]) & set(eights[1]["flops"])),
          f"{len(set(eights[0]['flops']))} + {len(set(eights[1]['flops']))}")
    check("chains have distinct physical rows",
          abs(eights[0]["bbox"]["y0"] - eights[1]["bbox"]["y0"]) > 5,
          f"y0 {eights[0]['bbox']['y0']} vs {eights[1]['bbox']['y0']}")

    # --- the adder ---------------------------------------------------------
    carry = blocks.carry_chains(c, drv, fanin)
    check("carry chain detected", len(carry) >= 1, len(carry))
    if carry:
        best = carry[0]
        check("carry chain is adder-like",
              best["kind"] == "adder_or_subtractor_carry_chain", best["kind"])
        check("carry chain is deep enough for 8 bits", best["length"] >= 6,
              best["length"])
        check("carry chain has xor taps", best["xor_taps"] >= 3,
              best["xor_taps"])

    # --- the comparison against 496 ---------------------------------------
    cmps = blocks.comparators(c, drv, fanin, qnet)
    by_net = {d["net"]: d for d in cmps}
    check("S identified as a comparison", "S" in by_net, sorted(by_net))
    if "S" in by_net:
        d = by_net["S"]
        # a+b == 496 with two 8-bit operands has exactly 15 solutions
        check("S has exactly 15 satisfying assignments",
              d["n_solutions"] == 15, d["n_solutions"])
        check("S depends on all 16 state bits", d["state_bits"] == 16,
              d["state_bits"])

    # --- the success-relevant state cone ----------------------------------
    rep = puzzle_report.gather(data, "S", deep=False)
    sc = rep["sequential_cone"]
    check("all 16 state bits are in the cone",
          sc["transitive_state_bits"] == 16, sc["transitive_state_bits"])
    check("no state bits outside the cone",
          sc["state_bits_outside_cone"] == 0, sc["state_bits_outside_cone"])
    check("no cells outside the cone", rep["cells_outside_cone"] == 0,
          rep["cells_outside_cone"])
    check("register-graph depth is 8 (shift fill time)",
          sc["register_graph_depth"] == 8, sc["register_graph_depth"])
    check("suggested BMC depth matches the true answer",
          sc["suggested_min_bmc_cycles"] == 8,
          sc["suggested_min_bmc_cycles"])
    check("A and B classified as serial data inputs",
          sorted(s["input"] for s in rep["candidate_serial_inputs"])
          == ["A", "B"],
          [s["input"] for s in rep["candidate_serial_inputs"]])
    check("en classified as broadcast control",
          [s["input"] for s in rep["broadcast_control_inputs"]] == ["en"],
          [s["input"] for s in rep["broadcast_control_inputs"]])

    # --- mux bank (the 16 enable-held flops) ------------------------------
    banks = blocks.mux_banks(c, drv, fanin)
    check("enable mux bank found",
          any(b["select_net"] == "en" and b["width"] == 16 for b in banks),
          [(b["select_net"], b["width"]) for b in banks[:3]])

    if fails:
        print(f"\nFAIL: {fails}")
        return 1
    print("\nPASS  all warm-up structures identified from the netlist alone")
    return 0


if __name__ == "__main__":
    sys.exit(main())
