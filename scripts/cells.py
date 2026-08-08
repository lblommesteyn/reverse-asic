"""Logical models of the sky130_fd_sc_hd standard cells.

Every function is written against a pluggable boolean backend (`ops`) so the same
definitions drive both concrete simulation (Python bools) and symbolic solving
(z3 expressions).

Naming convention reminder for the compound cells:
  aXY[b..][o|oi]  AND-OR:  X inputs into the first AND, Y into the second, ...
  oXY[b..][a|ai]  OR-AND
  a trailing 'i' inverts the output (pin Y); no 'i' means buffered (pin X)
  each 'b' marks one active-low input, whose pin name carries the _N suffix
"""

import re


class BoolOps:
    AND = staticmethod(lambda *a: all(a))
    OR = staticmethod(lambda *a: any(a))
    NOT = staticmethod(lambda a: not a)

    @staticmethod
    def XOR(*a):
        r = False
        for x in a:
            r ^= bool(x)
        return r

    @staticmethod
    def MUX(s, a0, a1):
        return a1 if s else a0


class Z3Ops:
    """Symbolic backend.  Python bools are coerced to z3 constants so that
    constant-driven pins and reset values mix freely with symbolic inputs."""

    def __init__(self):
        import z3

        self.z3 = z3

    def _c(self, x):
        return self.z3.BoolVal(x) if isinstance(x, bool) else x

    def AND(self, *a):
        a = [self._c(x) for x in a]
        return a[0] if len(a) == 1 else self.z3.And(*a)

    def OR(self, *a):
        a = [self._c(x) for x in a]
        return a[0] if len(a) == 1 else self.z3.Or(*a)

    def NOT(self, a):
        return self.z3.Not(self._c(a))

    def XOR(self, *a):
        a = [self._c(x) for x in a]
        r = a[0]
        for x in a[1:]:
            r = self.z3.Xor(r, x)
        return r

    def MUX(self, s, a0, a1):
        s = self._c(s)
        if self.z3.is_true(s):
            return self._c(a1)
        if self.z3.is_false(s):
            return self._c(a0)
        return self.z3.If(s, self._c(a1), self._c(a0))


class VerilogOps:
    """Emits Verilog expression strings.

    Sharing the cell definitions with the simulator means the generated
    behavioural Verilog cannot drift from what we simulate and solve: all three
    are the same functions with a different backend.  That is a consistency
    guarantee, NOT a correctness one -- if a definition below is wrong, all
    three backends are wrong together and will agree with each other.  Only
    audit_cell_semantics.py, which checks these functions against the SKY130
    Liberty data, can catch that.
    """

    @staticmethod
    def _c(x):
        if isinstance(x, bool):
            return "1'b1" if x else "1'b0"
        return x

    def AND(self, *a):
        a = [self._c(x) for x in a]
        return a[0] if len(a) == 1 else "(" + " & ".join(a) + ")"

    def OR(self, *a):
        a = [self._c(x) for x in a]
        return a[0] if len(a) == 1 else "(" + " | ".join(a) + ")"

    def NOT(self, a):
        return "(~" + self._c(a) + ")"

    def XOR(self, *a):
        a = [self._c(x) for x in a]
        return a[0] if len(a) == 1 else "(" + " ^ ".join(a) + ")"

    def MUX(self, s, a0, a1):
        return f"({self._c(s)} ? {self._c(a1)} : {self._c(a0)})"


# --- combinational cells: base name -> (input pins, output pin, function) ----

def _simple(inputs, output, fn):
    return {"inputs": inputs, "outputs": [output], "fn": fn, "seq": None}


COMB = {}


def _def(base, inputs, output, fn):
    COMB[base] = _simple(inputs, output, fn)


