# Real puzzle runbook

Exact commands, in order, for you to run locally on the puzzle files. Every tool
here was built and regression-tested against `warmup/` only. None of the puzzle
files have been opened by Claude.

Run everything from the repo root unless noted. `PY` below means
`.venv\Scripts\python.exe`.

### Step 0: prove the toolkit itself is sound

The cell models are verified against the SKY130 Liberty data, so the PDK must be
present:

```
git clone --depth 1 --filter=blob:none --sparse ^
  https://github.com/google/skywater-pdk-libs-sky130_fd_sc_hd pdk\sky130hd
cd pdk\sky130hd && git sparse-checkout set cells timing && cd ..\..
.venv\Scripts\python.exe tests\run_all.py
```

All **13** suites must report PASS, in particular `cell semantics` (149/149
cells verified exhaustively against Liberty, 0 mismatches). If the PDK is absent
that suite FAILs by design: "could not verify" must never read as "verified".

Read **`AUDIT.md`** before trusting any result. Its section H is the checklist
that must pass before you accept an answer, and section F lists what could still
go wrong undetected.

---

## A. Inspect the hierarchy

```
.venv\Scripts\python.exe scripts\inspect_gds.py puzzle.gds
```

**Good output:** a top cell name, a die size, a few thousand top-level
instances, and a functional cell inventory of `sky130_fd_sc_hd__*` names. Port
labels listed at the bottom, including something like `success`, `clk`, `rst_n`.

**Also do this manually:** read the printed layer inventory and confirm every
layer carrying shapes is one we model (li1/mcon/met1/via/met2/via2/met3/via3/
met4/via4/met5, plus cell-internal and label layers). A conductor or cut layer
outside that stack is the single most dangerous unverified assumption in the
whole toolkit (AUDIT.md section F1).

**Failure modes:**
- *"layout appears FLAT"* — the hierarchy was stripped. The whole netlist flow
  is unavailable and the fallback is cell-geometry fingerprinting against the
  PDK. Stop and tell me; this changes the approach.
- Unfamiliar cell names (not `sky130_fd_sc_hd__`) — a different library. Tell me
  the prefix and I will extend `cells.py`.

**Bring back if stuck:** the printed cell inventory and the port label list.

---

## B. Check cell coverage before extracting

```
.venv\Scripts\python.exe scripts\check_cells.py puzzle.gds
```

**Good output:** every line starts `ok comb` or `ok seq`, ending with
`N distinct functional cells, 0 problems`.

**Failure modes:**
- `MISSING MODEL <cell> gds pins [...]` — the cell is not in `cells.py`. Likely
  candidates the warm-up did not exercise: latches (`dlxtp`, `dlrtp`), scan
  flops (`sdfrtp`), tie cells (`conb`), low-power variants (`lpflow_*`).
- `PIN MISMATCH` — my naming-convention decoder got a pin name wrong.

**Bring back:** the exact `MISSING MODEL` / `PIN MISMATCH` lines with the GDS
pin lists. That is all I need to add the model, and it contains no layout data.

---

## C. Extract and validate

```
.venv\Scripts\python.exe scripts\run_pipeline.py puzzle.gds puzzle --output success
```

This runs extract → validate → report → reduce → visualize → export in one go.
If the design is large and step 4 is slow, add `--fast` to skip symbolic
comparator detection on the first pass.

**Good output:** `validation : PASS` in the summary, and in step 3 every check
`[PASS]`, especially:
- `unresolved-vias`      every via resolved to both layers
- `single-driver`        every net has at most one driver
- `unsupported-cells`    every cell type has a model
- `pin-label-agreement`  every pin's labels agree on one net
- `refused-cells`        no tristate or clock-gate cells
- `level-sensitive-latches` and `negedge-flops`  none present

The last three are refusals added by the audit. A latch or a falling-edge flop
extracts fine but **cannot be simulated correctly** by this toolkit, so they are
hard failures rather than silent wrong answers. If either fires, stop and tell
me: the fix is real work in the simulator, not a flag.

**Failure modes and what they mean:**

