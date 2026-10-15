"""One-click UFM access from the region pages: a CLI session in iTerm, or the web UI in the browser.

    GET  /api/ufm/<site>                      the site's UFMs and which kinds of access work
    POST /api/ufm/<site>/<cli|web>/<ufm-host> (or /<engine-site>/api/ufm/<cli|web>/<host>)

UFM names and IPs come from nscaleproxy's host map (netops-misc/nscaleproxy), the same map
the browser uses, plus the UFM appliances Teleport lists as nodes (`tsh ls`).

CLI, first route that applies:
  * the UFM is a Teleport node (OSL1 ufm0/ufm1, STA1 ufm2): `tsh ssh [--login <jump_user>] <node>`,
    jump_user from [ufm.<site>];
  * its IP is in the profile's UFM SSH settings ([ufm.<site>], else [ufm] for ICE2: ufm_hosts,
    ufm_user, and op_ref = op://vault/item/password, else keychain_service + keychain_account):
    the switch login script through the site's jump host ([<site>] jump_host / jump_user), the
    password read from 1Password (`op read`, so a rotated password is picked up on the next
    click) or Keychain.
Web: nscaleproxy (SOCKS through jmp0 + PAC on 127.0.0.1:8080). If its PAC is not answering, it is
started in an iTerm tab first; then https://<ufm-host>/ opens in the default browser, which must
use the PAC (as it already does once nscaleproxy has been set up). The UFM web password is typed
by you (or your password manager) on the UFM login page; it never passes through this service.
"""

from __future__ import annotations

import configparser
import ipaddress
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import device_connect
from device_connect import ConnectError

PAC_URL = "http://127.0.0.1:8080/proxy.pac"
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,99}$")
TOOL_PYTHON = Path.home() / ".local/share/uv/tools/nscaleproxy/bin/python"
_cache: dict = {}
_lock = threading.Lock()


def cached(key: str, ttl: float, compute):
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    value = compute()
    with _lock:
        _cache[key] = (time.time(), value)
    return value


def map_file() -> Path | None:
    """nscaleproxy's packaged host map, wherever uv installed it (editable or not)."""
    override = os.environ.get("NSCALEPROXY_MAP_FILE")
    if override:
        return Path(override)
    if TOOL_PYTHON.exists():
        done = subprocess.run([str(TOOL_PYTHON), "-c", "from nscaleproxy import cli; print(cli.DEFAULT_MAP_FILE)"],
                              capture_output=True, text=True, timeout=20)
        if done.returncode == 0 and done.stdout.strip():
            return Path(done.stdout.strip())
    return None


def host_map() -> dict:
    """UFM hostname -> IP (web UI targets only: no -bmc consoles, no raw-IP aliases)."""
    def load():
        path = map_file()
        if not path or not path.is_file():
            return {}
        hosts = json.loads(path.read_text(encoding="utf-8")).get("hosts") or {}
        return {name: ip for name, ip in hosts.items()
                if isinstance(ip, str) and "ufm" in name and not name.endswith("-bmc") and NAME.match(name)
                and not re.match(r"^\d+\.\d+\.\d+\.\d+$", name)}
    return cached("map", 300, load)


def teleport_nodes() -> set:
    def load():
        if not shutil.which("tsh"):
            return set()
        done = subprocess.run(["tsh", "ls", "--format=names"], capture_output=True, text=True, timeout=60)
        return {line.strip() for line in done.stdout.splitlines() if "ufm" in line}
    return cached("nodes", 300, load)


def belongs(name: str, site: str) -> bool:
    return name.startswith(site + "-") or ("-%s-" % site) in name


def profile_for(app, site: str) -> Path:
    region = app.by_site[site]
    profile = (region.get("engine") or {}).get("profile") or app.fabrics.profile or "~/.config/idc-automation/device-access.ini"
    return Path(os.path.expanduser(str(profile)))


def read_profile(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path, encoding="utf-8")
    return parser