# buffers / inverters
_def("buf", ["A"], "X", lambda o, p: p["A"])
_def("bufinv", ["A"], "Y", lambda o, p: o.NOT(p["A"]))
_def("inv", ["A"], "Y", lambda o, p: o.NOT(p["A"]))
_def("clkbuf", ["A"], "X", lambda o, p: p["A"])
_def("clkinv", ["A"], "Y", lambda o, p: o.NOT(p["A"]))
_def("dlygate4sd1", ["A"], "X", lambda o, p: p["A"])
_def("dlygate4sd2", ["A"], "X", lambda o, p: p["A"])
_def("dlygate4sd3", ["A"], "X", lambda o, p: p["A"])
_def("dlymetal6s2s", ["A"], "X", lambda o, p: p["A"])
_def("dlymetal6s4s", ["A"], "X", lambda o, p: p["A"])
_def("dlymetal6s6s", ["A"], "X", lambda o, p: p["A"])
_def("bufbuf", ["A"], "X", lambda o, p: p["A"])
_def("clkdlybuf4s15", ["A"], "X", lambda o, p: p["A"])
_def("clkdlybuf4s18", ["A"], "X", lambda o, p: p["A"])
_def("clkdlybuf4s25", ["A"], "X", lambda o, p: p["A"])
_def("clkdlybuf4s50", ["A"], "X", lambda o, p: p["A"])
_def("clkinvlp", ["A"], "Y", lambda o, p: o.NOT(p["A"]))
_def("probe_p", ["A"], "X", lambda o, p: p["A"])
_def("probec_p", ["A"], "X", lambda o, p: p["A"])
_def("lpflow_clkbufkapwr", ["A"], "X", lambda o, p: p["A"])
_def("lpflow_clkinvkapwr", ["A"], "Y", lambda o, p: o.NOT(p["A"]))
_def("lpflow_lsbuf_lh_isowell", ["A"], "X", lambda o, p: p["A"])
_def("lpflow_lsbuf_lh_isowell_tap", ["A"], "X", lambda o, p: p["A"])
_def("lpflow_lsbuf_lh_hl_isowell_tap", ["A"], "X", lambda o, p: p["A"])

# low-power isolation cells: functions taken from Liberty and verified there
_def("lpflow_isobufsrc", ["A", "SLEEP"], "X",
     lambda o, p: o.AND(p["A"], o.NOT(p["SLEEP"])))
_def("lpflow_isobufsrckapwr", ["A", "SLEEP"], "X",
     lambda o, p: o.AND(p["A"], o.NOT(p["SLEEP"])))
_def("lpflow_inputiso0p", ["A", "SLEEP"], "X",
     lambda o, p: o.AND(o.NOT(p["SLEEP"]), p["A"]))
_def("lpflow_inputiso0n", ["A", "SLEEP_B"], "X",
     lambda o, p: o.AND(p["SLEEP_B"], p["A"]))
_def("lpflow_inputiso1p", ["A", "SLEEP"], "X",
     lambda o, p: o.OR(p["A"], p["SLEEP"]))
_def("lpflow_inputiso1n", ["A", "SLEEP_B"], "X",
     lambda o, p: o.OR(p["A"], o.NOT(p["SLEEP_B"])))
COMB["macro_sparecell"] = {"inputs": [], "outputs": ["LO"], "seq": None,
                           "multi": True, "fn": lambda o, p: {"LO": False}}
COMB["conb"] = {"inputs": [], "outputs": ["HI", "LO"], "seq": None, "multi": True,
                "fn": lambda o, p: {"HI": True, "LO": False}}

# basic gates (2..4 input)
for _n in (2, 3, 4):
    _ins = ["A", "B", "C", "D"][:_n]
    _def(f"and{_n}", list(_ins), "X",
         lambda o, p, i=tuple(_ins): o.AND(*[p[k] for k in i]))
    _def(f"or{_n}", list(_ins), "X",
         lambda o, p, i=tuple(_ins): o.OR(*[p[k] for k in i]))
    _def(f"nand{_n}", list(_ins), "Y",
         lambda o, p, i=tuple(_ins): o.NOT(o.AND(*[p[k] for k in i])))
    _def(f"nor{_n}", list(_ins), "Y",
         lambda o, p, i=tuple(_ins): o.NOT(o.OR(*[p[k] for k in i])))

_def("xor2", ["A", "B"], "X", lambda o, p: o.XOR(p["A"], p["B"]))
_def("xnor2", ["A", "B"], "Y", lambda o, p: o.NOT(o.XOR(p["A"], p["B"])))
_def("xor3", ["A", "B", "C"], "X", lambda o, p: o.XOR(p["A"], p["B"], p["C"]))
# NB: xnor2's output pin is Y but xnor3's is X.  Liberty-verified.
_def("xnor3", ["A", "B", "C"], "X",
     lambda o, p: o.NOT(o.XOR(p["A"], p["B"], p["C"])))

# muxes
_def("mux2", ["A0", "A1", "S"], "X", lambda o, p: o.MUX(p["S"], p["A0"], p["A1"]))
_def("mux2i", ["A0", "A1", "S"], "Y",
     lambda o, p: o.NOT(o.MUX(p["S"], p["A0"], p["A1"])))
