#!/usr/bin/env python3
"""Fetch UFM's fabric files for the cabling check, without a terminal.

Used by the dashboard's "Fetch from UFM" button (POST /api/cabling/fetch).

Path: this Mac -> one `tsh ssh` session to the jump host -> a small worker there
-> ssh to the active UFM host -> `docker exec ufm` reads three files UFM already
writes and returns them as one tar bundle:

  * opt/ufm/tmp/fabric_analysis/ibdiagnet.out/ibdiagnet2.lst      current fabric scan
  * opt/ufm/shared_config_files/periodicTopo/master.topo          UFM master topology
  * opt/ufm/shared_config_files/reports/TopologyCompare/TopologyCompare.json  (link followed)

Read-only: nothing is sent to the fabric and nothing on UFM changes. The UFM host
password comes from macOS Keychain (scripts/configure_ufm_access.sh) and is written
only to the worker's stdin, never to argv, the environment or a file.
"""

from __future__ import annotations

import base64
import configparser
import gzip
import io
import json
import os
import shlex
import shutil
import subprocess
import tarfile
import threading
import time
from pathlib import Path

SCAN = "opt/ufm/tmp/fabric_analysis/ibdiagnet.out/ibdiagnet2.lst"
MASTER = "opt/ufm/shared_config_files/periodicTopo/master.topo"
REPORT = "opt/ufm/shared_config_files/reports/TopologyCompare/TopologyCompare.json"
BEGIN, END = "__ICE2_UFM_BUNDLE_BEGIN__", "__ICE2_UFM_BUNDLE_END__"

# Runs on the jump host. Logs in to each UFM host in turn under a PTY (OpenSSH reads
# the password from the controlling terminal), and prints one JSON line per host.
WORKER = r'''
import json, os, pty, select, signal, sys, time
cfg = json.loads(sys.stdin.readline())
secret = cfg.pop("password").encode("utf-8") + b"\n"
def emit(o):
    sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
for host in cfg["hosts"]:
    emit({"event": "trying", "host": host})
    cmd = ["ssh", "-o", "ConnectTimeout=10", "-o", "NumberOfPasswordPrompts=1", "-o", "StrictHostKeyChecking=yes",
           "%s@%s" % (cfg["user"], host), cfg["command"]]
    pid, master = pty.fork()
    if pid == 0:
        try: os.execvp(cmd[0], cmd)
        finally: os._exit(127)
    out, sent, tail, status = [], False, b"", None
    deadline = time.time() + cfg.get("timeout", 150)
    while True:
        if time.time() > deadline:
            try: os.kill(pid, signal.SIGKILL)
            except OSError: pass
            break
        r, _, _ = select.select([master], [], [], 0.5)
        if r:
            try: data = os.read(master, 1 << 20)
            except OSError: data = b""
            if not data: break
            out.append(data)
            if not sent:
                tail = (tail + data)[-256:]
                if b"password:" in tail.lower():
                    os.write(master, secret); sent = True
                    emit({"event": "authenticating", "host": host})
        else:
            done, st = os.waitpid(pid, os.WNOHANG)
            if done: status = st; break
    os.close(master)
    if status is None:
        try: _, status = os.waitpid(pid, 0)
        except ChildProcessError: status = 0
    text = b"".join(out).decode("utf-8", "replace")
    b, e = text.find(cfg["begin"]), text.find(cfg["end"])
    raw = text[b + len(cfg["begin"]):e] if b >= 0 and e > b else ""
    # Errors (e.g. "No such container" on a standby UFM) share the terminal stream:
    # accept only a base64 gzip bundle, which always starts with "H4sI".
    tokens = [t for t in raw.split() if t.startswith("H4sI")]
    data = max(tokens, key=len) if tokens else ""
    if data:
        emit({"event": "bundle", "host": host, "data": data}); break
    lines = [l.strip() for l in text.replace("\r", "").splitlines()
             if l.strip() and cfg["begin"] not in l and cfg["end"] not in l and "password:" not in l.lower()]
    emit({"event": "failed", "host": host, "detail": (lines[-1] if lines else "no response")[:200]})
emit({"event": "done"})
'''


def read_profile(path: Path) -> dict:
    parser = configparser.ConfigParser(interpolation=None)
    if not parser.read(path, encoding="utf-8"):
        raise RuntimeError("Device profile not found: %s" % path)
    if not parser.has_section("ufm"):
        raise RuntimeError("UFM access is not set up yet. Run ./scripts/configure_ufm_access.sh, then restart the service.")
    ice2 = dict(parser.items("ice2")) if parser.has_section("ice2") else {}
    ufm = dict(parser.items("ufm"))
    for key in ("ufm_user", "keychain_service", "keychain_account"):
        if not ufm.get(key):
            raise RuntimeError("The [ufm] profile section is missing %s. Run ./scripts/configure_ufm_access.sh again." % key)
    return {"jump_host": ice2.get("jump_host", "jmp0"), "jump_user": ice2.get("jump_user") or ice2.get("ssh_user", ""),
            "ufm_user": ufm["ufm_user"], "ufm_hosts": (ufm.get("ufm_hosts") or "10.1.67.190 10.1.67.191").split(),
            "service": ufm["keychain_service"], "account": ufm["keychain_account"]}


