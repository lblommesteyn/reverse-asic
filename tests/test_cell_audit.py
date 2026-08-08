"""The cell-semantics audit is a required gate, not an optional extra.

Everything downstream inherits cells.py, so if a single cell model disagrees
with the SKY130 Liberty data the entire result is wrong in a way no other check
can see.  This suite fails if the PDK is missing, because "we could not verify"
must not be silently equivalent to "verified".
"""

import contextlib
import io
import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

import audit_cell_semantics as audit

PDK = os.path.join(HERE, "..", "pdk", "sky130hd")


def main():
    if not os.path.isdir(os.path.join(PDK, "cells")):
        print("FAIL  SKY130 library not present; cell semantics are UNVERIFIED")
        print("  git clone --depth 1 --filter=blob:none --sparse \\")
        print("    https://github.com/google/skywater-pdk-libs-sky130_fd_sc_hd"
              " pdk/sky130hd")
        print("  (cd pdk/sky130hd && git sparse-checkout set cells timing)")
        return 1

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = audit.main.__wrapped__() if hasattr(audit.main, "__wrapped__") \
            else _run(audit)
    out = buf.getvalue()
    print(out.strip())
    return rc


def _run(mod):
    saved = sys.argv
    sys.argv = ["audit", "--pdk", PDK]
    try:
        return mod.main()
    finally:
        sys.argv = saved


if __name__ == "__main__":
    sys.exit(main())
