# asic-re: GDS -> netlist -> understanding -> SAT solution

Reverse-engineering toolkit for the Jane Street ASIC puzzle. Built and validated
entirely against `warmup/`, which the puzzle rules explicitly permit AI help on.
The puzzle files themselves have never been opened by an AI; running the pipeline
on them is a human step, documented in **`RUNBOOK.md`**.

## Setup

```
python -m venv .venv
.venv/Scripts/python -m pip install gdstk klayout z3-solver networkx numpy

# required: authoritative cell data for the semantics audit
git clone --depth 1 --filter=blob:none --sparse   https://github.com/google/skywater-pdk-libs-sky130_fd_sc_hd pdk/sky130hd
(cd pdk/sky130hd && git sparse-checkout set cells timing)

.venv/Scripts/python tests/run_all.py     # 13 suites, all must PASS
```

See **`AUDIT.md`** for the adversarial correctness audit: what is independently
verified, what is not, and the checklist to satisfy before trusting an answer.

## Validation status

| suite | what it guarantees |
|---|---|
| extraction | deterministic hashes, 0 unresolved vias, 0 unresolved pins, 0 unsupported cells, validator PASS, structurally isomorphic to `01_netlist.v` (79/79 cells uniquely identified) |
| behaviour | 315 vectors against `00_source.v`, 0 mismatches |
| blocks | both 8-bit shift registers, the adder's carry chain, `S` as a 15-solution comparison, the success cone, serial vs broadcast input classification |
| reduction | 120 random vectors x 12 cycles, reduced netlist behaviourally identical |
| solver | minimum depth 8 found, all enumerated solutions sum to 496 and are distinct, range constraints honoured, impossible constraints unsat, any-cycle mode, replay confirms every model |
| protocol | clock, active edge, reset polarity and duration, enable window, data inputs and byte framing all recovered from a VCD alone |
| export | exported SMT2 re-solves to sat at depth 8 and unsat at depth 7, agreeing with the BMC engine |

## How extraction works

The standard-cell hierarchy survives in the GDS, so gates need not be recovered
from transistors: each is a named `sky130_fd_sc_hd` reference carrying its pin
names as li1 text labels. Only wiring is rebuilt geometrically, over

```
li1 -mcon- met1 -via- met2 -via2- met3 -via3- met4 -via4- met5
```

Each conductor layer is flattened and merged, so a merged polygon *is* a
connected component within that layer. Layers join only through cut centre
points, since a cut is fully enclosed by the metal above and below. Union-find
over `(layer, polygon)` then yields nets. No polygon-vs-polygon intersection is
ever computed, which is why zero cuts go unresolved.

Tie-offs matter: a signal pin landing on VPWR/VGND is a constant, not a primary
input, so power roots are identified explicitly and propagated as constants.

## Scripts

| script | purpose |
|---|---|
| `inspect_gds.py` | hierarchy, cell inventory, layers, port labels |
| `check_cells.py` | validate cell-library pin sets against GDS labels |
| `extract_netlist.py` | geometry -> netlist JSON, with diagnostics and hashes |
| `validate_netlist.py` | PASS/WARN/FAIL invariants (drivers, vias, clocks, loops) |
| `puzzle_report.py` | derived structural report, Markdown + JSON |
| `blocks.py` | shift chains, counters, comparators, adders, muxes, decoders, motifs |
| `seqcone.py` | sequential cone, distances, suggested BMC depth |
| `netgraph.py` | cones, register graph, SCCs, symbolic cone functions, model counting |
| `reduce.py` | cone restriction, buffer collapse, constant folding |
| `solve_bmc.py` | incremental BMC with protocol pinning, grouping, charsets, enumeration |
| `vcd_analyze.py` | protocol inference from a VCD |
| `vcd_replay.py` | drive the recovered netlist from a VCD |
| `visualize.py` | floorplan, cone, block map, register graph (SVG) |
| `emit_verilog.py` | structural Verilog (needs PDK models) |
| `emit_behavioural.py` | self-contained behavioural Verilog (no PDK) |
| `export_solver.py` | SMT2 + yosys scripts for an independent solver path |
| `run_pipeline.py` | all of the above in one command |
| `compare_netlists.py` | WL-refinement isomorphism check against a gold netlist |

## Notable design decisions

**Comparators are identified by function, not by cell names.** The logic cone of
each candidate net is turned into a z3 expression and its satisfying assignments
are counted. Exactly one solution means a comparison against a constant, and the
model *is* that constant. On the warm-up this reports `S` as having 15 solutions
in 2^16, which is exactly the number of byte pairs summing to 496.

**Adders are found by the carry ripple, not by XOR chains.** After technology
mapping the carry propagates through compound AOI cells and the sum bits hang off
it as XOR taps; looking for XOR feeding XOR finds nothing at all.

**BMC depth is estimated from register-graph depth, not distance to the target.**
In a shift register every bit feeds the comparator directly, so all sit at
distance 1, yet the register still takes its full length to fill. The register
graph's longest path gives 8 on the warm-up, which is the true answer.

**One set of cell definitions drives everything.** The same functions are
evaluated with a boolean backend (simulation), a z3 backend (solving), and a
Verilog-string backend (behavioural emission), so the three can never disagree.
That is consistency, not correctness: if a definition were wrong, all three
would be wrong together. Correctness comes from `audit_cell_semantics.py`, which
checks all 149 supported cells against the SKY130 Liberty data exhaustively.

**Reduction is equivalence-tested.** A smaller netlist that is not equivalent is
worse than no reduction, so buffer collapsing and constant folding are checked by
simulating both netlists on the same random vectors.

## Gotchas

- Enable-held shift registers always feed Q back through the hold mux, so
  self-loops must be ignored or no chain in the design is detectable.
- Clocking assumes nothing about which net is the clock: each cycle evaluates
  the clock port low then high, and every flop capturing on a rising edge of *its
  own* CLK net. Inverted and gated clock trees need no special handling.
- Aliasing a buffer's output requires deleting the buffer, or it still drives the
  net it was aliased onto and the netlist gains a second driver.
- Yosys is not installed in this environment, so the generated `.ys` scripts are
  written from documented syntax but untested. The SMT2 path is tested.
