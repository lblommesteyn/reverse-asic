"""Independently verify cells.py against the authoritative SKY130 Liberty data.

This is the only check in the toolkit that can catch a wrong cell model.  The
simulator, the z3 backend, the behavioural Verilog and the SMT export all derive
from cells.py, so their agreeing with each other proves nothing about whether
those definitions match the real standard cells.  The Liberty files shipped with
the SkyWater PDK are a genuinely independent source: they state each output's
boolean function, and for sequential cells the next_state / clocked_on / clear /
preset expressions.

Combinational cells are compared by exhaustive truth table.  Sequential cells are
compared by exhaustive transition table over every input combination and both
values of the stored bit, plus clock pin and edge, and async clear/preset pin and
polarity.

Usage:
  python scripts/audit_cell_semantics.py [--pdk pdk/sky130hd] [--json out.json]
"""

import argparse
import glob
import itertools
import json
import os
import re
import sys

import cells as cl

PREFERRED_CORNER = "tt_025C_1v80"


# --------------------------------------------------------------- liberty ---

class LibertyExpr:
    """Parser/evaluator for Liberty boolean function syntax.

    Operators, tightest first:
        '   postfix invert
        !   prefix invert
        ^   xor
        & * space   and
        | +  or
    """

    TOKEN = re.compile(r"\s*(\(|\)|[!'^&*|+]|[A-Za-z_][A-Za-z_0-9]*|1'b[01]|[01])")

    def __init__(self, text):
        self.text = text
        self.toks = []
        pos = 0
        while pos < len(text):
            m = self.TOKEN.match(text, pos)
            if not m:
                if text[pos].isspace():
                    pos += 1
                    continue
                raise ValueError(f"bad token at {pos} in {text!r}")
            self.toks.append(m.group(1))
            pos = m.end()
        self.i = 0
        self.ast = self._or()
        if self.i != len(self.toks):
            raise ValueError(f"trailing tokens in {text!r}")

    def _peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else None

    def _or(self):
        node = self._xor()
        while self._peek() in ("|", "+"):
            self.i += 1
            node = ("or", node, self._xor())
        return node

    def _xor(self):
        node = self._and()
        while self._peek() == "^":
            self.i += 1
            node = ("xor", node, self._and())
        return node

    def _and(self):
        node = self._unary()
        while True:
            t = self._peek()
            if t in ("&", "*"):
                self.i += 1
                node = ("and", node, self._unary())
            elif t is not None and (t == "(" or re.fullmatch(
                    r"[A-Za-z_][A-Za-z_0-9]*|1'b[01]|[01]", t)):
                # implicit AND from juxtaposition
                node = ("and", node, self._unary())
            else:
                return node

    def _unary(self):
        t = self._peek()
        if t == "!":
            self.i += 1
            return ("not", self._unary())
        if t == "(":
            self.i += 1
            node = self._or()
            if self._peek() != ")":
                raise ValueError(f"unbalanced parens in {self.text!r}")
            self.i += 1
        elif t in ("0", "1"):
            self.i += 1
            node = ("const", t == "1")
        elif t in ("1'b0", "1'b1"):
            self.i += 1
            node = ("const", t.endswith("1"))
        elif t is not None and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", t):
            self.i += 1
            node = ("var", t)
        else:
            raise ValueError(f"unexpected token {t!r} in {self.text!r}")
        while self._peek() == "'":
            self.i += 1
            node = ("not", node)
        return node

    def variables(self):
        out = set()

        def walk(n):
            if n[0] == "var":
                out.add(n[1])
            else:
                for c in n[1:]:
                    if isinstance(c, tuple):
                        walk(c)
        walk(self.ast)
        return out

    def eval(self, env):
        def ev(n):
            k = n[0]
            if k == "var":
                return bool(env[n[1]])
            if k == "const":
                return n[1]
            if k == "not":
                return not ev(n[1])
            if k == "and":
                return ev(n[1]) and ev(n[2])
            if k == "or":
                return ev(n[1]) or ev(n[2])
            if k == "xor":
                return ev(n[1]) != ev(n[2])
            raise ValueError(k)
        return ev(self.ast)


