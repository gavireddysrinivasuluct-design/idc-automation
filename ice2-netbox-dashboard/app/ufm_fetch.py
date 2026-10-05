#!/usr/bin/env python3
"""Fetch UFM's fabric data for the cabling check, without a terminal.

Used by the dashboard's "Fetch from UFM" button (POST /api/cabling/fetch).
Two read-only sources, both through one `tsh ssh` session to the jump host:

1. Live links (preferred, when a UFM web user is set up): the jump host asks the
   UFM REST API for /ufmRest/resources/links, UFM's current view of every link.
   This is what UFM knows right now, so the comparison is current. The answer is
   converted to the ibdiagnet2.lst layout so the cabling engine is unchanged.
   The UFM TLS certificate is pinned on first use (local-inputs/ufm/tls-pins.json).

2. UFM's files (needs the UFM host SSH login): ssh to the active UFM host, and
`docker exec ufm` reads the UFM files while the host supplies the approved design:

  * opt/ufm/tmp/fabric_analysis/ibdiagnet.out/ibdiagnet2.lst      current fabric scan
  * opt/ufm/shared_config_files/periodicTopo/master.topo          UFM master topology
  * opt/ufm/shared_config_files/reports/TopologyCompare/TopologyCompare.json  (link followed)
  * /root/nscale_Compute.topo                                                   (host file)

Every fetch also refreshes `/root/nscale_Compute.topo` before the cabling check.
It never silently compares a new UFM snapshot against an older design copy.

Read-only: nothing is sent to the fabric and nothing on UFM changes (REST is GET
only). Passwords come from macOS Keychain (scripts/configure_ufm_access.sh) and
are written only to the worker's stdin, never to argv, the environment or a file.
"""

from __future__ import annotations

import base64
import configparser
import gzip
import io
import hashlib
import json
import os
import re
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
DESIGN = "/root/nscale_Compute.topo"
LINKS_PATH = "/ufmRest/resources/links"
BEGIN, END = "__ICE2_UFM_BUNDLE_BEGIN__", "__ICE2_UFM_BUNDLE_END__"
DESIGN_BEGIN, DESIGN_END = "__ICE2_UFM_DESIGN_BEGIN__", "__ICE2_UFM_DESIGN_END__"

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
    db, de = raw.find(cfg["design_begin"]), raw.find(cfg["design_end"])
    design = ""
    if db >= 0 and de > db:
        design = "".join(raw[db + len(cfg["design_begin"]):de].split())
        raw = raw[:db]
    # Errors (e.g. "No such container" on a standby UFM) share the terminal stream:
    # accept only a base64 gzip bundle, which always starts with "H4sI".
    tokens = [t for t in raw.split() if t.startswith("H4sI")]
    data = max(tokens, key=len) if tokens else ""
    if data and design:
        emit({"event": "bundle", "host": host, "data": data, "design": design}); break
    lines = [l.strip() for l in text.replace("\r", "").splitlines()
             if l.strip() and cfg["begin"] not in l and cfg["end"] not in l and "password:" not in l.lower()]
    emit({"event": "failed", "host": host, "detail": (lines[-1] if lines else "no response")[:200]})
