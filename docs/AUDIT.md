# Adversarial correctness audit

Objective: find every plausible way this pipeline could confidently produce a
**wrong** answer on a larger SKY130 design. Conducted entirely on the warm-up,
the public SKY130 PDK, and synthetic tests. No puzzle file was inspected.

Summary: **6 real bugs found and fixed, 2 silent-wrongness classes found and
converted into hard refusals, 1 diagnostic gap closed.** Four of the six bugs
were invisible to the pre-audit test suite because that suite compared the
toolkit against itself.

---

## A. Assumptions required for correctness

1. The GDS retains the standard-cell hierarchy with sky130 cell names.
2. Each cell's pin names appear as li1 text labels, and a label lies on the
   conductor shape belonging to that pin.
3. Connectivity is fully described by the layer stack
   `li1-mcon-met1-via-met2-via2-met3-via3-met4-via4-met5`.
4. A via is enclosed by metal on both sides, so its centre point identifies one
   polygon per side.
5. Every net has exactly one driver (no tristate, no wired logic).
6. `cells.py` describes each cell's true logic function.
7. Instance transforms map cell-local pin coordinates to die coordinates.
8. A clock cycle can be modelled as clock-low then clock-high, with each flop
   capturing on a rising edge of its own CLK net.
9. Reduction preserves behaviour.
10. The z3 encoding faithfully represents the simulator's semantics.
11. Power pins/rails are constants, not signals.

## B. Assumptions now independently verified

| # | How, and by what independent source |
|---|---|
| 4 | 3,469 cuts in the warm-up, 0 unresolved; adversarial missing-via and open-trunk cases caught |
| 5 | multi-driver and bridged-net cases produce a FAIL verdict |
| 6 | **149/149 supported cells checked against SKY130 Liberty** (`audit_cell_semantics.py`): exhaustive truth tables for combinational cells, exhaustive transition tables plus clock pin/edge and clear/preset pin/polarity for sequential. Liberty is a source we did not write. |
| 7 | All 8 orientations checked against an affine transform implemented from first principles, not by calling klayout again; plus routed round-trips in R0/R180/MX/MY, the only orientations the warm-up actually contains |
| 8 | 11 sequential circuits vs RTL references written independently of `cells.py` |
| 9 | 300 random circuits, reduced and compared from 6 random **non-reset** initial states each |
| 10 | 300 random circuits: every BMC model replayed concretely; every unsat cross-checked against exhaustive concrete search where enumerable |
| 11 | Signal pins tied to VPWR and to VGND both resolve to constants, not primary inputs |

## C. Assumptions still NOT independently verified

- **1, 2, 3 (the layer stack and label convention).** Confirmed on the warm-up
  and consistent with the PDK, but there is no second source that says "these
  are all the conductor layers". A design using met5 routing above what we model,
  or a cell whose pin label sits off its own shape, would break silently.
  *Mitigation:* `inspect_gds.py` prints the full layer inventory; any layer with
  shapes that is not in our stack is visible there. **Check this manually.**
- **Extraction of dense flop cells by the synthetic harness.** The router cannot
  wire `dfrtp` (its internal met1 covers the li1 pin), so flop extraction is
  verified only via the warm-up's gold-netlist isomorphism, which does cover 16
  `dfrtp` instances across all four orientations.
- **Yosys cross-check.** Not installed; the generated `.ys` scripts are untested.

## D. Known unsupported SKY130 constructs

Refused loudly (`check_cells.py` and `validate_netlist.py` FAIL):

| construct | why refused |
|---|---|
| `ebufn`, `einvn`, `einvp` | tristate: legitimately multi-driver, breaks the one-driver-per-net model |
| `dlclkp`, `sdlclkp` | integrated clock gates described by a Liberty statetable, so no model can be verified against an authoritative source |
| any level-sensitive latch (`dlxtp`, `dlrtp`, `dlxbp`, …) | the simulator models only edge-triggered storage; a latch would simulate as permanently stuck |
| any falling-edge flop (`dfrtn`, `sdfrtn`, `dfbbn`, …) | a cycle is modelled low-then-high, so a falling edge never occurs and the flop would never capture |

Ignored as physical-only: `decap`, `diode`, `tap*`, `fill*`, `lpflow_bleeder`,
`lpflow_decapkapwr`.

Note the last two rows: those cells *extract* correctly, and earlier they would
have *simulated* silently wrongly. They are now hard failures.

## E. Failure modes validation catches