def load_liberty_cell(path):
    """Extract the pieces we care about from one cell's Liberty JSON."""
    d = json.load(open(path))
    info = {"pins": {}, "outputs": {}, "ff": None, "latch": None,
            "statetable": None}
    for k, v in d.items():
        if k.startswith("pin,"):
            name = k.split(",", 1)[1]
            direction = v.get("direction")
            info["pins"][name] = direction
            if direction == "output" and "function" in v:
                info["outputs"][name] = v["function"]
        elif k.startswith("ff,"):
            iq = k.split(",")[1:]
            info["ff"] = {"iq": iq[0], "iqn": iq[1] if len(iq) > 1 else None,
                          **{kk: vv for kk, vv in v.items()
                             if isinstance(vv, str)}}
        elif k.startswith("latch,"):
            iq = k.split(",")[1:]
            info["latch"] = {"iq": iq[0], "iqn": iq[1] if len(iq) > 1 else None,
                             **{kk: vv for kk, vv in v.items()
                                if isinstance(vv, str)}}
        elif k.startswith("statetable"):
            info["statetable"] = v
    return info


def find_cells(pdk_root):
    """cell base name -> (full cell name, liberty json path)."""
    out = {}
    for cell_dir in sorted(glob.glob(os.path.join(pdk_root, "cells", "*"))):
        base = os.path.basename(cell_dir)
        libs = sorted(glob.glob(os.path.join(
            cell_dir, f"*__{PREFERRED_CORNER}.lib.json")))
        libs = [p for p in libs if "ccsnoise" not in p]
        if not libs:
            continue
        path = libs[0]
        name = os.path.basename(path).split("__" + PREFERRED_CORNER)[0]
        out[base] = (name, path)
    return out


# ----------------------------------------------------------------- audit ---

def audit_combinational(name, model, lib):
    """Exhaustive truth-table comparison.  Returns list of problem strings."""
    problems = []
    lib_in = sorted(p for p, d in lib["pins"].items() if d == "input")
    lib_out = sorted(lib["outputs"])
    my_in = sorted(model["inputs"])
    my_out = sorted(model["outputs"])

    if my_in != lib_in:
        problems.append(f"input pins {my_in} != liberty {lib_in}")
    if my_out != lib_out:
        problems.append(f"output pins {my_out} != liberty {lib_out}")
    if problems:
        return problems

    exprs = {}
    for o, fn in lib["outputs"].items():
        try:
            exprs[o] = LibertyExpr(fn)
        except Exception as e:
            problems.append(f"cannot parse liberty function for {o}: "
                            f"{fn!r} ({e})")
    if problems:
        return problems

    ops = cl.BoolOps()
    n = len(lib_in)
    if n > 16:
        return [f"skipped: {n} inputs is too many for exhaustive check"]
    for bits in itertools.product([False, True], repeat=n):
        env = dict(zip(lib_in, bits))
        res = model["fn"](ops, dict(env))
        mine = res if model.get("multi") else {model["outputs"][0]: res}
        for o, expr in exprs.items():
            want = expr.eval(env)
            got = bool(mine.get(o))
            if got != want:
                vec = " ".join(f"{k}={int(v)}" for k, v in sorted(env.items()))
                problems.append(
                    f"{o} mismatch at [{vec}]: ours={int(got)} "
                    f"liberty={int(want)} (function {lib['outputs'][o]!r})")
                return problems
    return problems


def my_next_state(model, env, iq):
    """Compute our model's next stored value, mirroring sim.py's step()."""
    ops = cl.BoolOps()
    s = model["seq"]
    d = bool(env.get(s["d"], False))
    if "en" in s:
        de = bool(env.get(s["en"], False))
        d = d if de else iq
    if "scan" in s:
        scd = bool(env.get(s["scan"][0], False))
        sce = bool(env.get(s["scan"][1], False))
        d = scd if sce else d
    return d