emit({"event": "done"})
'''


# Runs on the jump host: one HTTPS GET per UFM host, first good answer wins.
REST_WORKER = r"""
import base64, gzip, hashlib, http.client, json, ssl, sys
cfg = json.loads(sys.stdin.readline())
auth = "Basic " + base64.b64encode(("%s:%s" % (cfg["user"], cfg.pop("password"))).encode("utf-8")).decode("ascii")
def emit(o):
    sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
for host in cfg["hosts"]:
    emit({"event": "trying", "host": host})
    try:
        conn = http.client.HTTPSConnection(host, 443, timeout=cfg.get("timeout", 90), context=ctx)
        conn.connect()
        fp = hashlib.sha256(conn.sock.getpeercert(binary_form=True)).hexdigest()
        pin = cfg["pins"].get(host)
        if pin and pin != fp:
            emit({"event": "failed", "host": host, "detail": "TLS certificate changed (pinned %s..., now %s...). Verify with the UFM owner, then remove %s from tls-pins.json." % (pin[:16], fp[:16], host)}); conn.close(); continue
        emit({"event": "authenticating", "host": host})
        conn.request("GET", cfg["path"], headers={"Authorization": auth, "Accept": "application/json"})
        resp = conn.getresponse(); body = resp.read(); conn.close()
    except Exception as error:
        emit({"event": "failed", "host": host, "detail": "%s: %s" % (type(error).__name__, error)}); continue
    if resp.status != 200:
        emit({"event": "failed", "host": host, "detail": "HTTP %s %s" % (resp.status, resp.reason) + (" (wrong UFM web user or password)" if resp.status in (401, 403) else "")}); continue
    try:
        data = json.loads(body)
    except ValueError:
        emit({"event": "failed", "host": host, "detail": "not JSON (standby UFM or login page?)"}); continue
    if not isinstance(data, list) or not data:
        emit({"event": "failed", "host": host, "detail": "no links in the answer (standby UFM?)"}); continue
    emit({"event": "links", "host": host, "fingerprint": fp, "count": len(data), "data": base64.b64encode(gzip.compress(body)).decode("ascii")}); break
emit({"event": "done"})
"""


def read_profile(path: Path) -> dict:
    parser = configparser.ConfigParser(interpolation=None)
    if not parser.read(path, encoding="utf-8"):
        raise RuntimeError("Device profile not found: %s" % path)
    if not parser.has_section("ufm"):
        raise RuntimeError("UFM access is not set up yet. Run ./scripts/configure_ufm_access.sh, then restart the service.")
    ice2 = dict(parser.items("ice2")) if parser.has_section("ice2") else {}
    ufm = dict(parser.items("ufm"))
    has_ssh = all(ufm.get(k) for k in ("ufm_user", "keychain_service", "keychain_account"))
    has_rest = all(ufm.get(k) for k in ("rest_user", "rest_keychain_service", "rest_keychain_account"))
    if not (has_ssh or has_rest):
        raise RuntimeError("The [ufm] profile section has no complete login. Run ./scripts/configure_ufm_access.sh again.")
    hosts = (ufm.get("ufm_hosts") or "10.1.67.190 10.1.67.191").split()
    return {"jump_host": ice2.get("jump_host", "jmp0"), "jump_user": ice2.get("jump_user") or ice2.get("ssh_user", ""),
            "ufm_hosts": hosts, "has_ssh": has_ssh, "has_rest": has_rest,
            "ufm_user": ufm.get("ufm_user", ""), "service": ufm.get("keychain_service", ""), "account": ufm.get("keychain_account", ""),
            "rest_user": ufm.get("rest_user", ""), "rest_hosts": (ufm.get("rest_hosts") or " ".join(hosts)).split(),
            "rest_service": ufm.get("rest_keychain_service", ""), "rest_account": ufm.get("rest_keychain_account", "")}


def keychain_password(service: str, account: str) -> str:
    if not shutil.which("security"):
        raise RuntimeError("macOS Keychain (security) is required for the UFM password.")
    done = subprocess.run(["security", "find-generic-password", "-s", service, "-a", account, "-w"],
                          text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if done.returncode or not done.stdout.strip():
        raise RuntimeError("The UFM password (Keychain item %s) is missing. Run ./scripts/configure_ufm_access.sh." % service)
    return done.stdout.rstrip("\n")


def save_bundle(blob: bytes, target_dir: Path, skip: tuple = ()) -> dict:
    """Unpack UFM's tar bundle into target_dir as gzip files, keeping UFM's timestamps."""
    target_dir.mkdir(parents=True, exist_ok=True)
    saved = {}
    names = {SCAN: "ibdiagnet2.lst.gz", MASTER: "master.topo.gz", REPORT: "topology-compare.json.gz"}
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        members = {m.name.lstrip("/"): m for m in tar.getmembers() if m.isfile()}
        if SCAN not in members:
            raise RuntimeError("UFM's bundle has no fabric scan.")
        for source, name in names.items():
            member = None if source in skip else members.get(source)
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


def save_design(data: bytes, target_dir: Path) -> dict:
    """Atomically replace the approved design copied from the UFM host."""
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "nscale_Compute.topo"
    if target.is_file():
        shutil.copy2(target, target.with_name("nscale_Compute.previous.topo"))
    partial = target.with_name(target.name + ".part")
    partial.write_bytes(data)
    os.chmod(partial, 0o600)
    partial.replace(target)
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def fetch_files(prof: dict, target_dir: Path, progress: dict, timeout: int = 180, skip: tuple = ()) -> dict:
    """UFM files plus the approved host design over one SSH session."""
    password = keychain_password(prof["service"], prof["account"])
    archive = shlex.quote("cd / && tar czhf - --ignore-failed-read %s %s %s 2>/dev/null" % (SCAN, MASTER, REPORT))
    remote = ("docker exec ufm test -s /%s && test -s %s && { echo %s; docker exec ufm sh -c %s | base64 -w0; echo; "
              "echo %s; base64 -w0 %s; echo; echo %s; echo %s; }"
              % (SCAN, shlex.quote(DESIGN), BEGIN, archive, DESIGN_BEGIN, shlex.quote(DESIGN), DESIGN_END, END))
    payload = {"password": password, "user": prof["ufm_user"], "hosts": prof["ufm_hosts"], "command": remote,
               "begin": BEGIN, "end": END, "design_begin": DESIGN_BEGIN, "design_end": DESIGN_END,
               "timeout": timeout - 20}
    del password
    encoded = base64.b64encode(WORKER.encode("utf-8")).decode("ascii")
    bootstrap = "import base64,sys;exec(base64.b64decode(sys.argv[1]))"
    command = ["tsh", "ssh", "--login", prof["jump_user"], prof["jump_host"], "python3 -u -c %s %s" % (shlex.quote(bootstrap), encoded)]
    progress.update(step="connecting", detail="Teleport session to %s" % prof["jump_host"])
    child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    watchdog = threading.Timer(timeout, child.kill)
    watchdog.daemon = True
    watchdog.start()
    errors, bundle, design_blob, host = [], None, None, None
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
                bundle, design_blob, host = event["data"], event.get("design"), event["host"]
        child.wait()
        stderr = child.stderr.read()
    finally:
        watchdog.cancel()
    if not bundle or not design_blob:
        if child.returncode and not errors:
            raise RuntimeError("Jump-host worker failed (exit %s): %s" % (child.returncode, stderr.strip()[-200:]))
        raise RuntimeError("No UFM host returned its files. " + " | ".join(errors[-2:]))
    progress.update(step="saving", detail="saving UFM files and approved design from %s" % host)
    try:
        blob = base64.b64decode(bundle, validate=True)
        design = base64.b64decode(design_blob, validate=True)
    except (ValueError, TypeError) as error:
        raise RuntimeError("UFM returned an unreadable bundle (%s); try again." % error)
    saved = save_bundle(blob, target_dir, skip)
    saved["nscale_Compute.topo"] = save_design(design, target_dir)
    (target_dir / ".files-fetched-at").write_text(str(time.time()))
    return {"host": host, "files": saved, "skipped": errors}


def _pick(record: dict, *names):
    for name in names:
        value = record.get(name)
        if value not in (None, ""):
            return value
    return None


SW_AGG = re.compile(r"^([^:]+):(?:sw\d+p\d+|FNM\d+)$")  # one record per cable, all 4 planes up (FNM1 = sw73p1)
SW_CHIP = re.compile(r"^([^:]+):A(\d+)/(\d+)$")    # one record per plane (chip A1..A4)
SW_OTHER = re.compile(r"^(\S*-swi-\S+?):(\S+)$")   # e.g. FNM1 (UFM management port)
LST_END = re.compile(r"\{ (?:SW|CA) Ports:\S+ SystemGUID:\S+ NodeGUID:(\S+) .*?\{([^}]*)\} LID:")


def scan_names(path: Path) -> dict:
    """Node GUID -> node description from the last scan file (adapter names such as
    'nvl72d031-T14 mlx5_2'); UFM REST shows host:interface names instead."""
    names = {}
    try:
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith("{"):
                    for guid, desc in LST_END.findall(line):
                        names[guid.lower()] = desc
    except (OSError, EOFError):
        pass
    return names


def links_to_lst(links: list, host: str, names: dict | None = None) -> tuple[str, dict]:
    """UFM REST links -> ibdiagnet2.lst lanes the cabling engine already reads.

    UFM REST (checked on ICE2, UFM 6.x) reports:
      * one record per cable when all four planes are up: switch end 'bel1:sw37p1',
        port = NVOS number; adapter end 'host:ibB..p1s3' -> expanded to 4 lanes, U1..U4
      * one record per plane otherwise: switch end 'bel2:A2/58' (chip 2, port 58),
        adapter end '<name> mlx5_0', port = plane
    Adapter names come from the last scan (by node GUID), because REST shows
    host:interface names that do not carry the mlx5 index used for the rail.
    """
    names = names or {}
    out = ['# This database file was created by the IDC dashboard from UFM REST %s' % LINKS_PATH,
           '# Running version: "UFM REST live links (%s)"' % host, '']
    stats = {"records": len(links), "lanes": 0, "cables": 0, "planes": 0, "skipped": 0, "unnamed_new": 0}

    def ends(link, side, planes):
        desc = str(link.get(side + "_port_node_description") or "").strip()
        port = int(re.findall(r"\d+", str(link.get(side + "_port") or "0"))[-1] or 0)
        guid = str(link.get(side + "_port_name") or "").split("_")[0].lower() or str(link.get(side + "_guid") or "0").lower()
        m = SW_AGG.match(desc)
        if m:
            return [("SW", "MF0;%s:Q3400_RA/U%d" % (m.group(1), u), port, guid) for u in planes]
        m = SW_CHIP.match(desc)
        if m:
            return [("SW", "MF0;%s:Q3400_RA/U%s" % (m.group(1), m.group(2)), int(m.group(3)), guid)]
        m = SW_OTHER.match(desc)
        if m:
            return [("SW", "MF0;%s:Q3400_RA/U1" % m.group(1), port, guid)]
        name = names.get(guid)
        if not name:
            name = desc if " mlx5_" in desc else desc.split(":")[0]
            stats["unnamed_new"] += 1
        if "{" in name or "}" in name:
            return []
        return [("CA", name, p, guid) for p in (planes if len(planes) > 1 else [port])]

    for link in links:
        agg = any(SW_AGG.match(str(link.get(s + "_port_node_description") or "")) for s in ("source", "destination"))
        planes = [1, 2, 3, 4] if agg else [0]
        a, b = ends(link, "source", planes), ends(link, "destination", planes)
        if not a or not b or len(a) != len(b):
            stats["skipped"] += 1
            continue
        stats["cables" if agg else "planes"] += 1
        for x, y in zip(a, b):
            out.append("%s %s PHY=4x LOG=ACT SPD=?" % tuple(
                "{ %s Ports:00 SystemGUID:%s NodeGUID:%s PortGUID:%s VenID:0 DevID:0 Rev:0 {%s} LID:0 PN:%x }"
                % (kind, g, g, g, desc, num) for kind, desc, num, g in (x, y)))
            stats["lanes"] += 1
    if not stats["lanes"]:
        fields = sorted(links[0].keys()) if links and isinstance(links[0], dict) else []
        raise RuntimeError("UFM REST answered with %d links, but none could be read. Fields seen: %s"
                           % (len(links), ", ".join(fields)[:300]))
    return "\n".join(out) + "\n", stats


def check_plausible(text: str, previous: Path) -> None:
    """Refuse a conversion that would wipe the dashboard (e.g. an unknown REST format):
    the live result must name about as many leaf-spine lanes as the last scan."""
    import ufm_cabling
    def leaf_spine(lines):
        n = 0
        for line in lines:
            if line.startswith("{"):
                hosts = [m.group(1) for m in (ufm_cabling.SWITCH.match(d) for _, d, _ in ufm_cabling.END.findall(line)) if m]
                n += len(hosts) == 2 and any(ufm_cabling.LEAF.search(h) for h in hosts) and any(ufm_cabling.SPINE.search(h) for h in hosts)
        return n
    now = leaf_spine(text.splitlines())
    try:
        with gzip.open(previous, "rt", encoding="utf-8", errors="replace") as handle:
            before = leaf_spine(handle)
    except (OSError, EOFError):
        before = 0
    if now == 0 or (before and now < before * 0.5):
        raise RuntimeError("UFM live links look incomplete (%d leaf-spine lanes, last scan had %d); the previous data is kept." % (now, before))


def fetch_rest(prof: dict, target_dir: Path, progress: dict, timeout: int = 120) -> dict:
    """Live links from the UFM REST API, saved as the current scan."""
    pins_file = target_dir / "tls-pins.json"
    try:
        pins = json.loads(pins_file.read_text())
    except (OSError, ValueError):
        pins = {}
    password = keychain_password(prof["rest_service"], prof["rest_account"])
    payload = {"password": password, "user": prof["rest_user"], "hosts": prof["rest_hosts"], "path": LINKS_PATH,
               "pins": pins, "timeout": timeout - 30}
    del password
    events = _run_worker(prof, REST_WORKER, payload, progress, timeout,
                         {"trying": ("connecting", "UFM REST on %s via " + prof["jump_host"]),
                          "authenticating": ("reading", "reading live links from UFM %s")})
    got = next((e for e in events if e.get("event") == "links"), None)
    errors = ["%s: %s" % (e["host"], e.get("detail", "")) for e in events if e.get("event") == "failed"]
    if not got:
        raise RuntimeError("No UFM host answered the REST API. " + " | ".join(errors[-2:]))
    progress.update(step="saving", detail="converting %s live links from UFM %s" % (format(got["count"], ","), got["host"]))
    raw = gzip.decompress(base64.b64decode(got["data"], validate=True))
    text, stats = links_to_lst(json.loads(raw), got["host"], scan_names(target_dir / "ibdiagnet2.lst.gz"))
    check_plausible(text, target_dir / "ibdiagnet2.lst.gz")
    target_dir.mkdir(parents=True, exist_ok=True)
    if pins.get(got["host"]) != got["fingerprint"]:
        pins[got["host"]] = got["fingerprint"]
        pins_file.write_text(json.dumps(pins, indent=1) + "\n")
        os.chmod(pins_file, 0o600)
    for name, data in (("links.json.gz", raw), ("ibdiagnet2.lst.gz", text.encode("utf-8"))):
        target = target_dir / name
        if name.startswith("ibdiag") and target.is_file():
            shutil.copy2(target, target.with_name("ibdiagnet2.lst.previous.gz"))
        partial = target.with_name(name + ".part")
        with gzip.open(partial, "wb", compresslevel=6) as handle:
            handle.write(data)
        os.chmod(partial, 0o600)
        partial.replace(target)
    return {"host": got["host"], "links": got["count"], "stats": stats, "skipped": errors,
            "files": {"ibdiagnet2.lst.gz": {"bytes": len(text), "ufm_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}}}


def _run_worker(prof: dict, script: str, payload: dict, progress: dict, timeout: int, steps: dict) -> list:
    """Run a worker on the jump host with the JSON payload on stdin; return its events."""
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    bootstrap = "import base64,sys;exec(base64.b64decode(sys.argv[1]))"
    command = ["tsh", "ssh", "--login", prof["jump_user"], prof["jump_host"], "python3 -u -c %s %s" % (shlex.quote(bootstrap), encoded)]
    progress.update(step="connecting", detail="Teleport session to %s" % prof["jump_host"])
    child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    watchdog = threading.Timer(timeout, child.kill)
    watchdog.daemon = True
    watchdog.start()
    events = []
    try:
        child.stdin.write(json.dumps(payload) + "\n")
        child.stdin.close()
        payload.clear()
        for line in child.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            events.append(event)
            step = steps.get(event.get("event"))
            if step:
                progress.update(step=step[0], detail=step[1] % event.get("host", ""))
        child.wait()
        stderr = child.stderr.read()
    finally:
        watchdog.cancel()
    if child.returncode and not any(e.get("event") in ("links", "failed") for e in events):
        raise RuntimeError("Jump-host worker failed (exit %s): %s" % (child.returncode, stderr.strip()[-200:]))
    return events


def fetch(profile_path: Path, target_dir: Path, progress: dict, timeout: int = 180) -> dict:
    """Run the whole fetch; `progress` is updated in place for the dashboard."""
    prof = read_profile(profile_path)
    if not shutil.which("tsh"):
        raise RuntimeError("Teleport CLI (tsh) is required.")
    if subprocess.run(["tsh", "status"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        raise RuntimeError("Teleport login expired. Run: tsh login")
    if not prof["has_ssh"]:
        raise RuntimeError("A UFM host SSH login is required: every fetch refreshes /root/nscale_Compute.topo before comparison.")
    if not prof["has_rest"]:
        return dict(fetch_files(prof, target_dir, progress, timeout), source="files")
    try:
        result = dict(fetch_rest(prof, target_dir, progress), source="rest")
    except RuntimeError as error:
        result = dict(fetch_files(prof, target_dir, progress, timeout), source="files")
        result["skipped"] = ["live links: %s" % error] + result["skipped"]
        return result
    # Design freshness is mandatory: do not use live REST links with an older design file.
    extra = fetch_files(prof, target_dir, progress, timeout, skip=(SCAN,))
    result["files"].update(extra["files"])
    result["skipped"].extend(extra["skipped"])
    return result