_def("mux4", ["A0", "A1", "A2", "A3", "S0", "S1"], "X",
     lambda o, p: o.MUX(p["S1"], o.MUX(p["S0"], p["A0"], p["A1"]),
                        o.MUX(p["S0"], p["A2"], p["A3"])))

# majority / full adder style
_def("maj3", ["A", "B", "C"], "X",
     lambda o, p: o.OR(o.AND(p["A"], p["B"]), o.AND(p["A"], p["C"]),
                       o.AND(p["B"], p["C"])))


def _fa(o, p):
    return {
        "SUM": o.XOR(p["A"], p["B"], p["CIN"]),
        "COUT": o.OR(o.AND(p["A"], p["B"]), o.AND(p["A"], p["CIN"]),
                     o.AND(p["B"], p["CIN"])),
    }


COMB["fa"] = {"inputs": ["A", "B", "CIN"], "outputs": ["SUM", "COUT"],
              "fn": _fa, "seq": None, "multi": True}


def _fah(o, p):
    """fah is fa with the carry-in pin named CI instead of CIN."""
    return {
        "SUM": o.XOR(p["A"], p["B"], p["CI"]),
        "COUT": o.OR(o.AND(p["A"], p["B"]), o.AND(p["A"], p["CI"]),
                     o.AND(p["B"], p["CI"])),
    }


COMB["fah"] = {"inputs": ["A", "B", "CI"], "outputs": ["SUM", "COUT"],
               "fn": _fah, "seq": None, "multi": True}


def _fahcin(o, p):
    """Full adder with an active-low carry-in: SUM is XNOR3, COUT is
    majority(A, B, !CIN).  Liberty-verified rather than assumed."""
    ci = o.NOT(p["CIN"])
    return {
        "SUM": o.NOT(o.XOR(p["A"], p["B"], p["CIN"])),
        "COUT": o.OR(o.AND(p["A"], p["B"]), o.AND(p["A"], ci),
                     o.AND(p["B"], ci)),
    }


COMB["fahcin"] = {"inputs": ["A", "B", "CIN"], "outputs": ["SUM", "COUT"],
                  "fn": _fahcin, "seq": None, "multi": True}


def _fahcon(o, p):
    """Full adder with an active-low carry-out."""
    return {
        "SUM": o.XOR(p["A"], p["B"], p["CI"]),
        "COUT_N": o.NOT(o.OR(o.AND(p["A"], p["B"]), o.AND(p["A"], p["CI"]),
                             o.AND(p["B"], p["CI"]))),
    }


COMB["fahcon"] = {"inputs": ["A", "B", "CI"], "outputs": ["SUM", "COUT_N"],
                  "fn": _fahcon, "seq": None, "multi": True}


def _ha(o, p):
    return {"SUM": o.XOR(p["A"], p["B"]), "COUT": o.AND(p["A"], p["B"])}


COMB["ha"] = {"inputs": ["A", "B"], "outputs": ["SUM", "COUT"],
              "fn": _ha, "seq": None, "multi": True}

# --- compound AOI / OAI cells ----------------------------------------------
# Generated from the aXY.../oXY... naming convention so the whole family is
# covered without hand-writing each one.

_COMPOUND_RE = re.compile(r"^([ao])((?:\d+b*)+)(o|oi|a|ai)$")
_GROUP_RE = re.compile(r"(\d)(b*)")


def _compound_spec(base):
    """Return (input_pin_names, output_pin, builder) for a compound cell name.

    The digit/'b' string parses into groups: each digit is a group width and the
    'b's immediately after it mark that group's leading inputs as active-low.
    So a21bo -> groups (2, no inv) and (1, inverted) -> X = (A1 & A2) | !B1_N,
    and a2bb2o -> X = (!A1_N & !A2_N) | (B1 & B2).
    """
    m = _COMPOUND_RE.match(base)
    if not m:
        return None
    kind, groups_str, tail = m.groups()
    parsed = _GROUP_RE.findall(groups_str)
    if "".join(w + b for w, b in parsed) != groups_str:
        return None
    inverted = tail.endswith("i")
    out_pin = "Y" if inverted else "X"

    letters = "ABCD"
    groups_pins, inv_masks = [], []
    for gi, (w, bs) in enumerate(parsed):
        w, n_inv = int(w), len(bs)
        if n_inv > w:
            return None
        L = letters[gi]
        pins = [f"{L}{k + 1}" for k in range(w)]
        mask = [k < n_inv for k in range(w)]
        pins = [p + "_N" if mk else p for p, mk in zip(pins, mask)]
        groups_pins.append(pins)
        inv_masks.append(mask)

    all_pins = [p for g in groups_pins for p in g]

    def build(o, p, groups_pins=groups_pins, inv_masks=inv_masks, kind=kind,
              inverted=inverted):
        inner = o.AND if kind == "a" else o.OR
        outer = o.OR if kind == "a" else o.AND
        terms = []
        for g, mask in zip(groups_pins, inv_masks):
            vals = [o.NOT(p[q]) if mk else p[q] for q, mk in zip(g, mask)]
            terms.append(inner(*vals) if len(vals) > 1 else vals[0])
        r = outer(*terms) if len(terms) > 1 else terms[0]
        return o.NOT(r) if inverted else r

    return all_pins, out_pin, build


