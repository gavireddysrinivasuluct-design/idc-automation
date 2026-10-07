#!/usr/bin/env python3
"""Review and approve the expected design topology (local-inputs/ufm/nscale_Compute.topo).

  ./scripts/approve_design.py                 show the approval state
  ./scripts/approve_design.py approve         show the diff of the fetched candidate, then approve it
  ./scripts/approve_design.py approve --note "CHG-1234"

The dashboard compares against the approved copy only. A design fetched from the UFM host
that differs from it waits here (and in the dashboard's evidence bar) until approved.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import design_gate  # noqa: E402

raise SystemExit(design_gate.main())
