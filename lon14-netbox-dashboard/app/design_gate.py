#!/usr/bin/env python3
"""The approved design topology as a versioned, reviewed artifact.

UFM fetches copy /root/nscale_Compute.topo from the UFM host. That file lives on an
operational host, so a manual or accidental edit there must not silently become the
"expected" cabling. Files in local-inputs/ufm/:

  nscale_Compute.fetched.topo   the latest copy from UFM (a candidate, never trusted by itself)
  nscale_Compute.topo           the APPROVED copy the cabling check compares against
  design-approved.json          manifest: sha256, size, link counts, who approved it and when,
                                plus the history of earlier approvals
  design-status.json            last fetch: candidate sha256, fetch time, and a summary of how
                                it differs from the approved copy

A fetch whose sha256 equals the approved one changes nothing. A different file is kept as a
candidate ("change pending") until someone approves it (dashboard button or
scripts/approve_design.py); until then the comparison keeps using the approved copy.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import shutil
import time
from pathlib import Path

ACTIVE = "nscale_Compute.topo"
FETCHED = "nscale_Compute.fetched.topo"
PREVIOUS = "nscale_Compute.previous.topo"
MANIFEST = "design-approved.json"
STATUS = "design-status.json"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def sha256_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(".%s.%d.tmp" % (path.name, os.getpid()))
    tmp.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def design_counts(path: Path) -> dict:
    """Leaf-spine / GPU-port counts of a design file, and its leaf-spine map (for diffs)."""
    import ufm_cabling  # local module
    try:
        rows, info = ufm_cabling.load_design_topo(path, None)
    except (OSError, ValueError) as error:
        return {"error": str(error)}
    ls = {(r["a_device"], r["a_port"]): (r["b_device"], r["b_port"]) for r in rows if r["link_type"] == "leaf-spine"}
    gpu = {(r["a_device"], r["a_port"]): (r["b_device"], r["b_port"]) for r in rows if r["link_type"] == "leaf-gpu"}
    return {"leaf_spine": info.get("leaf_spine"), "gpu_ports": info.get("gpu_ports"), "suspect": info.get("suspect_count", 0),
            "_ls": ls, "_gpu": gpu}


def diff_summary(old: Path, new: Path, limit: int = 25) -> dict:
    """How a candidate differs from the approved copy, in cabling terms."""
    a, b = design_counts(old), design_counts(new)
    if "error" in a or "error" in b:
        return {"error": a.get("error") or b.get("error")}
    changes = []
    for kind in ("_ls", "_gpu"):
        x, y = a[kind], b[kind]
        for key in sorted(set(x) | set(y)):
            if x.get(key) != y.get(key):
                changes.append({"type": "leaf-spine" if kind == "_ls" else "leaf-gpu", "port": list(key),
                                "approved": list(x[key]) if key in x else None, "fetched": list(y[key]) if key in y else None})
    return {"changed_ports": len(changes), "leaf_spine_changes": sum(c["type"] == "leaf-spine" for c in changes),
            "gpu_changes": sum(c["type"] == "leaf-gpu" for c in changes), "examples": changes[:limit],
            "approved_counts": {k: a[k] for k in ("leaf_spine", "gpu_ports", "suspect")},
            "fetched_counts": {k: b[k] for k in ("leaf_spine", "gpu_ports", "suspect")}}


def ingest_fetched(data: bytes, target_dir: Path, source: str = "UFM host /root/nscale_Compute.topo") -> dict:
    """Store a freshly fetched design as the candidate and work out its approval state.
    Never replaces the approved copy with a file that differs from the manifest."""
    target_dir.mkdir(parents=True, exist_ok=True)
    fetched, active = target_dir / FETCHED, target_dir / ACTIVE
    _write_bytes(fetched, data)
    sha = hashlib.sha256(data).hexdigest()
    manifest = _read_json(target_dir / MANIFEST)
    approved_sha = manifest.get("sha256")
    active_sha = sha256_file(active)
    if approved_sha and sha == approved_sha:
        if active_sha != sha:  # approved copy missing or edited locally: restore it from the identical candidate
            _write_bytes(active, data)
        state, diff = "approved", None
    elif not active.is_file():
        _write_bytes(active, data)  # first use: something to compare against, flagged as unapproved
        state, diff = "unapproved", None
    elif not approved_sha and sha == active_sha:
        state, diff = "unapproved", None  # existing copy, never reviewed
    else:
        state, diff = "change-pending", diff_summary(active, fetched)
    status = {"state": state, "candidate_sha256": sha, "candidate_bytes": len(data), "fetched_at": now_iso(),
              "source": source, "approved_sha256": approved_sha, "active_sha256": sha256_file(active), "diff": diff}
    _write_json(target_dir / STATUS, status)
    return status


def status(target_dir: Path) -> dict:
    """The design's governance state for the dashboard (cheap: no parsing)."""
    active, manifest, last = target_dir / ACTIVE, _read_json(target_dir / MANIFEST), _read_json(target_dir / STATUS)
    active_sha = sha256_file(active)
    if not active_sha:
        state = "missing"
    elif manifest.get("sha256") and manifest["sha256"] != active_sha:
        state = "tampered"  # the approved copy no longer matches its manifest
    elif last.get("state") == "change-pending" and last.get("candidate_sha256") not in (None, manifest.get("sha256")):
        state = "change-pending"
    elif manifest.get("sha256"):
        state = "approved"
    else:
        state = "unapproved"
    out = {"state": state, "file": ACTIVE, "active_sha256": active_sha,
           "approved_sha256": manifest.get("sha256"), "approved_at": manifest.get("approved_at"),
           "approved_by": manifest.get("approved_by"), "approved_note": manifest.get("note", ""),
           "fetched_at": last.get("fetched_at"), "candidate_sha256": last.get("candidate_sha256"),
           "source": last.get("source") or manifest.get("source")}
    if state == "change-pending":
        out["diff"] = last.get("diff")
    return out