# --- and/or/nand/nor with active-low leading inputs (and2b, nor4bb, ...) -----

_BFAM_RE = re.compile(r"^(and|or|nand|nor)(\d)(b+)$")


def _bfamily_spec(base):
    """and2b / nand4bb / or2b / nor4bb ...

    The position of the active-low inputs is NOT symmetric between the families,
    which is easy to get wrong and was wrong here until the Liberty audit caught
    it:

        and2b   A_N, B          nand4bb  A_N, B_N, C, D     leading inputs
        or2b    A,   B_N        nor4bb   A,   B,   C_N, D_N trailing inputs

    Verified exhaustively against the SKY130 Liberty functions by
    audit_cell_semantics.py.
    """
    m = _BFAM_RE.match(base)
    if not m:
        return None
    kind, w, bs = m.group(1), int(m.group(2)), len(m.group(3))
    if bs > w:
        return None
    letters = "ABCD"[:w]
    lead = kind in ("and", "nand")
    inv_at = set(range(bs)) if lead else set(range(w - bs, w))
    pins = [(L + "_N" if i in inv_at else L) for i, L in enumerate(letters)]
    inverted = kind in ("nand", "nor")
    out_pin = "Y" if inverted else "X"

    def build(o, p, pins=pins, inv_at=inv_at, kind=kind, inverted=inverted):
        vals = [o.NOT(p[q]) if i in inv_at else p[q]
                for i, q in enumerate(pins)]
        r = o.AND(*vals) if kind in ("and", "nand") else o.OR(*vals)
        return o.NOT(r) if inverted else r

    return pins, out_pin, build


# --- sequential cells -------------------------------------------------------
# seq spec: dict(clk=pin, d=pin, q=..., qn=..., rst=(pin, active_low),
#                set=(pin, active_low), en=pin, negedge=bool)

