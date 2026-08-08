"""Infer a design's input protocol from a VCD, without needing the netlist.

Answers the questions you need before running BMC: which signal is the clock and
on which edge, whether reset is active high or low and how long it is held, which
signals look like enables or strobes, which look like serial data, how many
cycles an attempt takes, and whether the data groups into 8- or 16-bit frames.

Emits a compact protocol_report.json plus a readable summary.  It reports only
derived timing statistics, not the waveform itself.

Usage: python scripts/vcd_analyze.py inputs.vcd --json protocol_report.json
"""

import argparse
import collections
import json
import math
import re


def parse_vcd(path):
    """Return (id->name, [(time, {name: value})]) with values as strings."""
    text = open(path).read()
    ids = {}
    widths = {}
    for m in re.finditer(
            r"\$var\s+\w+\s+(\d+)\s+(\S+)\s+([^\s$]+)\s*((?:\[[^\]]*\])?)\s*\$end",
            text):
        width, sid, name, rng = m.groups()
        ids[sid] = name + (rng or "")
        widths[name + (rng or "")] = int(width)

    body = text[text.find("$enddefinitions"):]
    changes = []
    time, cur = 0, {}
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("$"):
            continue
        if line.startswith("#"):
            if cur:
                changes.append((time, cur))
                cur = {}
            time = int(line[1:])
        elif line[0] in "01xzXZ" and len(line) > 1:
            sid = line[1:]
            if sid in ids:
                cur[ids[sid]] = line[0]
        elif line[0] in "bB":
            parts = line.split()
            if len(parts) == 2 and parts[1] in ids:
                cur[ids[parts[1]]] = parts[0][1:]
    if cur:
        changes.append((time, cur))
    return ids, widths, changes


def build_timeline(ids, changes):
    """name -> list of (time, value) transitions."""
    tl = collections.defaultdict(list)
    for t, delta in changes:
        for name, val in delta.items():
            tl[name].append((t, val))
    return tl


def detect_clock(tl):
    """The signal with the most regular, most numerous toggles."""
    best, best_score = None, None
    for name, trans in tl.items():
        vals = [v for _, v in trans]
        if len(trans) < 4 or set(vals) - {"0", "1"}:
            continue
        times = [t for t, _ in trans]
        gaps = [b - a for a, b in zip(times, times[1:])]
        if not gaps:
            continue
        mean = sum(gaps) / len(gaps)
        if mean <= 0:
            continue
        var = sum((g - mean) ** 2 for g in gaps) / len(gaps)
        regularity = math.sqrt(var) / mean
        score = (regularity, -len(trans))
        if best_score is None or score < best_score:
            best, best_score = name, score
    if best is None:
        return None
    times = [t for t, _ in tl[best]]
    gaps = [b - a for a, b in zip(times, times[1:])]
    half = min(gaps) if gaps else 0
    return {"name": best, "transitions": len(tl[best]),
            "half_period": half, "period": half * 2,
            "regularity_cv": round(best_score[0], 4)}


def sample_on_edge(tl, clock, names, edge="rising"):
    """Return [(time, {name: bit})] sampled at each clock edge."""
    events = sorted({t for tr in tl.values() for t, _ in tr})
    level = {n: "0" for n in tl}
    out = []
    prev = "0"
    idx = {n: 0 for n in tl}
    for t in events:
        for n in tl:
            while idx[n] < len(tl[n]) and tl[n][idx[n]][0] <= t:
                level[n] = tl[n][idx[n]][1]
                idx[n] += 1
        c = level.get(clock, "0")
        rise = prev == "0" and c == "1"
        fall = prev == "1" and c == "0"
        if (edge == "rising" and rise) or (edge == "falling" and fall):
            out.append((t, {n: level.get(n, "0") for n in names}))
        prev = c
    return out


def runs(seq):
    """[(value, start_index, length)] over a list."""
    out = []
    if not seq:
        return out
    cur, start = seq[0], 0
    for i, v in enumerate(seq[1:], 1):
        if v != cur:
            out.append((cur, start, i - start))
            cur, start = v, i
    out.append((cur, start, len(seq) - start))
    return out


