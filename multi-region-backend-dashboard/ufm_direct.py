"""UFM, read directly: the actual links of a NetBox-first region (OSL1, STA1, ...).

The same read-only path the ICE2 / LON14 dashboards use for "Fetch from UFM": one
`tsh ssh` session, which asks the UFM REST API for GET /ufmRest/resources/links. The
session goes either to a jump host that reaches the UFM address, or, where the UFM
appliance itself is a Teleport node (STA1: `tsh ssh sta1-p-phy-ufm2`), straight to the
UFM node, which asks its own REST API on 127.0.0.1 (then the VIP, if that node is standby). The UFM web password comes from macOS Keychain and is
written only to the worker's stdin. UFM's self-signed certificate is pinned on first use
(cache/<site>-ufm-pins.json).

Profile (the same private ini as the other dashboards, mode 600), one section per site:

    [ufm.osl1]
    jump_host = <teleport node that reaches the UFM VIP>      (or leave empty and set ufm_node)
    ufm_node = <the UFM appliance as a Teleport node>         (direct: tsh ssh <ufm_node>)
    jump_user = <your teleport login>                         (optional: tsh default login)
    hosts = 10.50.40.9                                        (direct default: 127.0.0.1 + VIP)
    rest_user = <read-only UFM web user>
    rest_keychain_service = idc-automation-osl1-ufm-rest
    rest_keychain_account = <same as rest_user>

scripts/configure_site_ufm.sh <site> writes it and stores the password.
"""

from __future__ import annotations

import base64
import configparser
import getpass
import gzip
import json
import re
import shlex
import subprocess
import threading
import time
from pathlib import Path

LINKS_PATH = "/ufmRest/resources/links"

WORKER = r"""
import base64, gzip, hashlib, http.client, json, ssl, sys, time
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
    except Exception as error:
        emit({"event": "failed", "host": host, "detail": "%s: %s" % (type(error).__name__, error)}); continue
    pin = cfg["pins"].get(host)
    if pin and pin != fp:
        emit({"event": "failed", "host": host, "detail": "TLS certificate changed; verify with the UFM owner, then remove the pin"}); conn.close(); continue
    try:
        conn.request("GET", cfg["path"], headers={"Authorization": auth, "Accept": "application/json"})
        resp = conn.getresponse(); body = resp.read()
    except Exception as error:
        emit({"event": "failed", "host": host, "detail": "%s: %s" % (type(error).__name__, error)}); continue
    if resp.status != 200:
        emit({"event": "failed", "host": host, "detail": "HTTP %s%s" % (resp.status, " (wrong UFM web user or password)" if resp.status in (401, 403) else "")}); continue
    try:
        data = json.loads(body)
    except ValueError:
        emit({"event": "failed", "host": host, "detail": "not JSON (standby UFM?)"}); continue
    if not isinstance(data, list) or not data:
        emit({"event": "failed", "host": host, "detail": "no links (standby UFM?)"}); continue
    emit({"event": "links", "host": host, "fingerprint": fp, "count": len(data), "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
          "data": base64.b64encode(gzip.compress(body)).decode("ascii")})
    break
emit({"event": "done"})
"""


class UfmError(RuntimeError):
    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


def read_profile(path: Path, site: str) -> dict:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path, encoding="utf-8")
    section = "ufm." + site
    if not parser.has_section(section):
        raise UfmError("Direct UFM access for %s is not set up" % site.upper(),
                       "Run scripts/configure_site_ufm.sh %s once (jump host, UFM address, read-only web user)." % site)
    cfg = dict(parser.items(section))
    # Where the tsh session lands: a jump host, or the UFM appliance itself (direct).
    cfg["target"] = (cfg.get("jump_host") or "").strip() or (cfg.get("ufm_node") or "").strip()
    cfg["direct"] = not (cfg.get("jump_host") or "").strip()
    missing = [k for k in ("hosts", "rest_user", "rest_keychain_service") if not cfg.get(k)]
    if not cfg["target"]:
        missing.insert(0, "jump_host or ufm_node")
    if missing:
        raise UfmError("[%s] in %s is missing %s" % (section, path, ", ".join(missing)),
                       "Run scripts/configure_site_ufm.sh %s again." % site)
    cfg["hosts"] = cfg["hosts"].split()
    cfg.setdefault("rest_keychain_account", cfg["rest_user"])
    return cfg


def tsh_command(cfg: dict) -> list[str]:
    """`tsh ssh [--login user] <jump host | UFM node> <python worker>`. UFM appliances may only
    have the platform Python, so fall back to it when python3 is not on PATH."""
    script = base64.b64encode(WORKER.encode()).decode()
    boot = "import base64,sys;exec(base64.b64decode(sys.argv[1]))"
    remote = ('PY=$(command -v python3 || command -v /usr/libexec/platform-python || echo python3); '
              '"$PY" -u -c %s %s' % (shlex.quote(boot), script))
    login = ["--login", cfg["jump_user"]] if cfg.get("jump_user") else []
    return ["tsh", "ssh", *login, cfg["target"], remote]


