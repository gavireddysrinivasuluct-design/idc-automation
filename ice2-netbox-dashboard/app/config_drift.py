"""Running switch configuration vs the trusted standard rendered by network-deployment.

The standard ("what each switch should run") is network-deployment's own render of a
cluster's backend switches, made exactly as its Makefile's ZTP target makes them: the
`compile_configs` tag in check mode, which renders roles/nscale_nvos/templates/nvos.j2
into compiled/<host>/startup-proposed.yaml on this machine and never contacts a switch.

* Pinned: every render is of one commit of origin/main, kept under
  <store>/<cluster>/<commit>/ with a manifest (commit, its date, when rendered, a sha256
  per host). A commit already rendered is never rendered again; nothing edits the store.
* Private: rendered configs hold secrets from the Ansible vault, so the store is 0700/0600
  and the dashboard never serves a config, only which settings differ; values under
  password / secret / key / community / token paths are masked.
* Separate checkout: network-deployment is cloned for this purpose under
  ~/.cache/idc-automation/network-deployment (IDC_ND_CHECKOUT to change it), never the
  operator's own clone, and rendered in its own uv environment.

The running configuration comes from `nv config show --output json` (read-only) on each
switch. Both sides are NVUE documents (a list of {"header": ...}, {"set": {...}}): the
`set` parts are merged and flattened to paths, and the diff lists settings on the switch
but not in the standard (added), in the standard but not on the switch (removed), and
set to another value (changed). The switch prints every secret as "*", so for secrets only
their presence is checked ("secrets_unverified"), never the value. This file is identical in
the ICE2 and LON14 dashboards.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_URL = "https://github.com/nscaledev/network-deployment.git"
BRANCH = "main"
GROUP = "backend_network"  # the inventory group of the backend leaves and spines
CHECKOUT = Path(os.environ.get("IDC_ND_CHECKOUT") or "~/.cache/idc-automation/network-deployment").expanduser()
KEEP = 3  # rendered commits kept per cluster
SECRET = re.compile(r"password|secret|(^|[-_/])key($|[-_/])|community|token|psk|private", re.I)
MASK = "••••••"
# `nv config show` prints every secret as "*": the setting is there, its value cannot be read
HIDDEN = re.compile(r"^\*+$")
MAX_PATHS = 400  # per switch in the page payload

# Run with network-deployment's own Python (it has PyYAML): the hosts of GROUP in the
# cluster inventory, and each one's compiled YAML as JSON.
CONVERT = r"""
import json, sys, yaml
from pathlib import Path
inv, group, compiled, out = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4])
doc = yaml.safe_load(inv.read_text()) or {}
def hosts(name, seen=()):
    g = doc.get(name) or {}
    found = list((g.get("hosts") or {}).keys())
    for child in (g.get("children") or {}):
        if child not in seen:
            found += hosts(child, seen + (name,))
    return found
names = sorted(set(hosts(group)))
missing = []
for h in names:
    f = compiled / h / "startup-proposed.yaml"
    if not f.is_file():
        missing.append(h); continue
    (out / (h + ".json")).write_text(json.dumps(yaml.safe_load(f.read_text()), sort_keys=True))
print(json.dumps({"hosts": names, "missing": missing}))
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _run(cmd: list[str], cwd: Path | None = None, env: dict | None = None, timeout: int = 900) -> str:
    done = subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout)
    if done.returncode:
        tail = (done.stderr or done.stdout or "").strip().splitlines()[-12:]
        what = " ".join(Path(c).name if i == 0 else c for i, c in enumerate(cmd[:3]) if "\n" not in c)
        raise RuntimeError("%s failed (exit %d): %s" % (what, done.returncode, " | ".join(tail)))
    return done.stdout


def vault_password(profile: Path | None) -> str:
    """The Ansible vault password, read from 1Password on every render: [config_standard]
    vault_op_ref = op://vault/item/field in the device profile. Never printed or saved."""
    import configparser
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(profile or "")
    except configparser.Error as error:
        raise RuntimeError("The device profile cannot be read: %s" % error) from None
    ref = parser.get("config_standard", "vault_op_ref", fallback="").strip()
    if not ref:
        raise RuntimeError("Set vault_op_ref = op://vault/item/field under [config_standard] in %s (the network-deployment "
                           "Ansible vault password in 1Password)." % profile)
    if not ref.startswith("op://") or any(c in ref for c in "\"'`$\\\n "):
        raise RuntimeError("vault_op_ref under [config_standard] must be a plain op://vault/item/field reference.")
    if not shutil.which("op"):
        raise RuntimeError("1Password CLI (op) is not installed.")
    try:
        done = subprocess.run(["op", "--account", os.environ.get("OP_ACCOUNT") or "nscale.1password.com", "read", "--no-newline", ref],
                              text=True, capture_output=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise RuntimeError("1Password did not answer. Unlock the 1Password app and try again.") from None
    if done.returncode or not done.stdout:
        raise RuntimeError("The vault password could not be read from 1Password: %s" % ((done.stderr.strip().splitlines() or ["no value"])[-1][:200]))
    return done.stdout


# ---------- the standard -------------------------------------------------------------

def checkout(progress=lambda text: None) -> tuple[str, str]:
    """Clone or fetch network-deployment; (commit of origin/main, its commit date)."""
    if not (CHECKOUT / ".git").is_dir():
        progress("cloning network-deployment")
        CHECKOUT.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "clone", "--quiet", "--branch", BRANCH, REPO_URL, str(CHECKOUT)], timeout=600)
    else:
        progress("fetching network-deployment")
        _run(["git", "fetch", "--quiet", "origin", BRANCH], cwd=CHECKOUT, timeout=300)
    commit = _run(["git", "rev-parse", "origin/" + BRANCH], cwd=CHECKOUT).strip()
    date = _run(["git", "show", "-s", "--format=%cI", commit], cwd=CHECKOUT).strip()
    return commit, date