| failure | detected as |
|---|---|
| missing via / unresolved cut | FAIL `unresolved-vias` |
| open metal segment | WARN `undriven-nets` / `dangling-outputs` |
| accidental short between driven nets | FAIL `single-driver` |
| two outputs on one net | FAIL `single-driver` |
| floating input | WARN `undriven-nets` |
| unused output | WARN `dangling-outputs` |
| duplicated port label on two nets | FAIL `port-resolution` |
| pin labels resolving to different nets | FAIL `pin-label-agreement` |
| unmodelled cell type | FAIL `unsupported-cells` |
| tristate / clock-gate cell | FAIL `refused-cells` |
| latch present | FAIL `level-sensitive-latches` |
| negedge flop present | FAIL `negedge-flops` |
| combinational loop | FAIL `build-circuit` |

All twelve adversarial layouts either extracted correctly or were flagged.

## F. Failure modes that could still evade validation

Ranked by how plausible they are on a real puzzle chip.

1. **A conductor or cut layer we do not model.** Routing on a layer outside the
   stack would appear simply as absent connectivity: nets would look open and be
   reported as many undriven nets. Loud, but the *diagnosis* would be wrong.
   **Look at the layer inventory from `inspect_gds.py` yourself.**
2. **A cell whose pin label does not lie on its own conductor shape.** The
   lookup would silently attach the pin to whatever polygon does contain the
   point. Partially defended by `pin-label-agreement`, which fires only when a
   pin has multiple labels that disagree.
3. **Two shapes on the same layer touching at exactly one point.** We tested
   that this does not create a false merge, but the electrical truth of a corner
   touch is ambiguous. If the real design relies on one, we would get it wrong.
4. **A net that is genuinely driven by a port and a cell.** Reads as
   single-driver, and the port label wins.
5. **Wrong assumption about which port is the clock.** `--clock` defaults to
   `clk`; a different name means every flop sees a constant clock and nothing
   ever captures. Symptom: BMC unsat at every depth.
6. **Antenna diodes or fill cells with real connectivity.** We drop them as
   physical-only. If a diode is on a signal net, we lose nothing electrically,
   but if a design used a fill cell functionally we would drop it.
7. **The reduction's cone restriction is correct only for the named target.** If
   you solve on the reduced netlist and then read an output outside the cone,
   it will be missing. Use the FULL netlist to decode the answer.

## G. Confidence achievable in a real-puzzle SAT result

**High for the logic, moderate for the interface.**

What is strong: cell semantics (149/149 against Liberty), pin transforms,
connectivity given the layer stack, sequential semantics, reduction, and BMC
soundness including unsat.

What remains uncertain is not the circuit model but the *framing*: whether we
identified the clock, the reset polarity, the enable protocol, and the correct
number of cycles. Those come from the VCD and are inferred, not proven. A wrong
protocol assumption yields unsat, not a wrong answer, which is the safe
direction.

The genuinely dangerous residual is F1/F2: a layer or label convention outside
what we model. Both are visible in `inspect_gds.py` output, which is why that is
step one and why you should read it rather than skim it.

A SAT result that survives all of section H should be trusted. A SAT result that
has not been replayed concretely should not be trusted at all.

## H. Cross-checks to require before accepting a candidate answer

Every one of these must pass:

1. `tests/run_all.py` reports **ALL PASS**, including `cell semantics`.
2. `inspect_gds.py` layer inventory contains **no conductor/cut layer outside
   our stack**, and you have read it.
3. `check_cells.py`: 0 problems, no `UNSUPPORTED` or `PIN MISMATCH`.
4. `validate_netlist.py` verdict is **PASS**, with `unresolved-vias`,
   `single-driver`, `pin-label-agreement`, `refused-cells`,
   `level-sensitive-latches` and `negedge-flops` all PASS.
5. The cone analysis' outside-cone bounding box **matches the output-generator
   region marked in `layout.png`**. This is the strongest independent confirmation
   available that the extraction is right, because it comes from Jane Street.
6. `vcd_replay.py` on the supplied VCD runs and leaves `success` low.
7. The solver reports `concrete replay asserts success: True`. Never accept a
   result without this line.
8. The solved depth is at least `suggested_min_bmc_cycles` from the report.
9. Re-solve at `depth - 1`: it must be **unsat**. A minimum-depth solution that
   is also satisfiable one cycle shorter means the depth accounting is wrong.
10. Export SMT2 and re-solve it independently: same verdict at the same depth.
11. Decode the final answer from the **full** netlist, not the reduced one.

---

## Bugs found

Each entry: what it was, why the existing tests missed it, and the regression
that now covers it.

### 1. Inverted-input polarity in the or/nor "b" family (SILENT WRONG LOGIC)

`or2b`, `or3b`, `or4b`, `or4bb`, `nor2b`, `nor3b`, `nor4b`, `nor4bb` had the
active-low input on the **leading** pins. SKY130 puts it on the **trailing**
pins for or/nor, while and/nand really do use leading. The convention is
asymmetric:

