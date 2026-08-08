"""Gate-level circuit model + simulator built from a recovered netlist JSON.

The same evaluation code runs over concrete booleans (simulation) and over z3
expressions (bounded model checking), by swapping the `ops` backend.

Clocking is handled generically rather than by assuming which net is the clock:
each cycle evaluates the combinational cone twice, once with the clock port low
and once high, and every flop that sees a rising edge on *its own* CLK net
captures the D value that was stable before the edge.  Inverted clock trees and
gated clocks therefore need no special casing.
"""

import collections
import json

import cells as cl


class Circuit:
    def __init__(self, data):
        self.data = data
        self.insts = data["instances"]
        self.models = []
        for r in self.insts:
            m = cl.get_model(r["cell"])
            if m is None:
                raise ValueError(f"no model for cell {r['cell']}")
            self.models.append(m)

        # driver map: net -> (inst_idx, pin) for cell outputs
        self.driver = {}
        for i, (r, m) in enumerate(zip(self.insts, self.models)):
            for out in m["outputs"]:
                net = r["pins"].get(out)
                if net is None:
                    continue
                if net in self.driver:
                    raise ValueError(f"net {net} multiply driven")
                self.driver[net] = (i, out)

        all_nets = set()
        for r in self.insts:
            all_nets.update(r["pins"].values())
        self.nets = sorted(all_nets)

        # tie-offs to the power rails are constants, not primary inputs
        self.constants = {n: bool(v)
                          for n, v in (data.get("constants") or {}).items()
                          if n in all_nets}

        # a net with no cell driver and no constant value is a primary input
        self.inputs = sorted(n for n in self.nets
                             if n not in self.driver and n not in self.constants)
        self.outputs = sorted(
            n for n in data["ports"] if n in self.driver
        )

        self.flops = [i for i, m in enumerate(self.models) if m["seq"]]
        self.comb = [i for i, m in enumerate(self.models) if not m["seq"]]
        self._topo()

    def _topo(self):
        """Topologically order combinational instances."""
        state_nets = set()
        for i in self.flops:
            m, r = self.models[i], self.insts[i]
            for out in m["outputs"]:
                if out in r["pins"]:
                    state_nets.add(r["pins"][out])
        known = set(self.inputs) | state_nets | set(self.constants)

        # Kahn's algorithm over the combinational cells, so ordering stays
        # linear in the netlist size rather than quadratic.
        consumers = collections.defaultdict(list)
        remaining = {}
        ready = []
        for i in self.comb:
            r, m = self.insts[i], self.models[i]
            need = {r["pins"][p] for p in m["inputs"] if p in r["pins"]}
            need = {n for n in need if n not in known}
            remaining[i] = len(need)
            for n in need:
                consumers[n].append(i)
            if not need:
                ready.append(i)

        order = []
        while ready:
            i = ready.pop()
            order.append(i)
            r, m = self.insts[i], self.models[i]
            for out in m["outputs"]:
                net = r["pins"].get(out)
                if net is None:
                    continue
                for j in consumers.get(net, ()):
                    remaining[j] -= 1
                    if remaining[j] == 0:
                        ready.append(j)

        if len(order) != len(self.comb):
            stuck = [i for i in self.comb if remaining[i] > 0]
            raise ValueError(
                f"combinational loop involving {len(stuck)} cells, e.g. "
                f"{[self.insts[i]['name'] for i in stuck[:5]]}")
        self.order = order

    # ---------------------------------------------------------------- eval
    def eval_comb(self, ops, values, wrap=None):
        """values: dict net->value, pre-seeded with inputs and flop outputs.

        `wrap`, if given, is applied to every computed net value.  Symbolic
        backends use it to introduce a fresh named variable per net (Tseitin
        style) so unrolled formulas stay shallow instead of nesting to the depth
        of the logic cone.
        """
        for i in self.order:
            r, m = self.insts[i], self.models[i]
            p = {}
            for pin in m["inputs"]:
                net = r["pins"].get(pin)
                p[pin] = values[net] if net is not None else False
            res = m["fn"](ops, p)
            if m.get("multi"):
                for out, val in res.items():
                    net = r["pins"].get(out)
                    if net is not None:
                        values[net] = wrap(net, val) if wrap else val
            else:
                net = r["pins"].get(m["outputs"][0])
                if net is not None:
                    values[net] = wrap(net, res) if wrap else res
        return values

    def _seed(self, ops, values, inputs, state):
        for n, v in self.constants.items():
            values[n] = v
        for n in self.inputs:
            values[n] = inputs.get(n, False)
        for i in self.flops:
            r, m, s = self.insts[i], self.models[i], self.models[i]["seq"]
            q = r["pins"].get(s["q"])
            if q is not None:
                values[q] = state[i]
            if "qn" in s and s["qn"] in r["pins"]:
                values[r["pins"][s["qn"]]] = ops.NOT(state[i])
        return values

    def reset_state(self):
        return {i: False for i in self.flops}

    def step(self, ops, inputs, state, clock_port="clk", wrap=None):
        """Advance one full clock cycle. Returns (new_state, settled values)."""
        lo = dict(inputs)
        lo[clock_port] = False
        v_lo = self.eval_comb(ops, self._seed(ops, {}, lo, state), wrap)
        state = self._apply_async(ops, v_lo, state)
        v_lo = self.eval_comb(ops, self._seed(ops, {}, lo, state), wrap)

        hi = dict(inputs)
        hi[clock_port] = True
        v_hi = self.eval_comb(ops, self._seed(ops, {}, hi, state), wrap)

        new = dict(state)
        for i in self.flops:
            r, s = self.insts[i], self.models[i]["seq"]
            clk_net = r["pins"].get(s["clk"])
            before = v_lo.get(clk_net, False)
            after = v_hi.get(clk_net, False)
            edge = ops.AND(ops.NOT(before), after)
            if s.get("negedge"):
                edge = ops.AND(before, ops.NOT(after))
            # Order matters: on scan-enabled flops the Liberty next_state is
            # (D & DE & !SCE) | (IQ & !DE & !SCE) | (SCD & SCE), i.e. the scan
            # mux OVERRIDES the data enable.  Applying scan first and enable
            # second would let DE=0 defeat SCE=1 and silently hold instead of
            # loading the scan value.
            d = v_lo.get(r["pins"].get(s["d"]), False)
            if "en" in s and s["en"] in r["pins"]:
                d = ops.MUX(v_lo.get(r["pins"][s["en"]], False), state[i], d)
            if "scan" in s and s["scan"][1] in r["pins"]:
                scd = v_lo.get(r["pins"][s["scan"][0]], False)
                sce = v_lo.get(r["pins"][s["scan"][1]], False)
                d = ops.MUX(sce, d, scd)
            new[i] = ops.MUX(edge, state[i], d)
        new = self._apply_async(ops, v_hi, new)
        if wrap:
            new = {i: wrap(f"ff{i}", v) for i, v in new.items()}
        v_out = self.eval_comb(ops, self._seed(ops, {}, hi, new), wrap)
        return new, v_out

    def _apply_async(self, ops, values, state):
        out = dict(state)
        for i in self.flops:
            r, s = self.insts[i], self.models[i]["seq"]
            if "rst" in s and s["rst"][0] in r["pins"]:
                pin, active_low = s["rst"]
                a = values.get(r["pins"][pin], True)
                asserted = ops.NOT(a) if active_low else a
                out[i] = ops.MUX(asserted, out[i], False)
            if "set" in s and s["set"][0] in r["pins"]:
                pin, active_low = s["set"]
                a = values.get(r["pins"][pin], True)
                asserted = ops.NOT(a) if active_low else a
                out[i] = ops.MUX(asserted, out[i], True)
        return out


def load_circuit(path):
    return Circuit(json.load(open(path)))


def summary(c):
    print(f"nets {len(c.nets)}  cells {len(c.insts)} "
          f"(comb {len(c.comb)}, flops {len(c.flops)})")
    print(f"primary inputs : {c.inputs}")
    print(f"driven ports   : {c.outputs}")