def _environment(progress) -> Path:
    """network-deployment's uv environment and Ansible collections, rebuilt only when
    uv.lock or requirements.yml change. Returns the venv's bin directory."""
    marker = CHECKOUT / ".idc-env"
    want = hashlib.sha256(b"".join((CHECKOUT / f).read_bytes() for f in ("uv.lock", "requirements.yml") if (CHECKOUT / f).is_file())).hexdigest()
    bin_dir = CHECKOUT / ".venv" / "bin"
    if marker.is_file() and marker.read_text().strip() == want and (bin_dir / "ansible-playbook").is_file():
        return bin_dir
    progress("preparing network-deployment's Python environment (uv sync)")
    _run(["uv", "sync", "--frozen"], cwd=CHECKOUT, timeout=900)
    progress("installing its Ansible collections")
    env = dict(os.environ, ANSIBLE_COLLECTIONS_PATH=str(CHECKOUT / ".collections"))
    _run([str(bin_dir / "ansible-galaxy"), "collection", "install", "-r", "requirements.yml", "-p", str(CHECKOUT / ".collections")],
         cwd=CHECKOUT, env=env, timeout=900)
    marker.write_text(want)
    return bin_dir


def render(cluster: str, store: Path, vault_password: str, progress=lambda text: None) -> dict:
    """Render the standard for `cluster`'s GROUP at origin/main (compile-only, no switch is
    contacted) into the store; a commit already there is kept as it is."""
    store = Path(store).resolve()  # the steps below run inside the checkout
    commit, date = checkout(progress)
    target = store / cluster / commit
    if (target / "manifest.json").is_file():
        _point(store, cluster, commit)
        return json.loads((target / "manifest.json").read_text())
    _run(["git", "checkout", "--quiet", "--detach", "--force", commit], cwd=CHECKOUT)
    bin_dir = _environment(progress)
    compiled = CHECKOUT / "compiled"
    for old in compiled.iterdir() if compiled.is_dir() else []:
        if old.name != ".gitkeep":
            shutil.rmtree(old) if old.is_dir() else old.unlink()
    progress("rendering %s %s (compile_configs, check mode)" % (cluster, GROUP))
    env = dict(os.environ, ANSIBLE_VAULT_PASSWORD=vault_password, ANSIBLE_COLLECTIONS_PATH=str(CHECKOUT / ".collections"),
               PATH=str(bin_dir) + os.pathsep + os.environ.get("PATH", ""), ANSIBLE_DIFF_DIR=str(CHECKOUT / "diffs"))
    started = time.monotonic()
    _run([str(bin_dir / "ansible-playbook"), "playbooks/%s.yml" % cluster, "--tags", "compile_configs",
          "-i", "inventories/%s/hosts.yml" % cluster, "--vault-password-file=scripts/echo-vault-pass.sh",
          "-e", "user=%s" % (os.environ.get("USER") or "dashboard"), "--check", "-f25", "--limit", GROUP],
         cwd=CHECKOUT, env=env, timeout=1800)
    partial = store / cluster / (commit + ".part")
    shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir(parents=True, mode=0o700)
    found = json.loads(_run([str(bin_dir / "python"), "-c", CONVERT, "inventories/%s/hosts.yml" % cluster, GROUP,
                             str(compiled), str(partial)], cwd=CHECKOUT))
    hosts = {}
    for f in sorted(partial.glob("*.json")):
        f.chmod(0o600)
        hosts[f.stem] = hashlib.sha256(f.read_bytes()).hexdigest()
    if not hosts:
        raise RuntimeError("network-deployment rendered no %s config for %s" % (GROUP, cluster))
    manifest = {"cluster": cluster, "group": GROUP, "repo": REPO_URL, "branch": BRANCH, "commit": commit, "committed_at": date,
                "rendered_at": now_iso(), "seconds": round(time.monotonic() - started), "hosts": hosts,
                "inventory_hosts": found["hosts"], "not_rendered": found["missing"],
                "how": "ansible-playbook playbooks/%s.yml --tags compile_configs --check --limit %s" % (cluster, GROUP)}
    (partial / "manifest.json").write_text(json.dumps(manifest, indent=1))
    (partial / "manifest.json").chmod(0o600)
    partial.replace(target)
    _point(store, cluster, commit)
    for old in sorted((p for p in (store / cluster).iterdir() if p.is_dir() and p.name != commit and not p.name.endswith(".part")),
                      key=lambda p: p.stat().st_mtime)[:-(KEEP - 1) or None]:
        shutil.rmtree(old, ignore_errors=True)
    return manifest


