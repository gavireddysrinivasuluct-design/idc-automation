#!/usr/bin/env python3
"""Collect a complete candidate known_hosts file through the approved jump host.

The collected keys are not a substitute for independent fingerprint approval.
"""
from __future__ import annotations

import argparse
import csv
import getpass
import ipaddress
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_NETBOX_URL = "http://127.0.0.1:8444"


def token() -> str:
    result = subprocess.run(
        ["security", "find-generic-password", "-s", "netbox-mcp-token", "-a", getpass.getuser(), "-w"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode or not result.stdout.strip():
        raise RuntimeError("NetBox token is missing. Run scripts/configure_netbox_token.sh first.")
    return result.stdout.strip()


def device_names(path: Path) -> list[str]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        names = [(row.get("hostname") or "").strip() for row in csv.DictReader(handle)]
    names = [name for name in names if name]
    if not names or len(names) != len(set(names)):
        raise RuntimeError("Device inventory must contain unique non-empty hostname values.")
    return names


def resolve_on_jump_host(names: list[str], jump_host: str, jump_user: str) -> dict[str, str]:
    """Names NetBox has no IP for (LON14 sys2): resolve them with the jump host's DNS."""
    import shlex
    script = ("import json,socket,sys\nout={}\nfor n in json.load(sys.stdin):\n"
              " try: out[n]=socket.gethostbyname(n)\n except OSError: pass\nprint(json.dumps(out))")
    done = subprocess.run(["tsh", "ssh", "--login", jump_user, jump_host, "python3 -c %s" % shlex.quote(script)],
                          input=json.dumps(names), text=True, capture_output=True, check=False, timeout=120)
    if done.returncode:
        raise RuntimeError("DNS lookup on %s failed: %s" % (jump_host, done.stderr.strip()[-200:]))
    answer = json.loads(done.stdout.strip().splitlines()[-1])
    return {n: str(ipaddress.ip_address(a)) for n, a in answer.items() if n in names}


def management_ips(names: list[str], netbox_url: str, jump: tuple[str, str] | None = None) -> list[str]:
    headers = {"Authorization": "Token " + token(), "Accept": "application/json"}
    ips: list[str] = []
    no_ip: list[str] = []
    for name in names:
        request = Request(netbox_url.rstrip("/") + "/api/dcim/devices/?limit=2&name=" + quote(name, safe=""), headers=headers)
        with urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        matches = payload.get("results") or []
        if len(matches) != 1:
            raise RuntimeError("NetBox returned %d devices for %s." % (len(matches), name))
        primary = matches[0].get("primary_ip4") or matches[0].get("primary_ip") or {}
        address = (primary.get("address") or "").split("/", 1)[0]
        if not address and jump:
            no_ip.append(name)
            continue
        try:
            ips.append(str(ipaddress.ip_address(address)))
        except ValueError as error:
            raise RuntimeError("NetBox has no valid primary IP for %s." % name) from error
    if no_ip:
        resolved = resolve_on_jump_host(no_ip, *jump)
        unresolved = [n for n in no_ip if n not in resolved]
        if unresolved:
            raise RuntimeError("No NetBox IP and no DNS answer on %s for %d device(s), including: %s" % (jump[0], len(unresolved), ", ".join(unresolved[:5])))
        print("Resolved %d device(s) without a NetBox IP through DNS on %s." % (len(resolved), jump[0]))
        ips.extend(resolved[n] for n in no_ip)
    if len(ips) != len(set(ips)):
        raise RuntimeError("NetBox returned duplicate management IPs; resolve this before collecting host keys.")
    return ips


def collect(ips: list[str], jump_host: str, jump_user: str) -> list[str]:
    if not shutil.which("tsh"):
        raise RuntimeError("Teleport CLI (tsh) is required.")
    remote = "for address in %s; do ssh-keyscan -T 5 -t rsa \"$address\" 2>/dev/null; done" % " ".join(ips)
    result = subprocess.run(
        ["tsh", "ssh", "--login", jump_user, jump_host, "--", remote],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode:
        raise RuntimeError("Host-key collection through %s failed: %s" % (jump_host, result.stderr.strip()))
    expected = set(ips)
    entries = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[0] in expected and fields[1] == "ssh-rsa":
            entries.append(line)
    found = {line.split()[0] for line in entries}
    missing = expected - found
    if missing:
        raise RuntimeError("No RSA host key returned for %d device(s), including: %s" % (len(missing), ", ".join(sorted(missing)[:5])))
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect candidate device SSH host keys through an approved Teleport jump host.")
    parser.add_argument("--jump-host", required=True)
    parser.add_argument("--jump-user", required=True)
    parser.add_argument("--netbox-url", default=DEFAULT_NETBOX_URL)
    parser.add_argument("--fabric", choices=["sys1", "sys2"], default="sys1", help="sys2: the Ethernet backend's 328 switches (assets/sys2/devices.csv -> local-inputs/sys2/known_hosts)")
    parser.add_argument("--devices", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--accept-live-keys", action="store_true", help="Required acknowledgement that live keys need independent approval.")
    args = parser.parse_args()
    if not args.accept_live_keys:
        raise RuntimeError("Refusing to install live-collected keys without --accept-live-keys.")
    sys2 = args.fabric == "sys2"
    args.devices = args.devices or PROJECT_ROOT / "assets" / ("sys2/devices.csv" if sys2 else "devices.csv")
    args.output = args.output or PROJECT_ROOT / "local-inputs" / ("sys2/known_hosts" if sys2 else "known_hosts")
    ips = management_ips(device_names(args.devices), args.netbox_url, (args.jump_host, args.jump_user))
    entries = collect(ips, args.jump_host, args.jump_user)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    candidate = args.output.with_name(args.output.name + ".candidate")
    candidate.write_text("# Live-collected keys: verify fingerprints independently before use.\n" + "\n".join(entries) + "\n", encoding="utf-8")
    os.chmod(candidate, 0o600)
    if args.output.exists():
        backup = args.output.with_name(args.output.name + ".previous")
        shutil.copy2(args.output, backup)
        os.chmod(backup, 0o600)
    os.replace(candidate, args.output)
    print("Installed %d live-collected host keys at %s" % (len(entries), args.output))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError) as error:
        print("Error: %s" % error, file=sys.stderr)
        raise SystemExit(2)
