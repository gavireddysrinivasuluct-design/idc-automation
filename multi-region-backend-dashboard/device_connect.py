"""One-click switch login: open Terminal on a switch's SSH prompt, password from 1Password or Keychain.

POST /api/connect/<site>/<hostname> (or /<engine-site>/api/connect/... from an engine page)
resolves the switch's management IP from this service's own data (never from the request),
then opens iTerm (Terminal if iTerm is not installed) running scripts/connect_switch.exp. The
first login opens a new iTerm window; later ones open as tabs in that window while it exists:

    tsh ssh -t --login <jump_user> <jump_host> ssh <ssh_user>@<ip>

The expect script reads the password itself (`op read <op_ref>`, or the Keychain entry the
live sync already uses) and types it at the switch's password prompt; the password never
reaches this server, the browser or a file.

Per-site settings come from the private device profile (the engine's profile for ICE2 and
LON14, --device-profile for the others). First match wins for each key:
    [connect.<site>]  ->  [<site>]  ->  [connect]
Keys: ssh_user, jump_host, jump_user, and the password source: op_ref (op://vault/item/field)
if set, else keychain_service + keychain_account (as written by configure_device_access.sh).
"""

from __future__ import annotations

import configparser
import ipaddress
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "scripts" / "connect_switch.exp"
HOSTNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
SAFE = re.compile(r"^[A-Za-z0-9._@-]{1,100}$")
ITERM = Path("/Applications/iTerm.app")
ONEPASSWORD_ACCOUNT = lambda: os.environ.get("OP_ACCOUNT") or "nscale.1password.com"  # noqa: E731
# One iTerm window for every login this service opens: new window first, then tabs in it.
ITERM_SCRIPT = """
on run argv
  set cmd to item 1 of argv
  set label to item 2 of argv
  set wid to (item 3 of argv) as integer
  set wasRunning to application "iTerm" is running
  tell application "iTerm"
    activate
    set target to missing value
    repeat with w in windows
      if id of w is wid then set target to w
    end repeat
    if target is missing value then
      if not wasRunning and (count of windows) > 0 then
        set target to current window
      else
        set target to (create window with default profile)
      end if
    else
      tell target to create tab with default profile
    end if
    tell current session of target
      set name to label
      write text cmd
    end tell
    select target
    return id of target
  end tell
end run
"""
_window = {"id": 0}
_window_lock = threading.Lock()

KEYS = ("op_ref", "keychain_service", "keychain_account", "ssh_user", "jump_host", "jump_user")


class ConnectError(Exception):
    """Something the user has to fix; `status` is the HTTP code to answer with."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def op_secret(ref: str, profile: Path) -> list[str]:
    """A 1Password secret reference, checked: the script runs `op read <ref>` at login time."""
    if not ref.startswith("op://") or any(c in ref for c in "\"'`$\\\n"):
        raise ConnectError("op_ref in %s must be a plain op://vault/item/field reference." % profile, 409)
    # The account is always named (like blech): without it, op in a fresh terminal can answer
    # "not currently signed in" even when the desktop app is unlocked.
    return ["op", ref, ONEPASSWORD_ACCOUNT()]


def settings(site: str, profile: Path) -> dict:
    parser = configparser.ConfigParser(interpolation=None)
    if not profile.is_file() or not parser.read(profile):
        raise ConnectError("No device profile at %s." % profile, 409)
    out = {}
    for key in KEYS:
        for section in ("connect." + site, site, "connect"):
            if parser.has_option(section, key) and parser.get(section, key).strip():
                out[key] = parser.get(section, key).strip()
                break
    if out.get("op_ref"):
        out["secret"] = op_secret(out["op_ref"], profile)
    elif out.get("keychain_service") and out.get("keychain_account"):
        out["secret"] = ["keychain", out["keychain_service"], out["keychain_account"]]
    else:
        raise ConnectError("No switch password source for %s: add op_ref = op://<vault>/<item>/password under "
                           "[connect.%s] in %s (or keychain_service and keychain_account)." % (site, site, profile), 409)
    for key in ("ssh_user", "jump_host", "jump_user") + (("keychain_service", "keychain_account") if out["secret"][0] == "keychain" else ()):
        if not out.get(key):
            raise ConnectError("Set %s under [connect.%s] in %s." % (key, site, profile), 409)
        if not SAFE.match(out[key]):
            raise ConnectError("%s in %s has characters that are not allowed." % (key, profile), 409)
    return out


def management_ip(value: str | None) -> str:
    try:
        return str(ipaddress.ip_interface((value or "").strip()).ip)
    except ValueError:
        raise ConnectError("NetBox has no usable management IP for this switch.", 404) from None


def ip_from_engine(port: int, hostname: str) -> str:
    """The engine's own device lookup (last sync, else NetBox): the same IP its inspector shows."""
    if "-swi-" not in hostname:
        raise ConnectError("Only switches can be opened from here.", 404)
    url = "http://127.0.0.1:%d/api/device/%s" % (port, urllib.request.quote(hostname, safe=""))
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            data = json.load(response)
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise ConnectError("Could not look up %s: %s" % (hostname, error), 502) from error
    if data.get("hostname") not in (None, hostname):
        raise ConnectError("The engine answered for a different device.", 502)
    return management_ip(data.get("primary_ip"))


