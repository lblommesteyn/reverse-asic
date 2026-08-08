"""Regression test for the VCD protocol analyzer.

Builds a stimulus with the same shape the real puzzle is expected to have --
reset held for a few cycles, an enable window, serial data shifted in byte-wise,
then idle -- writes it as a VCD, and checks the analyzer recovers the protocol
without being told anything about it.
"""

import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

import vcd_analyze
from vcd_write import write_vcd

TMP = os.path.join(HERE, "..", "generated", "protocol_fixture.vcd")

RESET_CYCLES = 3
BYTES_A = [0xF8, 0x41]
BYTES_B = [0xF8, 0x7A]
IDLE = 4


def build_cycles():
    rows = []
    for _ in range(RESET_CYCLES):
        rows.append({"clk": 0, "rst_n": 0, "en": 0, "A": 0, "B": 0})
    for a, b in zip(BYTES_A, BYTES_B):
        for k in range(7, -1, -1):
            rows.append({"clk": 0, "rst_n": 1, "en": 1,
                         "A": (a >> k) & 1, "B": (b >> k) & 1})
    for _ in range(IDLE):
        rows.append({"clk": 0, "rst_n": 1, "en": 0, "A": 0, "B": 0})
    return rows


def main():
    os.makedirs(os.path.dirname(TMP), exist_ok=True)
    rows = build_cycles()
    write_vcd(TMP, ["clk", "rst_n", "en", "A", "B"], rows)
    total = len(rows)
    print(f"fixture: {total} cycles "
          f"({RESET_CYCLES} reset, {8 * len(BYTES_A)} data, {IDLE} idle)")

    rep = vcd_analyze.analyze(TMP)
    fails = []

    def check(name, cond, got):
        print(f"  {'ok  ' if cond else 'FAIL'}  {name}: {got}")
        if not cond:
            fails.append(name)

    check("clock identified", rep["clock_used"] == "clk", rep["clock_used"])
    check("active edge rising", rep["active_edge"] == "rising",
          rep["active_edge"])
    check("cycle count", rep["cycles"] == total,
          f"{rep['cycles']} vs {total}")

    resets = {r["name"]: r for r in rep["candidate_resets"]}
    check("reset found", "rst_n" in resets, list(resets))
    if "rst_n" in resets:
        r = resets["rst_n"]
        check("reset polarity", r["polarity"] == "active_low", r["polarity"])
        check("reset duration", r["asserted_cycles"] == RESET_CYCLES,
              r["asserted_cycles"])

    enables = {e["name"] for e in rep["candidate_enables"]}
    check("enable found", "en" in enables, sorted(enables))

    data = {d["name"] for d in rep["candidate_data_inputs"]}
    check("data inputs found", {"A", "B"} <= data, sorted(data))

    check("active window byte aligned",
          rep.get("framing", {}).get("divisible_by_8") is True,
          f"window={rep.get('active_window_cycles')} "
          f"framing={rep.get('framing')}")
    check("byte count", rep.get("framing", {}).get("bytes_if_8bit")
          == len(BYTES_A), rep.get("framing", {}).get("bytes_if_8bit"))

    if fails:
        print(f"\nFAIL: {fails}")
        return 1
    print("\nPASS  protocol inferred correctly from the VCD alone")
    return 0


if __name__ == "__main__":
    sys.exit(main())