def audit_sequential(name, model, lib):
    problems = []
    grp = lib["ff"] or lib["latch"]
    is_latch = lib["latch"] is not None
    s = model["seq"]

    lib_in = sorted(p for p, d in lib["pins"].items() if d == "input")
    my_in = sorted(model["inputs"])
    if my_in != lib_in:
        problems.append(f"input pins {my_in} != liberty {lib_in}")

    lib_out = sorted(p for p, d in lib["pins"].items() if d == "output")
    my_out = sorted(model["outputs"])
    if my_out != lib_out:
        problems.append(f"output pins {my_out} != liberty {lib_out}")

    if bool(s.get("latch")) != is_latch:
        problems.append(f"latch flag {bool(s.get('latch'))} but liberty says "
                        f"{'latch' if is_latch else 'ff'}")

    # --- clock pin and edge ------------------------------------------------
    clocked = grp.get("clocked_on") or grp.get("enable")
    if clocked:
        try:
            e = LibertyExpr(clocked)
            vs = e.variables()
            if len(vs) != 1:
                problems.append(f"clock expression {clocked!r} has {len(vs)} vars")
            else:
                pin = next(iter(vs))
                # negedge iff the expression inverts the pin
                neg = not e.eval({pin: True})
                if s["clk"] != pin:
                    problems.append(f"clock pin {s['clk']} != liberty {pin}")
                if bool(s.get("negedge")) != neg:
                    problems.append(
                        f"clock edge: ours="
                        f"{'neg' if s.get('negedge') else 'pos'} "
                        f"liberty={'neg' if neg else 'pos'} ({clocked!r})")
        except Exception as ex:
            problems.append(f"cannot parse clocked_on {clocked!r}: {ex}")

    # --- async clear / preset ---------------------------------------------
    for lib_key, my_key, label in (("clear", "rst", "reset"),
                                   ("preset", "set", "set")):
        lib_expr = grp.get(lib_key)
        mine = s.get(my_key)
        if lib_expr and not mine:
            problems.append(f"liberty has {lib_key}={lib_expr!r} but our model "
                            f"has no {label}")
            continue
        if mine and not lib_expr:
            problems.append(f"our model has {label}={mine} but liberty has no "
                            f"{lib_key}")
            continue
        if not lib_expr:
            continue
        try:
            e = LibertyExpr(lib_expr)
            vs = e.variables()
            if len(vs) != 1:
                problems.append(f"{lib_key} {lib_expr!r} has {len(vs)} vars")
                continue
            pin = next(iter(vs))
            active_low = not e.eval({pin: True})
            if mine[0] != pin:
                problems.append(f"{label} pin {mine[0]} != liberty {pin}")
            if bool(mine[1]) != active_low:
                problems.append(
                    f"{label} polarity: ours="
                    f"{'low' if mine[1] else 'high'} "
                    f"liberty={'low' if active_low else 'high'}")
        except Exception as ex:
            problems.append(f"cannot parse {lib_key} {lib_expr!r}: {ex}")

    # --- next_state / data_in over the full transition table --------------
    ns = grp.get("next_state") or grp.get("data_in")
    if ns:
        try:
            e = LibertyExpr(ns)
        except Exception as ex:
            problems.append(f"cannot parse next_state {ns!r}: {ex}")
            e = None
        if e is not None:
            iq_name = grp["iq"]
            data_pins = sorted(v for v in e.variables() if v != iq_name)
            unknown = [p for p in data_pins if p not in lib["pins"]]
            if unknown:
                problems.append(f"next_state references unknown pins {unknown}")
            else:
                for bits in itertools.product([False, True],
                                              repeat=len(data_pins)):
                    env = dict(zip(data_pins, bits))
                    for iq in (False, True):
                        env2 = dict(env)
                        env2[iq_name] = iq
                        want = e.eval(env2)
                        got = my_next_state(model, env, iq)
                        if got != want:
                            vec = " ".join(f"{k}={int(v)}"
                                           for k, v in sorted(env.items()))
                            problems.append(
                                f"next_state mismatch at [{vec} IQ={int(iq)}]: "
                                f"ours={int(got)} liberty={int(want)} "
                                f"({ns!r})")
                            break
                    if problems:
                        break

    # --- Q / Q_N polarity ---------------------------------------------------
    iq_name = grp["iq"]
    iqn_name = grp.get("iqn")
    for pin, fn in lib["outputs"].items():
        expected_q = (fn.strip() == iq_name)
        expected_qn = iqn_name and fn.strip() == iqn_name
        if expected_q and s.get("q") != pin:
            problems.append(f"Q pin: ours={s.get('q')} liberty says {pin}={fn}")
        if expected_qn and s.get("qn") != pin:
            problems.append(f"Q_N pin: ours={s.get('qn')} liberty says "
                            f"{pin}={fn}")
    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdk", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "pdk", "sky130hd"))
    ap.add_argument("--json", default=None)
    ap.add_argument("--only", default=None, help="audit one cell base name")
    a = ap.parse_args()

    if not os.path.isdir(os.path.join(a.pdk, "cells")):
        print(f"FAIL: no SKY130 library at {a.pdk}")
        print("  clone it with:")
        print("  git clone --depth 1 --filter=blob:none --sparse \\")
        print("    https://github.com/google/skywater-pdk-libs-sky130_fd_sc_hd"
              " pdk/sky130hd")
        print("  (cd pdk/sky130hd && git sparse-checkout set cells timing)")
        return 2

    found = find_cells(a.pdk)
    if a.only:
        found = {k: v for k, v in found.items() if k == a.only}

    verified, mismatched, unverified, unsupported = [], [], [], []
    details = {}

    for base, (name, path) in sorted(found.items()):
        model = cl.get_model(name)
        if model is None:
            unsupported.append(base)
            continue
        try:
            lib = load_liberty_cell(path)
        except Exception as e:
            unverified.append((base, f"cannot load liberty: {e}"))
            continue

        if lib["ff"] or lib["latch"]:
            if not model["seq"]:
                mismatched.append((base, ["we model it as combinational but "
                                          "liberty defines a flop/latch"]))
                continue
            probs = audit_sequential(name, model, lib)
        elif lib["outputs"]:
            if model["seq"]:
                mismatched.append((base, ["we model it as sequential but "
                                          "liberty gives a boolean function"]))
                continue
            probs = audit_combinational(name, model, lib)
        else:
            unverified.append((base, "liberty has no function or ff group"))
            continue

        if not probs:
            verified.append(base)
        elif any(p.startswith("skipped") for p in probs):
            unverified.append((base, probs[0]))
        else:
            mismatched.append((base, probs))
        details[base] = {"cell": name, "problems": probs}

    print("CELL SEMANTICS AUDIT")
    print(f"  library cells present   : {len(found)}")
    print(f"  supported models        : "
          f"{len(verified) + len(mismatched) + len(unverified)}")
    print(f"  independently verified  : {len(verified)}")
    print(f"  mismatches              : {len(mismatched)}")
    print(f"  unverified              : {len(unverified)}")
    print(f"  not modelled (ignored)  : {len(unsupported)}")

    if mismatched:
        print("\nMISMATCHES")
        for base, probs in mismatched:
            print(f"  {base}")
            for p in probs[:4]:
                print(f"      {p}")
    if unverified:
        print("\nUNVERIFIED")
        for base, why in unverified:
            print(f"  {base}: {why}")

    if a.json:
        json.dump({
            "verified": verified,
            "mismatched": {b: p for b, p in mismatched},
            "unverified": {b: w for b, w in unverified},
            "not_modelled": unsupported,
            "details": details,
        }, open(a.json, "w"), indent=1)
        print(f"\nwrote {a.json}")

    return 1 if (mismatched or unverified) else 0


if __name__ == "__main__":
    sys.exit(main())