SEQ = {
    "dfxtp": dict(clk="CLK", d="D", q="Q"),
    "dfxbp": dict(clk="CLK", d="D", q="Q", qn="Q_N"),
    "dfrtp": dict(clk="CLK", d="D", q="Q", rst=("RESET_B", True)),
    "dfrtn": dict(clk="CLK_N", d="D", q="Q", rst=("RESET_B", True), negedge=True),
    "dfrbp": dict(clk="CLK", d="D", q="Q", qn="Q_N", rst=("RESET_B", True)),
    "dfstp": dict(clk="CLK", d="D", q="Q", set=("SET_B", True)),
    "dfsbp": dict(clk="CLK", d="D", q="Q", qn="Q_N", set=("SET_B", True)),
    "dfbbn": dict(clk="CLK_N", d="D", q="Q", qn="Q_N", rst=("RESET_B", True),
                  set=("SET_B", True), negedge=True),
    "dfbbp": dict(clk="CLK", d="D", q="Q", qn="Q_N", rst=("RESET_B", True),
                  set=("SET_B", True)),
    "edfxtp": dict(clk="CLK", d="D", q="Q", en="DE"),
    "edfxbp": dict(clk="CLK", d="D", q="Q", qn="Q_N", en="DE"),
    "sdfxtp": dict(clk="CLK", d="D", q="Q", scan=("SCD", "SCE")),
    "sdfxbp": dict(clk="CLK", d="D", q="Q", qn="Q_N", scan=("SCD", "SCE")),
    "sdfrtp": dict(clk="CLK", d="D", q="Q", rst=("RESET_B", True),
                   scan=("SCD", "SCE")),
    "sdfrbp": dict(clk="CLK", d="D", q="Q", qn="Q_N", rst=("RESET_B", True),
                   scan=("SCD", "SCE")),
    "sdfstp": dict(clk="CLK", d="D", q="Q", set=("SET_B", True),
                   scan=("SCD", "SCE")),
    "sdfbbp": dict(clk="CLK", d="D", q="Q", qn="Q_N", rst=("RESET_B", True),
                   set=("SET_B", True), scan=("SCD", "SCE")),
    "sdfrtn": dict(clk="CLK_N", d="D", q="Q", rst=("RESET_B", True),
                   scan=("SCD", "SCE"), negedge=True),
    "sdfbbn": dict(clk="CLK_N", d="D", q="Q", qn="Q_N",
                   rst=("RESET_B", True), set=("SET_B", True),
                   scan=("SCD", "SCE"), negedge=True),
    "sdfsbp": dict(clk="CLK", d="D", q="Q", qn="Q_N", set=("SET_B", True),
                   scan=("SCD", "SCE")),
    "sedfxtp": dict(clk="CLK", d="D", q="Q", en="DE", scan=("SCD", "SCE")),
    "sedfxbp": dict(clk="CLK", d="D", q="Q", qn="Q_N", en="DE",
                    scan=("SCD", "SCE")),
    "dlxtp": dict(latch=True, clk="GATE", d="D", q="Q"),
    "dlrtp": dict(latch=True, clk="GATE", d="D", q="Q", rst=("RESET_B", True)),
    "dlxtn": dict(latch=True, clk="GATE_N", d="D", q="Q", negedge=True),
    "dlxbp": dict(latch=True, clk="GATE", d="D", q="Q", qn="Q_N"),
    "dlxbn": dict(latch=True, clk="GATE_N", d="D", q="Q", qn="Q_N",
                  negedge=True),
    "dlrbp": dict(latch=True, clk="GATE", d="D", q="Q", qn="Q_N",
                  rst=("RESET_B", True)),
    "dlrbn": dict(latch=True, clk="GATE_N", d="D", q="Q", qn="Q_N",
                  rst=("RESET_B", True), negedge=True),
    "dlrtn": dict(latch=True, clk="GATE_N", d="D", q="Q",
                  rst=("RESET_B", True), negedge=True),
    "lpflow_inputisolatch": dict(latch=True, clk="SLEEP_B", d="D", q="Q"),
}

PREFIX = "sky130_fd_sc_hd__"

# Cells we deliberately refuse to model, with the reason.  Modelling these
# wrongly is far more dangerous than not modelling them, because the resulting
# circuit would look valid and simulate happily.  check_cells.py and
# validate_netlist.py turn any occurrence into a hard failure.
KNOWN_UNSUPPORTED = {
    "ebufn": "tristate buffer: drives a shared net, which breaks the "
             "one-driver-per-net assumption the whole extractor relies on",
    "einvn": "tristate inverter: same multi-driver problem",
    "einvp": "tristate inverter: same multi-driver problem",
    "dlclkp": "integrated clock gate: Liberty describes it with a statetable "
              "rather than a boolean function, so no model can be verified "
              "against an authoritative source",
    "sdlclkp": "integrated clock gate with scan: same statetable problem",
}

# Physical-only cells that carry no logic and are correctly ignored.
PHYSICAL_ONLY = {
    "decap", "diode", "lpflow_bleeder", "lpflow_decapkapwr",
    "tap", "tapvpwrvgnd", "fill", "fillcap", "macro_sparecell",
}


def unsupported_reason(cell):
    """Return why a cell is refused, or None if it is fine."""
    return KNOWN_UNSUPPORTED.get(base_name(cell))


def base_name(cell):
    """sky130_fd_sc_hd__nand2_2 -> nand2"""
    n = cell[len(PREFIX):] if cell.startswith(PREFIX) else cell
    return re.sub(r"_\d+$", "", n)


_cache = {}


def get_model(cell):
    """Return a model dict for a cell, or None if unknown."""
    base = base_name(cell)
    if base in _cache:
        return _cache[base]
    model = None
    if base in SEQ:
        s = dict(SEQ[base])
        ins = [s["clk"], s["d"]]
        for key in ("rst", "set"):
            if key in s:
                ins.append(s[key][0])
        if "en" in s:
            ins.append(s["en"])
        if "scan" in s:
            ins.extend(s["scan"])
        outs = [s["q"]] + ([s["qn"]] if "qn" in s else [])
        model = {"inputs": ins, "outputs": outs, "fn": None, "seq": s}
    elif base in COMB:
        model = COMB[base]
    else:
        spec = _compound_spec(base) or _bfamily_spec(base)
        if spec:
            pins, out, build = spec
            model = _simple(pins, out, build)
    _cache[base] = model
    return model
