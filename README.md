# reverse-asic

A toolkit that turns a placed-and-routed GDS layout back into a simulatable,
solvable gate-level circuit.

It was built for the Jane Street *"Can you reverse engineer an ASIC?"* puzzle
(2026), but nothing in it is puzzle-specific: give it any SKY130
standard-cell GDS and it will recover the netlist, tell you what the circuit is
made of, and let a SAT solver search for an input sequence that drives a chosen
output high.

```
GDS layout
   -> standard-cell instances + routed connectivity   (geometry)
   -> gate-level netlist                              (extraction)
   -> structural understanding                        (graph analysis)
   -> input sequence that asserts a target            (bounded model checking)
   -> replay through the full circuit                 (simulation)
```

## Why this is tractable

The interesting property of a modern placed-and-routed GDS is that it is not
really a soup of polygons. The standard-cell hierarchy usually survives: each
gate is a named reference to a library cell, and each cell carries its pin names
as text labels. So the gates do not have to be recovered from transistors. Only
the wiring does.

Wiring is rebuilt geometrically over the SKY130 stack:

```
li1 -mcon- met1 -via- met2 -via2- met3 -via3- met4 -via4- met5
```

Each conductor layer is flattened and merged, so a merged polygon *is* one
connected component within that layer. Layers join only through cut shapes, and
because a cut is enclosed by metal on both sides, its centre point identifies
exactly one polygon per side. A union-find over `(layer, polygon)` nodes then
yields the nets. No polygon-against-polygon intersection is ever computed, which
is why extraction resolves every via rather than *most* of them.

## Quickstart

```bash
python -m venv .venv
.venv/Scripts/python -m pip install gdstk klayout z3-solver networkx numpy

# authoritative cell data, required by the semantics audit
git clone --depth 1 --filter=blob:none --sparse \
  https://github.com/google/skywater-pdk-libs-sky130_fd_sc_hd pdk/sky130hd
(cd pdk/sky130hd && git sparse-checkout set cells timing)

.venv/Scripts/python tests/run_all.py          # 18 suites, all must pass
.venv/Scripts/python scripts/run_pipeline.py warmup/04_final.gds warmup --output S
```

The pipeline writes a netlist, a validation report, a structural report,
SVG floorplans, a reduced netlist for solving, and Verilog/SMT2 exports into
`generated/`.

## What it does, stage by stage

| stage | script | output |
|---|---|---|
| first look at a layout | `inspect_gds.py` | hierarchy, cell inventory, layer inventory, port labels |
| check cell coverage | `check_cells.py` | every cell type modelled, pin sets matching the GDS |
| extract | `extract_netlist.py` | deterministic netlist JSON with diagnostics and hashes |
| validate | `validate_netlist.py` | PASS / WARN / FAIL over 17 structural invariants |
| investigate warnings | `diagnose.py` | undriven nets, dangling outputs, clock-tree tracing |
| understand | `puzzle_report.py` | shift chains, counters, comparators, adders, mux banks, motifs |
| explain a target | `explain_target.py` | what must be true for a chosen output to assert |
| infer the protocol | `vcd_analyze.py` | clock, reset polarity, enable windows, byte framing |
| shrink | `reduce.py` | cone restriction, buffer collapse, constant folding |
| solve | `solve_bmc.py` | incremental Z3 BMC with charset/range/grouping constraints |
| recover the answer | `replay_solution.py` | replay through the full netlist and decode the output |

Plus `visualize.py` for SVG views, `emit_verilog.py` / `emit_behavioural.py` /
`export_solver.py` for external tools, and `synth_layout.py` /
`netlist_builder.py` which generate test circuits with known ground truth.

## Correctness

Layout extraction fails quietly. A single missed via does not crash anything: it
splits one net into two and produces a circuit that simulates happily and is
wrong. Most of the engineering here is about not being fooled by that.

Current status, all reproducible with `tests/run_all.py`:

| check | result |
|---|---|
| SKY130 cell models vs Liberty data | 149 of 149 verified exhaustively, 0 mismatches |
| warm-up extraction | 3,469 cuts resolved, 0 unresolved, 0 unresolved pins |
| warm-up vs Jane Street's gate-level netlist | structurally isomorphic, 79/79 cells uniquely identified |
| warm-up behaviour vs the reference Verilog | 315 vectors, 0 mismatches |
| pin transforms | all 8 orientations vs an independent affine implementation |
| adversarial layouts | 12 corrupted layouts, each extracted correctly or caught |
| leaf-cell classification | 9 cases: annotation ignored, any electrical geometry refused |
| sequential semantics | 11 circuits vs independent RTL references |
| randomised differential | 300 random circuits, ~3,500 cells |
| BMC soundness | 28 known-answer checks including unsat cases |

The distinction that matters most: the simulator, the Z3 backend, the
behavioural Verilog and the SMT2 export all derive from the same cell
definitions, so their agreeing with each other proves only that the execution
engines match. It says nothing about whether those definitions describe the real
standard cells. That is why `audit_cell_semantics.py` exists, checking every
supported cell against the SkyWater Liberty data, which is a source this project
did not write. It found four genuine cell-model bugs. See
[`docs/AUDIT.md`](docs/AUDIT.md).

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — how extraction works and why
  each design decision was made
- [`docs/AUDIT.md`](docs/AUDIT.md) — adversarial correctness audit: assumptions,
  which are independently verified, which are not, every bug found, and the
  checks to satisfy before trusting a result
- [`docs/RUNBOOK.md`](docs/RUNBOOK.md) — stage-by-stage operating guide with
  expected output and failure modes
- [`docs/UPSTREAM_README.md`](docs/UPSTREAM_README.md) — Jane Street's original
  README for the puzzle repository

## Repository layout

```
scripts/     29 modules: extraction, analysis, solving, export, visualisation
tests/       19 suites: regression, adversarial, differential, fuzz
docs/        architecture, audit, runbook
warmup/      Jane Street's worked example (source, netlist, DEF, GDS)
generated/   all output, git-ignored
pdk/         SKY130 library, git-ignored, cloned on setup
```

## Credits and terms

The puzzle, the warm-up design, `puzzle.gds`, `layout.png` and
`example_inputs.vcd` are Jane Street's, from
[janestreet/asic-puzzle-2026](https://github.com/janestreet/asic-puzzle-2026).
Cell data comes from the
[SkyWater SKY130 PDK](https://github.com/google/skywater-pdk-libs-sky130_fd_sc_hd)
under Apache 2.0.

Jane Street asked solvers not to feed the puzzle files into an AI tool, not to
use AI to generate writeups, and not to post spoilers or a full writeup publicly
until submissions close. This repository contains tooling and its test suite;
solved outputs live in the git-ignored `generated/` directory and are not
published here.
