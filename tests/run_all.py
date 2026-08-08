"""Run the whole regression suite.  Exit non-zero if anything fails."""

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(HERE, "..", ".venv", "Scripts", "python.exe")
if not os.path.exists(PY):
    PY = sys.executable

TESTS = [
    ("cell semantics", "test_cell_audit.py"),
    ("extraction", "test_extract.py"),
    ("orientations", "test_orientations.py"),
    ("extractor attack", "test_extract_adversarial.py"),
    ("hierarchy", "test_hierarchy.py"),
    ("physical leaves", "test_physical_leaf.py"),
    ("diagnostics", "test_diagnose.py"),
    ("explain target", "test_explain_target.py"),
    ("replay solution", "test_replay_solution.py"),
    ("sequential", "test_sequential.py"),
    ("bmc soundness", "test_bmc_soundness.py"),
    ("fuzz", "test_fuzz.py"),
    ("behaviour", "test_warmup.py"),
    ("blocks", "test_blocks.py"),
    ("reduction", "test_reduce.py"),
    ("solver", "test_solver.py"),
    ("protocol", "test_protocol.py"),
    ("verilog export", "test_export.py"),
]


def main():
    results = []
    for label, script in TESTS:
        path = os.path.join(HERE, script)
        if not os.path.exists(path):
            results.append((label, "MISSING", 0.0))
            continue
        t0 = time.time()
        p = subprocess.run([PY, path], capture_output=True, text=True)
        dt = time.time() - t0
        ok = p.returncode == 0
        results.append((label, "PASS" if ok else "FAIL", dt))
        if not ok:
            print(f"===== {label} FAILED =====")
            print(p.stdout[-4000:])
            print(p.stderr[-2000:])

    print("\n" + "=" * 52)
    for label, status, dt in results:
        print(f"  {status:7s} {label:16s} {dt:6.1f}s")
    print("=" * 52)
    bad = [r for r in results if r[1] != "PASS"]
    print("ALL PASS" if not bad else f"{len(bad)} SUITE(S) FAILING")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