def approve(target_dir: Path, sha256: str, by: str | None = None, note: str = "") -> dict:
    """Approve the fetched candidate (or the current copy, for a first approval) by its sha256.
    The sha256 must be given explicitly, so an approval always names exactly what was reviewed."""
    fetched, active = target_dir / FETCHED, target_dir / ACTIVE
    source = None
    for path in (fetched, active):
        if sha256_file(path) == sha256:
            source = path
            break
    if not source:
        raise RuntimeError("No fetched or current design file has sha256 %s; fetch again and review it." % sha256[:16])
    data = source.read_bytes()
    counts = design_counts(source)
    if "error" in counts:
        raise RuntimeError("The design file cannot be parsed, so it cannot be approved: %s" % counts["error"])
    if active.is_file() and sha256_file(active) != sha256:
        shutil.copy2(active, target_dir / PREVIOUS)
    _write_bytes(active, data)
    old = _read_json(target_dir / MANIFEST)
    history = old.get("history", [])
    if old.get("sha256"):
        history.append({k: old.get(k) for k in ("sha256", "approved_at", "approved_by", "note")})
    manifest = {"sha256": sha256, "bytes": len(data), "approved_at": now_iso(), "approved_by": by or getpass.getuser(),
                "note": note, "source": _read_json(target_dir / STATUS).get("source") or "UFM host /root/nscale_Compute.topo",
                "leaf_spine": counts["leaf_spine"], "gpu_ports": counts["gpu_ports"], "suspect_entries": counts["suspect"],
                "history": history[-20:]}
    _write_json(target_dir / MANIFEST, manifest)
    last = _read_json(target_dir / STATUS)
    if last:
        last.update(state="approved" if last.get("candidate_sha256") == sha256 else last.get("state"), approved_sha256=sha256,
                    active_sha256=sha256, diff=None if last.get("candidate_sha256") == sha256 else last.get("diff"))
        _write_json(target_dir / STATUS, last)
    return status(target_dir)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys
    here = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Review and approve the expected design topology (nscale_Compute.topo).")
    parser.add_argument("--dir", type=Path, default=here / "local-inputs" / "ufm", help="folder with the design files (default local-inputs/ufm)")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("status", help="show the approval state (default)")
    ing = sub.add_parser("ingest", help="register a fetched design file as the candidate")
    ing.add_argument("file", type=Path)
    ap = sub.add_parser("approve", help="approve the candidate (or the current copy) after reviewing its diff")
    ap.add_argument("--sha256", help="approve exactly this file (default: the candidate shown by status)")
    ap.add_argument("--note", default="", help="why it is approved (e.g. change ticket)")
    ap.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    if args.cmd == "ingest":
        print(json.dumps(ingest_fetched(args.file.read_bytes(), args.dir, source=str(args.file)), indent=1))
        return 0
    st = status(args.dir)
    if args.cmd != "approve":
        print(json.dumps(st, indent=1))
        return 0
    sha = args.sha256 or (st.get("candidate_sha256") if st["state"] == "change-pending" else st.get("active_sha256"))
    if not sha:
        print("Nothing to approve: no design file.")
        return 1
    print("Design state: %s" % st["state"])
    print("Approved now: %s (%s, by %s)" % ((st.get("approved_sha256") or "none")[:16], st.get("approved_at") or "-", st.get("approved_by") or "-"))
    print("To approve:   %s" % sha)
    fetched, active = args.dir / FETCHED, args.dir / ACTIVE
    if active.is_file() and fetched.is_file() and sha256_file(fetched) == sha and sha256_file(active) != sha:
        diff = diff_summary(active, fetched, limit=200)
        if "error" in diff:
            print("Cannot diff: %s" % diff["error"])
        else:
            print("Ports that change: %d (%d leaf-spine, %d leaf-GPU)" % (diff["changed_ports"], diff["leaf_spine_changes"], diff["gpu_changes"]))
            for c in diff["examples"]:
                print("  %-9s %-34s approved: %-40s fetched: %s" % (c["type"], " ".join(c["port"]), " ".join(c["approved"] or ["-"]), " ".join(c["fetched"] or ["-"])))
    if not args.yes and input("Approve this file as the expected design? Type yes: ").strip().lower() != "yes":
        print("Not approved.")
        return 1
    print(json.dumps(approve(args.dir, sha, note=args.note), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