| check | meaning | action |
|---|---|---|
| `unresolved-vias` FAIL | a via did not land on metal above or below; nets are silently split | send me the count and the `unresolved_cut_examples` from `puzzle_validate.json` |
| `single-driver` FAIL | two cells drive one net, so extraction merged nets that should be separate | send me the count and one example |
| `undriven-nets` WARN | nets driving inputs with no driver and no port label | often fine (real primary inputs); check the names look like ports |
| `combinational loop` FAIL | usually a latch modelled as combinational | send the cell types in the loop |
| `clock-coverage` WARN | clock pin count differs from flop count | usually gated clocks; note it and continue |

The pipeline stops on FAIL. `--force` continues anyway, but do not trust
anything downstream if you use it.

**Diagnostic file:** `generated/puzzle_validate.json`.

---

## D. Read the structural report

```
generated\puzzle_report.md
```

This is the single most useful artifact. Read in this order:

1. **Interface** — primary inputs, driven outputs, likely clock and reset nets,
   and the split between *candidate serial data inputs* (feed few flops) and
   *broadcast control inputs* (feed many). This tells you which pins carry the
   answer and which are protocol.
2. **Sequential cone for `success`** — how many state bits matter, how many are
   outside, and **suggested minimum BMC cycles**. That number is the register
   graph's depth, which for a shift-register design is its fill time.
3. **Cells outside the cone** plus its bounding box — the output generator.
   Cross-check that box against the region labelled in `layout.png`. Agreement
   is strong independent evidence the extraction is correct.
4. **Detected blocks** — shift chains, counters, carry chains, comparators, mux
   banks, decoders, repeated motifs. On the warm-up these come out as: two
   8-bit shift registers with serial inputs A and B, one 8-deep carry chain with
   4 XOR taps, and `S` as a comparison with exactly 15 satisfying assignments in
   2^16 (which is exactly the number of byte pairs summing to 496).

Note especially any row in **Constant / narrow comparators** with
`kind = constant_comparator`: that means the logic has exactly one satisfying
assignment, and `constant_int_msb_first` is the magic value the circuit is
checking for.

---

## E. Look at the pictures

```
generated\puzzle_blocks.svg      detected blocks boxed on the floorplan
generated\puzzle_cone.svg        grey = cannot influence success
generated\puzzle_registers.svg   register dependency graph at real positions
generated\puzzle_floorplan.svg   everything, coloured by cell family
```

Jane Street says the placement hints at function, so look for rows of flops
(registers), XOR ripples (adders/LFSRs), wide AND/NOR convergence (comparators),
and repeated identical tiles (per-byte or per-character datapaths).

`puzzle_cone.svg` should show a visually distinct grey region. That is the
output generator.

---

## F & G. Analyse the example VCD and infer the protocol

```
.venv\Scripts\python.exe scripts\vcd_analyze.py example_inputs.vcd --json generated\protocol_report.json
```

**Good output:** a clock with `regularity cv` near 0, one reset candidate with a
polarity and an assert duration, one or more enable candidates with a
*contiguous* window, and an active window whose length is divisible by 8.

**What to extract from it, and these are the numbers you feed to the solver:**
- reset net name, polarity, and how many cycles it is held
- enable net name and its window
- which inputs are data
- `active_window_cycles` and `framing.bytes_if_8bit`

Then replay that stimulus through the recovered netlist:

```
.venv\Scripts\python.exe scripts\vcd_replay.py generated\puzzle.json example_inputs.vcd
```

**Good output:** it runs, and `success` never goes high. Jane Street says those
are the wrong inputs, so `success` staying low is the expected result and is a
real cross-check that your netlist behaves like their design.

**Failure mode:** if the replay errors on missing signals, the VCD names differ
from the port labels; pass `--clock <name>`.

**Bring back:** the whole of `generated/protocol_report.json`. It is derived
timing statistics only, no waveform.

---

## H. Reduce (already done by the pipeline)

`generated/puzzle_reduced.json` is the cone-restricted, buffer-collapsed,
constant-folded netlist. Use it for solving; it is equivalence-tested against
the full netlist on the warm-up. If you want it standalone:

```
.venv\Scripts\python.exe scripts\reduce.py generated\puzzle.json generated\puzzle_reduced.json --target success
```

---

## I. Run the BMC

