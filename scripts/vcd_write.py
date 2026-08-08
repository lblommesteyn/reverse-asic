"""Minimal VCD writer, for dumping simulated traces.

Used to view a solved input sequence in Surfer/GTKWave, and to generate protocol
fixtures for testing the VCD analyzer without touching any puzzle file.
"""

import itertools

_IDCHARS = "".join(chr(c) for c in range(33, 127))


def _ids(n):
    out, width = [], 1
    while len(out) < n:
        out = ["".join(p) for p in itertools.product(_IDCHARS, repeat=width)]
        width += 1
    return out[:n]


def write_vcd(path, signals, cycles, timescale="1ns", half_period=5):
    """signals: ordered list of names.  cycles: list of dicts name->0/1.

    One clock cycle is emitted per entry, with the named clock (if any signal is
    called 'clk') toggling low then high, and data changing on the low phase so
    it is stable across the rising edge.
    """
    ids = dict(zip(signals, _ids(len(signals))))
    lines = [
        "$timescale %s $end" % timescale,
        "$scope module tb $end",
    ]
    for s in signals:
        lines.append(f"$var wire 1 {ids[s]} {s} $end")
    lines.append("$upscope $end")
    lines.append("$enddefinitions $end")

    t = 0
    last = {}
    for row in cycles:
        # low phase: apply new data values
        lines.append(f"#{t}")
        for s in signals:
            v = "0" if s == "clk" else str(int(bool(row.get(s, 0))))
            if last.get(s) != v:
                lines.append(f"{v}{ids[s]}")
                last[s] = v
        t += half_period
        # high phase: clock rises
        lines.append(f"#{t}")
        if "clk" in ids and last.get("clk") != "1":
            lines.append(f"1{ids['clk']}")
            last["clk"] = "1"
        t += half_period
    lines.append(f"#{t}")

    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return path
