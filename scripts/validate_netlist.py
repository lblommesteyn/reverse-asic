"""Invariant checks on a recovered netlist: PASS / WARN / FAIL.

Extraction from geometry can fail silently.  A single missed via splits one net
into two, which does not crash anything: it produces a circuit that simulates
happily and is wrong.  These checks are the defence against that, so run this
before trusting any simulation or SAT result.

Severity policy:
  FAIL  the netlist is definitely wrong or unusable
  WARN  legitimate in some designs, but worth eyeballing
  PASS  invariant held

Usage: python scripts/validate_netlist.py recovered.json [--json out.json]
"""

import argparse
import collections
import json

import cells as cl
from sim import Circuit

CLOCK_PINS = {"CLK", "CLK_N", "GATE", "GATE_N"}
RESET_PINS = {"RESET_B", "SET_B"}


class Report:
    def __init__(self):
        self.items = []

    def add(self, level, check, msg, detail=None):
        self.items.append({"level": level, "check": check, "message": msg,
                           "detail": detail or {}})

    def worst(self):
        for lv in ("FAIL", "WARN"):
            if any(i["level"] == lv for i in self.items):
                return lv
        return "PASS"

    def render(self):
        order = {"FAIL": 0, "WARN": 1, "PASS": 2}
        lines = []
        for it in sorted(self.items, key=lambda i: (order[i["level"]],
                                                    i["check"])):
            lines.append(f"[{it['level']:4s}] {it['check']:28s} {it['message']}")
            d = it["detail"]
            for k in ("examples", "list"):
                if d.get(k):
                    for e in d[k][:10]:
                        lines.append(f"           {e}")
                    if len(d[k]) > 10:
                        lines.append(f"           ... {len(d[k]) - 10} more")
        return "\n".join(lines)


def classify_nets(data):
    """Split nets into clock / reset / constant / signal, with fanout counts."""
    insts = data["instances"]
    clock_nets, reset_nets = collections.Counter(), collections.Counter()
    fanout = collections.Counter()
    sinks = collections.defaultdict(list)
    drivers = collections.defaultdict(list)

    for r in insts:
        m = cl.get_model(r["cell"])
        outs = set(m["outputs"]) if m else set()
        for pin, net in r["pins"].items():
            if pin in outs:
                drivers[net].append(f"{r['name']}.{pin}")
            else:
                sinks[net].append(f"{r['name']}.{pin}")
                fanout[net] += 1
                if pin in CLOCK_PINS:
                    clock_nets[net] += 1
                if pin in RESET_PINS:
                    reset_nets[net] += 1
    return {
        "fanout": fanout, "sinks": sinks, "drivers": drivers,
        "clock_nets": clock_nets, "reset_nets": reset_nets,
    }