def ip_from_fabric(view: dict, hostname: str) -> str:
    topo = view.get("topology") or {}
    for switch in (topo.get("spines") or []) + (topo.get("leaves") or []):
        if switch.get("name") == hostname:
            return management_ip(switch.get("ip"))
    raise ConnectError("%s is not a switch in this region's NetBox snapshot." % hostname, 404)


def require_teleport() -> None:
    if not shutil.which("tsh"):
        raise ConnectError("tsh is not installed: brew install teleport", 409)
    if subprocess.run(["tsh", "status"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20).returncode:
        raise ConnectError("Teleport login needed: run tsh login --proxy=nscale.teleport.sh, then click again.", 409)


def launch(args: list[str], label: str) -> None:
    """Run `args` in the dashboard's iTerm window (a new tab after the first), else in Terminal.

    For logins that read 1Password, iTerm needs macOS permission to reach 1Password's data:
    System Settings > Privacy & Security > Files & Folders > iTerm > "Data shared by
    1Password and affiliated apps". Without it `op` answers "not currently signed in"."""
    command = "clear; " + " ".join(shlex.quote(a) for a in args)
    if ITERM.is_dir():
        with _window_lock:  # two quick clicks must not both open a window
            done = subprocess.run(["osascript", "-e", ITERM_SCRIPT, command, label, str(_window["id"])],
                                  capture_output=True, text=True, timeout=30)
            if done.returncode:
                raise ConnectError("iTerm did not open: %s" % (done.stderr.strip() or "osascript failed"), 500)
            _window["id"] = int(done.stdout.strip() or 0)
        return
    apple = command.replace("\\", "\\\\").replace('"', '\\"')
    done = subprocess.run(["osascript", "-e", 'tell application "Terminal" to do script "%s"' % apple,
                           "-e", 'tell application "Terminal" to activate'],
                          capture_output=True, text=True, timeout=20)
    if done.returncode:
        raise ConnectError("Terminal did not open: %s" % (done.stderr.strip() or "osascript failed"), 500)


def open_terminal(site: str, hostname: str, ip: str, cfg: dict) -> None:
    if cfg["secret"][0] == "op" and not shutil.which("op"):
        raise ConnectError("1Password CLI (op) is not installed: brew install 1password-cli", 409)
    require_teleport()
    launch([str(SCRIPT), *cfg["secret"], cfg["jump_user"], cfg["jump_host"], cfg["ssh_user"], ip, hostname], hostname)


def connect(app, site: str, hostname: str) -> dict:
    """Resolve, check the profile, and open Terminal. Raises ConnectError for anything to fix."""
    if site not in app.by_site:
        raise ConnectError("Unknown region.", 404)
    if not HOSTNAME.match(hostname):
        raise ConnectError("Not a device name.", 400)
    region = app.by_site[site]
    engine = app.engines.get(site)
    default = Path(os.path.expanduser(str(app.fabrics.profile or "~/.config/idc-automation/device-access.ini")))
    if engine:
        profile = region["engine"].get("profile")
        cfg = settings(site, Path(os.path.expanduser(profile)) if profile else default)
        ip = ip_from_engine(engine.port, hostname)
    elif region.get("fabric_view"):
        cfg = settings(site, default)
        ip = ip_from_fabric(app.fabrics.view(site), hostname)
    else:
        raise ConnectError("This region has no device view.", 404)
    open_terminal(site, hostname, ip, cfg)
    return {"ok": True, "hostname": hostname, "ip": ip, "via": cfg["jump_host"],
            "password": "1Password" if cfg["secret"][0] == "op" else "Keychain"}