def ssh_settings(parser: configparser.ConfigParser, site: str, ip: str) -> dict | None:
    """The UFM SSH login whose ufm_hosts include `ip`, with the site's jump host, or None."""
    for section in ("ufm." + site, "ufm"):
        if not parser.has_section(section):
            continue
        sec = dict(parser.items(section))
        hosts = (sec.get("ufm_hosts") or "").split()
        if sec.get("op_ref"):
            secret = device_connect.op_secret(sec["op_ref"], Path(section))
        elif sec.get("keychain_service") and sec.get("keychain_account"):
            secret = ["keychain", sec["keychain_service"], sec["keychain_account"]]
        else:
            secret = None
        if ip in hosts and sec.get("ufm_user") and secret:
            site_sec = dict(parser.items(site)) if parser.has_section(site) else {}
            jump_host = sec.get("jump_host") or site_sec.get("jump_host")
            jump_user = sec.get("jump_user") or site_sec.get("jump_user") or site_sec.get("ssh_user")
            if jump_host and jump_user:
                return {"secret": secret,
                        "ssh_user": sec["ufm_user"], "jump_host": jump_host, "jump_user": jump_user}
    return None


def teleport_login(parser: configparser.ConfigParser, site: str) -> str | None:
    section = "ufm." + site
    return (parser.get(section, "jump_user", fallback="") or "").strip() or None


def ufms(app, site: str) -> list[dict]:
    if site not in app.by_site:
        raise ConnectError("Unknown region.", 404)
    names = {n: ip for n, ip in host_map().items() if belongs(n, site)}
    nodes = {n for n in teleport_nodes() if belongs(n, site)}
    parser = read_profile(profile_for(app, site))
    out = []
    for name in sorted(set(names) | nodes):
        ip = names.get(name)
        cli = "teleport" if name in nodes else "ssh" if ip and ssh_settings(parser, site, ip) else None
        out.append({"name": name, "ip": ip, "cli": cli, "web": bool(ip)})
    return out


def find(app, site: str, host: str) -> dict:
    if not NAME.match(host):
        raise ConnectError("Not a UFM name.", 400)
    for ufm in ufms(app, site):
        if ufm["name"] == host:
            return ufm
    raise ConnectError("%s is not a known UFM for this region (nscaleproxy host map or Teleport nodes)." % host, 404)


def open_cli(app, site: str, host: str) -> dict:
    ufm = find(app, site, host)
    device_connect.require_teleport()
    parser = read_profile(profile_for(app, site))
    if ufm["cli"] == "teleport":
        login = teleport_login(parser, site)
        device_connect.launch(["tsh", "ssh", "-t", *(["--login", login] if login else []), host], host)
        return {"ok": True, "opened": "cli", "host": host, "via": "Teleport node"}
    cfg = ssh_settings(parser, site, ufm["ip"] or "")
    if not cfg:
        raise ConnectError("No CLI route for %s: it is not a Teleport node and %s has no UFM SSH login "
                           "(ufm_hosts, ufm_user, keychain_service) for %s." % (host, profile_for(app, site), ufm["ip"]), 409)
    ipaddress.ip_address(ufm["ip"])  # from the host map; refuse anything that is not an address
    if cfg["secret"][0] == "op" and not shutil.which("op"):
        raise ConnectError("1Password CLI (op) is not installed: brew install 1password-cli", 409)
    device_connect.launch([str(device_connect.SCRIPT), *cfg["secret"], cfg["jump_user"], cfg["jump_host"],
                           cfg["ssh_user"], ufm["ip"], host], host)
    return {"ok": True, "opened": "cli", "host": host, "via": cfg["jump_host"],
            "password": "1Password" if cfg["secret"][0] == "op" else "Keychain"}


def pac() -> str | None:
    try:
        with urllib.request.urlopen(PAC_URL, timeout=2) as response:
            return response.read().decode("utf-8", "replace")
    except OSError:
        return None


def open_web(app, site: str, host: str) -> dict:
    ufm = find(app, site, host)
    if not ufm["web"]:
        raise ConnectError("%s is not in nscaleproxy's host map, so the browser cannot reach its web UI." % host, 409)
    text, started = pac(), False
    if text is None:
        if not shutil.which("nscaleproxy"):
            raise ConnectError("nscaleproxy is not installed: uv tool install from netops-misc/nscaleproxy.", 409)
        device_connect.require_teleport()
        device_connect.launch(["nscaleproxy"], "nscaleproxy")
        started = True
        deadline = time.time() + 45
        while text is None and time.time() < deadline:
            time.sleep(1)
            text = pac()
        if text is None:
            raise ConnectError("nscaleproxy did not start: see its iTerm tab (it may be asking for your Teleport username).", 504)
    if host not in text:
        raise ConnectError("The running nscaleproxy does not route %s; restart it (uv tool upgrade nscaleproxy)." % host, 409)
    subprocess.run(["open", "https://%s/" % host], check=False, timeout=20)
    return {"ok": True, "opened": "web", "host": host, "via": "nscaleproxy", "started_proxy": started}