def validate(path, out_json=None):
    data = json.load(open(path))
    rep = Report()
    diag = data.get("diagnostics", {})
    insts = data["instances"]

    # --- extraction-stage diagnostics -----------------------------------
    conn = diag.get("connectivity", {})
    nun = conn.get("total_unresolved_cuts")
    if nun is None:
        rep.add("WARN", "unresolved-vias", "no connectivity diagnostics present")
    elif nun == 0:
        rep.add("PASS", "unresolved-vias", "every via resolved to both layers")
    else:
        rep.add("FAIL", "unresolved-vias",
                f"{nun} vias could not be resolved; nets are split",
                {"examples": conn.get("unresolved_cut_examples", [])})

    npin = diag.get("unresolved_pin_count", 0)
    if npin:
        rep.add("FAIL", "unresolved-pins",
                f"{npin} cell pin labels did not land on any conductor",
                {"examples": diag.get("unresolved_pins", [])})
    else:
        rep.add("PASS", "unresolved-pins", "all cell pin labels resolved")

    phys = diag.get("physical_only_cells", {})
    if phys:
        rep.add("PASS", "physical-only-cells",
                f"{len(phys)} annotation-only leaf cells ignored (no geometry "
                f"on any electrical layer, so they cannot join a net)",
                {"examples": [f"{k} x{v['count']} layers {sorted(v['layers'])}"
                              for k, v in phys.items()]})

    unsup = diag.get("unsupported_cells", {})
    if unsup:
        rep.add("FAIL", "unsupported-cells",
                f"{len(unsup)} cell types have no logical model",
                {"examples": [f"{k} x{v['count']} pins {v['pins']}"
                              for k, v in unsup.items()]})
    else:
        rep.add("PASS", "unsupported-cells", "every cell type has a model")

    dis = diag.get("pin_label_disagreement_count", 0)
    if dis:
        rep.add("FAIL", "pin-label-agreement",
                f"{dis} pins have labels resolving to different nets; the "
                f"assigned net depends on label order and is not trustworthy",
                {"examples": diag.get("pin_label_disagreements", [])})
    else:
        rep.add("PASS", "pin-label-agreement",
                "every pin's labels agree on one net")

    refused = diag.get("refused_cells", {})
    if refused:
        rep.add("FAIL", "refused-cells",
                f"{len(refused)} cell types are deliberately unmodelled",
                {"examples": [f"{k} x{v['count']}: {v['reason']}"
                              for k, v in refused.items()]})
    else:
        rep.add("PASS", "refused-cells",
                "no tristate or statetable-only cells present")

    miss = diag.get("pins_missing_count", 0)
    if miss:
        rep.add("WARN", "pins-missing-in-layout",
                f"{miss} instances are missing a pin the model expects",
                {"examples": diag.get("pins_missing_from_layout", [])})
    else:
        rep.add("PASS", "pins-missing-in-layout",
                "every instance exposes all modelled pins")

    for p in diag.get("port_problems", []):
        rep.add("FAIL", "port-resolution", str(p))

    # --- structural invariants -------------------------------------------
    info = classify_nets(data)
    multi = {n: d for n, d in info["drivers"].items() if len(d) > 1}
    if multi:
        rep.add("FAIL", "single-driver",
                f"{len(multi)} nets have more than one driver (shorted nets)",
                {"examples": [f"{n}: {d}" for n, d in list(multi.items())[:10]]})
    else:
        rep.add("PASS", "single-driver", "every net has at most one driver")

    consts = set(data.get("constants", {}))
    ports = set(data.get("ports", []))
    undriven = [n for n in info["sinks"]
                if n not in info["drivers"] and n not in consts]
    unexpected = [n for n in undriven if n not in ports]
    if unexpected:
        rep.add("WARN", "undriven-nets",
                f"{len(unexpected)} nets drive inputs but have no driver and "
                f"are not ports (real primary inputs, or a broken connection)",
                {"list": sorted(unexpected)[:20]})
    else:
        rep.add("PASS", "undriven-nets",
                "every undriven net is a declared port or constant")

    dangling = [n for n in info["drivers"]
                if n not in info["sinks"] and n not in ports]
    if dangling:
        rep.add("WARN", "dangling-outputs",
                f"{len(dangling)} driven nets have no sink and are not ports",
                {"list": sorted(dangling)[:20]})
    else:
        rep.add("PASS", "dangling-outputs", "no orphaned cell outputs")

    # --- simulation-model limits -------------------------------------------
    # These cells extract fine but sim.py cannot represent them, so they must
    # be a hard failure rather than a silently wrong simulation.
    latches, negedge = [], []
    for r in insts:
        m = cl.get_model(r["cell"])
        s_ = (m or {}).get("seq")
        if not s_:
            continue
        if s_.get("latch"):
            latches.append(f"{r['name']} {r['cell']}")
        elif s_.get("negedge"):
            negedge.append(f"{r['name']} {r['cell']}")
    if latches:
        rep.add("FAIL", "level-sensitive-latches",
                f"{len(latches)} level-sensitive latches present; the "
                f"simulator models only edge-triggered storage, so a latch "
                f"would be simulated as permanently stuck",
                {"examples": latches[:10]})
    else:
        rep.add("PASS", "level-sensitive-latches", "no latches present")
    if negedge:
        rep.add("FAIL", "negedge-flops",
                f"{len(negedge)} falling-edge flops present; a clock cycle is "
                f"modelled as low-then-high, so a falling edge never occurs "
                f"and these flops would never capture data",
                {"examples": negedge[:10]})
    else:
        rep.add("PASS", "negedge-flops", "no falling-edge flops present")

    # --- clocks, resets, fanout -------------------------------------------
    n_flops = sum(1 for r in insts
                  if (cl.get_model(r["cell"]) or {}).get("seq"))
    clk = info["clock_nets"].most_common()
    if clk:
        top_clk, cnt = clk[0]
        covered = sum(c for _, c in clk)
        rep.add("PASS" if len(clk) <= 8 else "WARN", "clock-tree",
                f"{len(clk)} distinct clock nets drive {covered} clock pins; "
                f"largest '{top_clk}' drives {cnt}",
                {"list": [f"{n}: {c} clock pins" for n, c in clk[:10]]})
        if covered != n_flops:
            rep.add("WARN", "clock-coverage",
                    f"{covered} clock pins but {n_flops} sequential cells")
    else:
        rep.add("WARN" if n_flops else "PASS", "clock-tree",
                "no clock pins found")

    rst = info["reset_nets"].most_common()
    if rst:
        rep.add("PASS", "reset-tree",
                f"{len(rst)} reset/set nets driving "
                f"{sum(c for _, c in rst)} pins",
                {"list": [f"{n}: {c} pins" for n, c in rst[:10]]})

    hi = [(n, c) for n, c in info["fanout"].most_common(15)]
    rep.add("PASS", "high-fanout", "top fanout nets listed",
            {"list": [f"{n}: fanout {c}" for n, c in hi]})

    # --- can it even be built and levelised? -------------------------------
    try:
        c = Circuit(data)
        rep.add("PASS", "build-circuit",
                f"levelised {len(c.comb)} combinational cells, "
                f"{len(c.flops)} flops, {len(c.inputs)} primary inputs")
        if not c.outputs:
            rep.add("FAIL", "driven-outputs",
                    "no declared port is driven by a cell")
        else:
            rep.add("PASS", "driven-outputs", f"driven ports: {c.outputs}")
    except ValueError as e:
        rep.add("FAIL", "build-circuit", str(e))

    # --- hashes -------------------------------------------------------------
    h = data.get("hashes", {})
    if h:
        rep.add("PASS", "hashes",
                f"structural {h.get('structural_sha256', '')[:16]} "
                f"full {h.get('full_sha256', '')[:16]}")

    verdict = rep.worst()
    print(rep.render())
    print(f"\nOVERALL: {verdict}")
    print(f"cells {len(insts)}  nets {len(data['nets'])}  flops {n_flops}")

    if out_json:
        with open(out_json, "w") as f:
            json.dump({"verdict": verdict, "checks": rep.items}, f, indent=1)
        print(f"wrote {out_json}")
    return verdict


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("netlist")
    ap.add_argument("--json")
    a = ap.parse_args()
    v = validate(a.netlist, a.json)
    raise SystemExit(0 if v != "FAIL" else 1)