def _point(store: Path, cluster: str, commit: str) -> None:
    current = store / cluster / "current.json"
    current.write_text(json.dumps({"commit": commit, "at": now_iso()}))
    current.chmod(0o600)


def standard(store: Path, cluster: str) -> tuple[dict | None, dict[str, dict]]:
    """(manifest, {host: normalised config}) of the current standard, or (None, {})."""
    try:
        commit = json.loads((store / cluster / "current.json").read_text())["commit"]
        folder = store / cluster / commit
        manifest = json.loads((folder / "manifest.json").read_text())
    except (OSError, ValueError, KeyError):
        return None, {}
    return manifest, {h: normalise(json.loads((folder / (h + ".json")).read_text())) for h in manifest["hosts"]
                      if (folder / (h + ".json")).is_file()}


# ---------- the running configuration -------------------------------------------------

MARKED = re.compile(r"__\w+?_COMMAND_\d+_START__\s*(.*?)\s*__\w+?_COMMAND_\d+_END__", re.S)


def parse_running(text: str) -> dict | None:
    """The collector's raw output of `nv config show --output json`, normalised; None when it
    holds no JSON document."""
    m = MARKED.search(text or "")
    body = (m.group(1) if m else text or "").strip()
    start = min([i for i in (body.find("["), body.find("{")) if i >= 0], default=-1)
    if start < 0:
        return None
    try:
        return normalise(json.loads(body[start:]))
    except ValueError:
        return None


# ---------- comparison ------------------------------------------------------------------

def _merge(into: dict, more: dict) -> None:
    for key, value in more.items():
        if isinstance(value, dict) and isinstance(into.get(key), dict):
            _merge(into[key], value)
        else:
            into[key] = value


def normalise(doc) -> dict:
    """An NVUE document as one nested dict: every `set` part merged, `header` dropped."""
    out: dict = {}
    parts = doc if isinstance(doc, list) else [doc]
    for part in parts:
        if isinstance(part, dict):
            _merge(out, part.get("set", {} if "header" in part else part) or {})
    return out


def flatten(tree, path: str = "") -> dict[str, object]:
    """{"interface/sw1p1/type": "swp", ...}; an empty dict is a leaf (a key set with no value)."""
    if isinstance(tree, dict) and tree:
        out = {}
        for key, value in tree.items():
            out.update(flatten(value, (path + "/" if path else "") + str(key)))
        return out
    return {path: tree if not isinstance(tree, dict) else "{}"}


def shown(path: str, value) -> object:
    return MASK if SECRET.search(path) else value


def diff(want: dict, have: dict) -> dict:
    """Settings added on the switch, removed from it, and changed, against the standard."""
    a, b = flatten(want), flatten(have)

    def parents(side: dict) -> set[str]:
        out = set()
        for p in side:
            parts = p.split("/")
            out.update("/".join(parts[:i]) for i in range(1, len(parts)))
        return out

    # a key set with no value ({}) on one side and with settings below it on the other is
    # not itself missing: only the settings below it are reported
    pa, pb = parents(a), parents(b)
    added = sorted(p for p in b if p not in a and not (b[p] == "{}" and p in pa))
    removed = sorted(p for p in a if p not in b and not (a[p] == "{}" and p in pb))
    differ = [p for p in a if p in b and a[p] != b[p]]
    hidden = sorted(p for p in differ if isinstance(b[p], str) and HIDDEN.match(b[p]))
    changed = sorted(p for p in differ if p not in hidden)
    return {"added": [[p, shown(p, b[p])] for p in added], "removed": [[p, shown(p, a[p])] for p in removed],
            "changed": [[p, shown(p, a[p]), shown(p, b[p])] for p in changed], "hidden": hidden}


def compare(standard_by_host: dict[str, dict], running_by_host: dict[str, dict | None], hosts: list[str]) -> dict:
    """Per switch: state (match, drift, no-standard, no-running) and its differences."""
    out = {}
    for host in hosts:
        want, have = standard_by_host.get(host), running_by_host.get(host)
        if want is None:
            out[host] = {"state": "no-standard"}
        elif have is None:
            out[host] = {"state": "no-running" if host not in running_by_host else "unreadable"}
        else:
            d = diff(want, have)
            hidden = d.pop("hidden")
            n = {k: len(v) for k, v in d.items()}
            out[host] = {"state": "drift" if any(n.values()) else "match", "counts": n, "settings": len(flatten(want)),
                         "secrets_unverified": hidden, **{k: v[:MAX_PATHS] for k, v in d.items()},
                         "truncated": any(x > MAX_PATHS for x in n.values())}
    return out