```
and2b   A_N, B          nand4bb  A_N, B_N, C, D      leading
or2b    A,   B_N        nor4bb   A,   B,   C_N, D_N  trailing
```

*Missed because* the warm-up contains none of these cells, and every other test
compared the toolkit against itself.
*Regression:* `test_cell_audit.py` — exhaustive truth-table comparison against
Liberty for all 149 supported cells.

### 2. `xnor3` output pin (WRONG PIN NAME)

Modelled as `Y`; SKY130 uses `X`. (`xnor2` really is `Y` — also asymmetric.)
*Missed:* same reason. *Regression:* same.

### 3. `fah` carry-in pin name (WRONG PIN NAME)

Modelled with `CIN`; `fah` uses `CI`. (`fa` uses `CIN`.)
*Missed:* same reason. *Regression:* same.

### 4. Scan/enable priority inverted in `sim.py` (SILENT WRONG STATE)

The simulator applied the scan mux and then the enable mux, so `DE=0` could
defeat `SCE=1` and hold instead of loading the scan value. Liberty is explicit:
`next_state = (D & DE & !SCE) | (IQ & !DE & !SCE) | (SCD & SCE)` — scan
overrides enable.
*Missed because* no scan-enabled flop was modelled at all before this audit, so
nothing exercised the interaction.
*Regressions:* `test_cell_audit.py` transition tables, and
`test_sequential.py::scan+enable flop`.

### 5. Same bug in `emit_behavioural.py` (SILENT WRONG EXPORT)

The Verilog emitter had the same scan-then-enable order, found by re-reading the
file after fixing `sim.py`. It would have made the exported Verilog and SMT2
disagree with the simulator for scan flops.
*Missed because* no test compared the emitted Verilog's semantics to the
simulator's; the export test only checked structure.
*Regression:* covered by `test_cell_audit.py` (shared definitions) and now
documented; the emitter comment records the required order.

### 6. Latches and negedge flops silently mis-simulated (SILENT WRONG STATE)

A level-sensitive latch was simulated as edge-triggered — in practice stuck at
its reset value — and a falling-edge flop never captured anything, because a
cycle is modelled low-then-high. Both produced plausible, completely wrong
behaviour.
*Missed because* the warm-up uses only `dfrtp`, and no test built a latch.
*Fix:* rather than risk an unsound model, both are now hard FAILs in
`validate_netlist.py`.
*Regression:* `test_sequential.py::refusal_checks` asserts both refusals fire.

### 7. Pin-label disagreement went unreported (DIAGNOSTIC GAP)

If a pin's multiple labels resolved to different nets, the extractor kept
whichever it visited last, making the result depend on iteration order.
*Missed because* the warm-up has no such pin.
*Fix:* new `pin-label-agreement` FAIL check.

### Test-harness bugs found (would have caused false confidence)

- The synthetic router contacted pins at their label point, which shorted pins
  sharing an x and shorted pins near the power rail. This made a **correct**
  extractor look broken in 16 of 32 orientation cases. Fixed by searching the
  pin's li1 polygon for a rail-clear, x-unique contact.
- The "accidental short" adversarial case drew its bridge outside both trunks'
  x spans, so it shorted nothing and passed vacuously. `short_nets` now derives
  the bridge x from the trunk spans and raises if they do not overlap.
- The fuzz reducer check seeded initial state positionally, but reduction
  legitimately removes flops, so it compared unrelated states. Now keyed by
  instance name.
- The fuzz BMC check required success at the *first* cycle it appeared rather
  than the cycle requested. Now checks the requested cycle.

---

## Test inventory

| suite | scope |
|---|---|
| `test_cell_audit.py` | 149 cells vs SKY130 Liberty, exhaustive |
| `test_extract.py` | determinism, hashes, 0 unresolved, gold isomorphism |
| `test_orientations.py` | 14 routed placements + 32 transform checks |
| `test_extract_adversarial.py` | 12 corrupted layouts |
| `test_sequential.py` | 11 circuits vs RTL references + 2 refusals |
| `test_bmc_soundness.py` | 28 known-answer checks |
| `test_fuzz.py` | 300 random circuits, differential |
| `test_warmup.py` | 315 vectors vs `00_source.v` |
| `test_blocks.py` | block detectors on the warm-up |
| `test_reduce.py` | 120 vectors x 12 cycles equivalence |
| `test_solver.py` | solver features on the warm-up |
| `test_protocol.py` | VCD protocol inference |
| `test_export.py` | SMT2 round-trip agrees at depth 7/8 |
