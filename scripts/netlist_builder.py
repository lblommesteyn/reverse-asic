"""Build netlist JSON directly, for testing the simulator and solver.

Extraction is tested separately against real geometry.  This builder exists so
sequential semantics and solver behaviour can be tested on circuits whose
behaviour is known exactly, including flops that the synthetic router cannot
wire, and so randomised fuzzing can generate thousands of small circuits.
"""

import itertools
import random

import cells as cl

PREFIX = "sky130_fd_sc_hd__"


class Builder:
    def __init__(self, top="tb"):
        self.top = top
        self.insts = []
        self.ports = []
        self._n = 0

    def net(self, hint="n"):
        self._n += 1
        return f"{hint}{self._n}"

    def add(self, cell, pins, x=None, y=None):
        if not cell.startswith(PREFIX):
            cell = PREFIX + cell
        model = cl.get_model(cell)
        if model is None:
            raise ValueError(f"no model for {cell}")
        expected = set(model["inputs"]) | set(model["outputs"])
        unknown = set(pins) - expected
        if unknown:
            raise ValueError(f"{cell}: unknown pins {unknown}; "
                             f"expected {sorted(expected)}")
        name = f"U{len(self.insts)}"
        self.insts.append({
            "name": name, "cell": cell,
            "x": float(len(self.insts) if x is None else x),
            "y": float(0 if y is None else y),
            "orient": "0", "pins": dict(pins),
        })
        return name

    def port(self, name):
        if name not in self.ports:
            self.ports.append(name)
        return name

    def build(self):
        nets = {}
        for r in self.insts:
            for pin, net in r["pins"].items():
                nets.setdefault(net, []).append(f"{r['name']}.{pin}")
        return {
            "schema": 2, "top": self.top, "dbu": 0.001,
            "constants": {"VPWR": 1, "VGND": 0},
            "instances": self.insts,
            "ports": sorted(self.ports),
            "nets": {k: sorted(v) for k, v in sorted(nets.items())},
            "diagnostics": {},
        }


# ------------------------------------------------------------------ fuzzing

COMB_POOL = [
    ("nand2_1", ["A", "B"], "Y"),
    ("nor2_1", ["A", "B"], "Y"),
    ("and2_1", ["A", "B"], "X"),
    ("or2_1", ["A", "B"], "X"),
    ("xor2_1", ["A", "B"], "X"),
    ("xnor2_1", ["A", "B"], "Y"),
    ("inv_1", ["A"], "Y"),
    ("buf_1", ["A"], "X"),
    ("a21o_1", ["A1", "A2", "B1"], "X"),
    ("a21boi_1", ["A1", "A2", "B1_N"], "Y"),
    ("o21ai_1", ["A1", "A2", "B1"], "Y"),
    ("mux2_1", ["A0", "A1", "S"], "X"),
    ("and3_1", ["A", "B", "C"], "X"),
    ("nor3_1", ["A", "B", "C"], "Y"),
    ("maj3_1", ["A", "B", "C"], "X"),
    ("or2b_1", ["A", "B_N"], "X"),
    ("nand2b_1", ["A_N", "B"], "Y"),
]

FLOP_POOL = [
    ("dfxtp_1", {}),
    ("dfrtp_1", {"rst": "RESET_B"}),
    ("dfstp_1", {"set": "SET_B"}),
    ("edfxtp_1", {"en": "DE"}),
    ("dfrbp_1", {"rst": "RESET_B", "qn": "Q_N"}),
]


def random_netlist(rng, n_inputs=3, n_comb=10, n_flops=3, n_outputs=1):
    """A random but well-formed sequential circuit.

    Every net has exactly one driver, no combinational loop is created (gates
    only consume nets that already exist), and the clock and reset are shared,
    which is what a synthesised design looks like.
    """
    b = Builder("fuzz")
    clk = b.port("clk")
    rst = b.port("rst_n")
    pool = [b.port(f"in{i}") for i in range(n_inputs)]

    flop_qs = []
    for i in range(n_flops):
        cell, extra = rng.choice(FLOP_POOL)
        q = b.net("q")
        d = rng.choice(pool)
        pins = {"CLK": clk, "D": d, "Q": q}
        if "rst" in extra:
            pins[extra["rst"]] = rst
        if "set" in extra:
            pins[extra["set"]] = rst
        if "en" in extra:
            pins[extra["en"]] = rng.choice(pool)
        if "qn" in extra:
            pins[extra["qn"]] = b.net("qn")
        b.add(cell, pins)
        flop_qs.append(q)
        pool.append(q)

    for _ in range(n_comb):
        cell, ins, out = rng.choice(COMB_POOL)
        o = b.net("w")
        pins = {p: rng.choice(pool) for p in ins}
        pins[out] = o
        b.add(cell, pins)
        pool.append(o)

    # feed some flop D pins from combinational results, creating real state
    driven = [r for r in b.insts if cl.get_model(r["cell"])["seq"]]
    combo = [r["pins"][cl.get_model(r["cell"])["outputs"][0]]
             for r in b.insts if not cl.get_model(r["cell"])["seq"]]
    for r in driven:
        if combo and rng.random() < 0.7:
            r["pins"]["D"] = rng.choice(combo)

    outs = []
    for i in range(n_outputs):
        src = rng.choice(combo or flop_qs)
        name = f"out{i}"
        # rename the driving net to the port name
        for r in b.insts:
            for pin, net in r["pins"].items():
                if net == src:
                    r["pins"][pin] = name
        b.port(name)
        outs.append(name)

    return b.build(), outs