def analyze(path, out_json=None, clock_hint=None):
    ids, widths, changes = parse_vcd(path)
    tl = build_timeline(ids, changes)
    names = sorted(tl)

    clk = detect_clock(tl)
    clock_name = clock_hint or (clk["name"] if clk else None)
    rep = {
        "file": path,
        "signals": [{"name": n, "width": widths.get(n, 1),
                     "transitions": len(tl[n])} for n in names],
        "timestamps": len(changes),
        "time_range": [changes[0][0], changes[-1][0]] if changes else None,
        "clock": clk,
        "clock_used": clock_name,
    }
    if not clock_name:
        rep["error"] = "no clock-like signal found"
        _emit(rep, out_json)
        return rep

    others = [n for n in names if n != clock_name]
    for edge in ("rising", "falling"):
        samples = sample_on_edge(tl, clock_name, others, edge)
        rep[f"{edge}_edge_samples"] = len(samples)
    # prefer the edge that yields more distinct sampled patterns
    best_edge, best_variety = "rising", -1
    for edge in ("rising", "falling"):
        s = sample_on_edge(tl, clock_name, others, edge)
        variety = len({tuple(sorted(d.items())) for _, d in s})
        if variety > best_variety:
            best_edge, best_variety = edge, variety
    rep["active_edge"] = best_edge

    samples = sample_on_edge(tl, clock_name, others, best_edge)
    rep["cycles"] = len(samples)
    if not samples:
        _emit(rep, out_json)
        return rep

    per_signal = {n: [d[n] for _, d in samples] for n in others}

    sig_info = {}
    for n, seq in per_signal.items():
        bits = [s if s in "01" else "x" for s in seq]
        r = runs(bits)
        ones = sum(1 for b in bits if b == "1")
        toggles = sum(1 for a, b in zip(bits, bits[1:]) if a != b)
        first_one = next((i for i, b in enumerate(bits) if b == "1"), None)
        first_zero = next((i for i, b in enumerate(bits) if b == "0"), None)
        sig_info[n] = {
            "cycles": len(bits),
            "ones": ones,
            "duty": round(ones / len(bits), 3),
            "toggles": toggles,
            "first_one_cycle": first_one,
            "first_zero_cycle": first_zero,
            "longest_run": max((l for _, _, l in r), default=0),
            "initial_value": bits[0],
            "final_value": bits[-1],
        }

    # Reset: asserted at time zero and then released exactly once, never
    # re-asserted.  Requiring a single transition is what separates a reset from
    # an enable, since both start low -- an enable goes back down when the
    # transfer ends, giving it two or more transitions.
    resets = []
    for n, info in sig_info.items():
        b = list(per_signal[n])
        first = runs(b)[0]
        if first[1] == 0 and first[2] < len(b) * 0.5 and info["toggles"] == 1:
            resets.append({"name": n,
                           "polarity": "active_low" if first[0] == "0"
                                       else "active_high",
                           "asserted_cycles": first[2],
                           "deasserts_at_cycle": first[2],
                           "toggles": info["toggles"]})
    resets.sort(key=lambda r: r["asserted_cycles"])
    rep["candidate_resets"] = resets

    # Enable/strobe: few transitions, a clear window, not a reset.
    reset_names = {r["name"] for r in resets}
    enables = []
    for n in others:
        info = sig_info[n]
        if n in reset_names or info["toggles"] == 0:
            continue
        if info["toggles"] > max(4, len(samples) // 8):
            continue
        if not (0.05 < info["duty"] < 0.98):
            continue
        ones = [i for i, v in enumerate(per_signal[n]) if v == "1"]
        enables.append({"name": n, **info,
                        "window_start": ones[0] if ones else None,
                        "window_end": ones[-1] if ones else None,
                        "window_cycles": (ones[-1] - ones[0] + 1)
                                         if ones else 0,
                        "contiguous": bool(ones) and
                                      (ones[-1] - ones[0] + 1) == len(ones)})
    enables.sort(key=lambda e: (not e["contiguous"], e["toggles"]))
    rep["candidate_enables"] = enables

    # data: toggles often, no obvious control shape
    data = [{"name": n, **sig_info[n]} for n in others
            if sig_info[n]["toggles"] > max(4, len(samples) // 8)]
    data.sort(key=lambda d: -d["toggles"])
    rep["candidate_data_inputs"] = data

    rep["signal_statistics"] = sig_info

    # active window: cycles where any candidate data signal changes
    data_names = [d["name"] for d in data]
    if data_names:
        active = [i for i in range(len(samples))
                  if any(per_signal[n][i] != per_signal[n][max(0, i - 1)]
                         for n in data_names)]
        rep["data_activity"] = {
            "first_change_cycle": active[0] if active else None,
            "last_change_cycle": active[-1] if active else None,
            "changing_cycles": len(active),
        }

    # framing: does the enable (or data window) length divide by 8 or 16?
    win, win_src = None, None
    if enables:
        win = enables[0]["window_cycles"] or None
        win_src = f"enable '{enables[0]['name']}'"
    if win is None and rep.get("data_activity", {}).get(
            "last_change_cycle") is not None:
        d = rep["data_activity"]
        win = d["last_change_cycle"] - d["first_change_cycle"] + 1
        win_src = "data activity span (approximate)"
    rep["active_window_cycles"] = win
    rep["active_window_source"] = win_src
    if win:
        rep["framing"] = {
            "divisible_by_8": win % 8 == 0,
            "divisible_by_16": win % 16 == 0,
            "bytes_if_8bit": win // 8,
            "words_if_16bit": win // 16,
        }

    # transaction boundaries: repeated periodic patterns on control signals
    bounds = []
    for n in others:
        b = per_signal[n]
        pulses = [i for i, (a, c) in enumerate(zip(b, b[1:]), 1)
                  if a == "0" and c == "1"]
        if 2 <= len(pulses) <= 64:
            gaps = [q - p for p, q in zip(pulses, pulses[1:])]
            if gaps and len(set(gaps)) <= 2:
                bounds.append({"name": n, "pulse_cycles": pulses[:16],
                               "period": gaps[0], "pulses": len(pulses)})
    rep["candidate_transaction_boundaries"] = bounds

    _emit(rep, out_json)
    return rep


def _emit(rep, out_json):
    print(f"file: {rep['file']}")
    print(f"signals: {[s['name'] for s in rep['signals']]}")
    if rep.get("clock"):
        c = rep["clock"]
        print(f"clock: {c['name']} period {c['period']} "
              f"(regularity cv {c['regularity_cv']})")
    print(f"active edge: {rep.get('active_edge')}  cycles: {rep.get('cycles')}")
    for r in rep.get("candidate_resets", []):
        print(f"reset candidate: {r['name']} {r['polarity']} "
              f"asserted {r['asserted_cycles']} cycles, "
              f"deasserts at cycle {r['deasserts_at_cycle']}")
    for e in rep.get("candidate_enables", []):
        print(f"enable candidate: {e['name']} duty {e['duty']} "
              f"toggles {e['toggles']} window "
              f"{e['window_start']}..{e['window_end']} "
              f"({e['window_cycles']} cycles, "
              f"{'contiguous' if e['contiguous'] else 'gapped'})")
    for d in rep.get("candidate_data_inputs", []):
        print(f"data candidate: {d['name']} toggles {d['toggles']} "
              f"duty {d['duty']}")
    if rep.get("active_window_cycles"):
        f = rep.get("framing", {})
        print(f"active window [{rep.get('active_window_source')}]: "
              f"{rep['active_window_cycles']} cycles "
              f"(x8={f.get('divisible_by_8')} -> {f.get('bytes_if_8bit')} bytes, "
              f"x16={f.get('divisible_by_16')})")
    for b in rep.get("candidate_transaction_boundaries", []):
        print(f"periodic pulse: {b['name']} every {b['period']} cycles "
              f"({b['pulses']} pulses)")
    if out_json:
        json.dump(rep, open(out_json, "w"), indent=1)
        print(f"wrote {out_json}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("vcd")
    ap.add_argument("--json", default=None)
    ap.add_argument("--clock", default=None)
    a = ap.parse_args()
    analyze(a.vcd, a.json, a.clock)