Start with the protocol you inferred in G. A realistic first command, assuming
an active-low reset held 2 cycles, an enable, and one serial data input `din`:

```
.venv\Scripts\python.exe scripts\solve_bmc.py generated\puzzle_reduced.json ^
  --output success --reset-net rst_n --reset-cycles 2 --hold en=1 ^
  --data din --search 8 200 --json generated\solution.json
```

If the design consumes text, constrain it — this is usually the difference
between hopeless and instant:

```
  --group din=8 --charset din=printable --start-cycle 0
```

Other flags worth knowing:

| flag | use |
|---|---|
| `--any-cycle` | success at *any* cycle ≤ depth, not exactly at the last one |
| `--pattern en=0011111` | per-cycle control values, last character repeats |
| `--range din=65:90` | numeric range on each 8-bit group |
| `--enumerate 5` | find several distinct solutions |
| `--charset din=lower\|alnum\|digits\|hexdigits` | character classes |
| `--vcd out.vcd` | dump the solved stimulus for Surfer |
| `--timeout 60` | per-depth solver timeout in seconds |

**Good output:** a depth where it prints `sat`, then
`concrete replay asserts success: True`. The replay line is the important one:
it means the concrete simulator independently agrees.

**Failure modes:**
- `unsat` all the way up — your control-signal assumptions are wrong (enable
  never asserted, reset polarity inverted), or the depth is too small. Check the
  suggested BMC cycles in the report and re-read the protocol report.
- `unknown` — raise `--timeout` or reduce the symbolic input set with `--data`.
- `replay disagrees with the solver` — this is serious. It means the symbolic
  and concrete models differ, most likely an incorrect cell model. Send me the
  cell inventory and stop.
- Very slow at depth — use `puzzle_reduced.json` rather than `puzzle.json`, pin
  more control signals, and add grouping constraints.

---

## J. Replay and confirm

Every SAT result is replayed automatically. To re-verify independently:

```
.venv\Scripts\python.exe scripts\solve_bmc.py generated\puzzle_reduced.json ^
  --output success ... --vcd generated\solution.vcd
```

and view `solution.vcd` in Surfer. For a fully independent solver path:

```
.venv\Scripts\python.exe scripts\export_solver.py generated\puzzle_reduced.json ^
  generated\puzzle --output success --depth <the depth that worked> --hold en=1
```

That writes `puzzle_bmc.smt2` (solvable by any SMT solver), self-contained
behavioural Verilog, and yosys scripts. Yosys is not installed here so those
`.ys` files are untested; the SMT2 path is tested and known to agree with the
BMC engine on the warm-up at depths 7 (unsat) and 8 (sat).

---

## K & L. Re-enable the output generator and decode the answer

The extraction already includes the output-generator cells; only the *cone
analysis* set them aside. So run the winning trace through the **full** netlist,
not the reduced one:

```
.venv\Scripts\python.exe scripts\solve_bmc.py generated\puzzle.json ^
  --output success <same protocol flags> --cycles <winning depth> ^
  --json generated\final.json --vcd generated\final.vcd
```

Then look at all driven outputs across the run. `generated/final.json` contains
the per-cycle trace; `final.vcd` shows every port in Surfer. If the answer is a
serial string, read the output port's bits in groups of 8 — the solver already
prints `words`, `hex`, and `ascii` for each symbolic input, and the same
decoding applies to the output bit stream.

If the output generator drives a port that is not in `--output`, find it in the
report's **driven outputs** list and re-run pointing at it, or read it from the
VCD.

---

## What to send me if you want help reasoning

All of these are derived artifacts, not puzzle files:

1. `generated/puzzle_report.md` — the whole thing. This is the most useful
   single item by far.
2. `generated/puzzle_validate.json` — especially if anything is WARN or FAIL.
3. `generated/protocol_report.json` — the inferred protocol.
4. The `check_cells.py` output if any cell is missing or mismatched.
5. The solver's console output: the depth ladder and whether replay confirmed.
6. Counts and bounding boxes, never the GDS, the PNG, or the raw VCD.

With the report plus the protocol JSON I can reason about the circuit's
structure, suggest which blocks to attack, and tune the solver flags, without
ever seeing a puzzle file.