def keychain(service: str, account: str) -> str:
    out = subprocess.run(["security", "find-generic-password", "-s", service, "-a", account, "-w"],
                         capture_output=True, text=True, timeout=10)
    if out.returncode or not out.stdout.strip():
        raise UfmError("UFM password not in Keychain (%s)" % service, "Run scripts/configure_site_ufm.sh again.")
    return out.stdout.rstrip("\n")


def fetch_links(profile: Path, site: str, cache_dir: Path, progress=lambda text: None, timeout: int = 150) -> dict:
    cfg = read_profile(profile, site)
    pins_file = cache_dir / ("%s-ufm-pins.json" % site)
    pins = json.loads(pins_file.read_text()) if pins_file.is_file() else {}
    payload = {"user": cfg["rest_user"], "password": keychain(cfg["rest_keychain_service"], cfg["rest_keychain_account"]),
               "hosts": cfg["hosts"], "path": LINKS_PATH, "pins": pins, "timeout": timeout - 30}
    cmd = tsh_command(cfg)
    progress("UFM: Teleport session to %s%s" % (cfg["target"], " (the UFM node itself)" if cfg["direct"] else ""))
    child = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    timer = threading.Timer(timeout, child.kill)
    timer.daemon = True
    timer.start()
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
            if event.get("event") == "trying":
                progress("UFM: reading live links from %s via %s" % (event["host"], cfg["target"]))
        child.wait()
        stderr = child.stderr.read()
    finally:
        timer.cancel()
    got = next((e for e in events if e.get("event") == "links"), None)
    if not got:
        fails = ["%s: %s" % (e.get("host"), e.get("detail")) for e in events if e.get("event") == "failed"]
        raise UfmError("No UFM host answered: " + (" | ".join(fails) or stderr.strip()[-200:] or "worker failed"),
                       "Check `tsh login`, `tsh ssh %s` works, that it reaches %s on 443, and the UFM web password."
                       % (cfg["target"], " ".join(cfg["hosts"])))
    pins[got["host"]] = got["fingerprint"]
    cache_dir.mkdir(exist_ok=True)
    pins_file.write_text(json.dumps(pins, indent=1))
    raw = json.loads(gzip.decompress(base64.b64decode(got["data"])))
    (cache_dir / ("%s-ufm-links.json.gz" % site)).write_bytes(gzip.compress(json.dumps(raw).encode()))
    return {"host": got["host"], "at": got["at"], "collected_at": time.time(), "links": raw}


# -- turning UFM link records into (switch, port) -> far end -----------------------

def _pick(record: dict, *names):
    for name in names:
        if record.get(name) not in (None, ""):
            return record[name]
    return None


def _end(record: dict, side: str) -> tuple[str, str, int | None]:
    """(node name, port label, port number) of one end of a UFM link record."""
    desc = str(_pick(record, side + "_port_node_description", side + "_node_description", side + "_node_desc") or "")
    node, _, label = desc.rpartition(":") if ":" in desc else (desc, "", "")
    if " " in node and not label:          # host adapters: "gpu123 mlx5_0"
        node, label = node.split(" ", 1)
    label = label or str(_pick(record, side + "_port_dname", side + "_port_name") or "")
    port = _pick(record, side + "_port", side + "_port_number")
    try:
        number = int(port)
    except (TypeError, ValueError):
        m = re.search(r"(\d+)$", label)
        number = int(m.group(1)) if m else None
    return node.strip(), label.strip(), number


def links_by_switch_port(records: list, switch_names: set[str]) -> dict:
    """{"switch|port": {"peer": name, "peer_port": label}} for every UFM link touching a known switch."""
    shorts = {}
    for name in switch_names:
        shorts[name] = name
        shorts[name.lower()] = name
        m = re.search(r"(be[lsc]\d+|ifs\d+)$", name)
        if m:
            shorts.setdefault(m.group(1), name)
    out = {}
    for record in records or []:
        if not isinstance(record, dict):
            continue
        a, b = _end(record, "source"), _end(record, "destination")
        for near, far in ((a, b), (b, a)):
            name = shorts.get(near[0]) or shorts.get(near[0].lower())
            if name and near[2] is not None:
                out["%s|%d" % (name, near[2])] = {"peer": far[0], "peer_port": far[1] or (str(far[2]) if far[2] else "")}
    return out


def same_device(netbox_name: str | None, ufm_name: str | None) -> bool:
    """NetBox and UFM often spell a host differently (sys2-osl1-p-phy-gpu012 vs gpu012)."""
    if not netbox_name or not ufm_name:
        return False
    a, b = netbox_name.lower(), ufm_name.lower()
    if a == b or a.endswith("-" + b) or b.endswith("-" + a):
        return True
    tail = lambda s: re.split(r"[-_. ]", s)[-1]  # noqa: E731
    return tail(a) == tail(b) and re.search(r"\d", tail(a)) is not None