def keychain_password(service: str, account: str) -> str:
    if not shutil.which("security"):
        raise RuntimeError("macOS Keychain (security) is required for the UFM password.")
    done = subprocess.run(["security", "find-generic-password", "-s", service, "-a", account, "-w"],
                          text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if done.returncode or not done.stdout.strip():
        raise RuntimeError("The UFM password is not in Keychain. Run ./scripts/configure_ufm_access.sh.")
    return done.stdout.rstrip("\n")


def save_bundle(blob: bytes, target_dir: Path) -> dict:
    """Unpack UFM's tar bundle into target_dir as gzip files, keeping UFM's timestamps."""
    target_dir.mkdir(parents=True, exist_ok=True)
    saved = {}
    names = {SCAN: "ibdiagnet2.lst.gz", MASTER: "master.topo.gz", REPORT: "topology-compare.json.gz"}
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        members = {m.name.lstrip("/"): m for m in tar.getmembers() if m.isfile()}
        if SCAN not in members:
            raise RuntimeError("UFM's bundle has no fabric scan.")
        for source, name in names.items():
            member = members.get(source)
            if not member:
                continue
            data = tar.extractfile(member).read()
            target = target_dir / name
            if target.is_file():
                shutil.copy2(target, target.with_name(name.replace(".gz", ".previous.gz")))
            partial = target.with_name(name + ".part")
            with gzip.open(partial, "wb", compresslevel=6) as handle:
                handle.write(data)
            os.utime(partial, (member.mtime, member.mtime))
            os.chmod(partial, 0o600)
            partial.replace(target)
            saved[name] = {"bytes": len(data), "ufm_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(member.mtime))}
    return saved


def fetch(profile_path: Path, target_dir: Path, progress: dict, timeout: int = 180) -> dict:
    """Run the whole fetch; `progress` is updated in place for the dashboard."""
    prof = read_profile(profile_path)
    if not shutil.which("tsh"):
        raise RuntimeError("Teleport CLI (tsh) is required.")
    if subprocess.run(["tsh", "status"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        raise RuntimeError("Teleport login expired. Run: tsh login")
    password = keychain_password(prof["service"], prof["account"])
    remote = ("echo %s; docker exec ufm test -s /%s && docker exec ufm sh -c %s | base64 -w0; echo; echo %s"
              % (BEGIN, SCAN, shlex.quote("cd / && tar czhf - --ignore-failed-read %s %s %s 2>/dev/null" % (SCAN, MASTER, REPORT)), END))
    payload = {"password": password, "user": prof["ufm_user"], "hosts": prof["ufm_hosts"], "command": remote,
               "begin": BEGIN, "end": END, "timeout": timeout - 20}
    del password
    encoded = base64.b64encode(WORKER.encode("utf-8")).decode("ascii")
    bootstrap = "import base64,sys;exec(base64.b64decode(sys.argv[1]))"
    command = ["tsh", "ssh", "--login", prof["jump_user"], prof["jump_host"], "python3 -u -c %s %s" % (shlex.quote(bootstrap), encoded)]
    progress.update(step="connecting", detail="Teleport session to %s" % prof["jump_host"])
    child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    watchdog = threading.Timer(timeout, child.kill)
    watchdog.daemon = True
    watchdog.start()
    errors, bundle, host = [], None, None
    try:
        child.stdin.write(json.dumps(payload) + "\n")
        child.stdin.close()
        payload.clear()
        for line in child.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.get("event")
            if kind == "trying":
                progress.update(step="connecting", detail="UFM %s via %s" % (event["host"], prof["jump_host"]))
            elif kind == "authenticating":
                progress.update(step="reading", detail="reading UFM files on %s" % event["host"])
            elif kind == "failed":
                errors.append("%s: %s" % (event["host"], (event.get("detail") or "no data").splitlines()[-1][:160]))
            elif kind == "bundle":
                bundle, host = event["data"], event["host"]
        child.wait()
        stderr = child.stderr.read()
    finally:
        watchdog.cancel()
    if not bundle:
        if child.returncode and not errors:
            raise RuntimeError("Jump-host worker failed (exit %s): %s" % (child.returncode, stderr.strip()[-200:]))
        raise RuntimeError("No UFM host returned its files. " + " | ".join(errors[-2:]))
    progress.update(step="saving", detail="saving files from UFM %s" % host)
    try:
        blob = base64.b64decode(bundle, validate=True)
    except ValueError as error:
        raise RuntimeError("UFM returned an unreadable bundle (%s); try again." % error)
    saved = save_bundle(blob, target_dir)
    return {"host": host, "files": saved, "skipped": errors}
