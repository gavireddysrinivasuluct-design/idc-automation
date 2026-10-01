#!/usr/bin/env python3
"""Read-only ICE2 NTP collector with one local password prompt.

The password stays only in process memory. It is sent to the SSH password
prompt through a local pseudo-terminal and is never logged, saved, or put in
an environment variable.
"""
from __future__ import annotations

import argparse
import configparser
import csv
import getpass
import ipaddress
import os
import pty
import select
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

DEFAULT_NTP_COMMANDS = [
    "nv show system ntp --output json",
    "nv show system ntp server detail --output json",
]


@dataclass(frozen=True)
class Device:
    hostname: str
    address: str


@dataclass
class Result:
    device: Device
    code: int
    stdout: str
    stderr: str


def rows(path: Path, *columns: str) -> List[Dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = set(columns) - set(reader.fieldnames or [])
        if missing:
            raise ValueError("%s missing columns: %s" % (path, ", ".join(sorted(missing))))
        return [{key: (value or "").strip() for key, value in row.items()} for row in reader]


def load_commands(path: Path) -> List[str]:
    if not path.is_file():
        raise ValueError("Command file does not exist: %s" % path)
    commands = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if not commands:
        raise ValueError("Command file contains no commands: %s" % path)
    return commands


def build_remote_command(commands: List[str], ntp_report: bool) -> str:
    if ntp_report:
        return """printf '__NTP_STATUS_JSON__\\n'
%s
printf '\\n__NTP_SERVERS_JSON__\\n'
%s""" % (commands[0], commands[1])
    chunks: List[str] = []
    for number, command in enumerate(commands, start=1):
        chunks.extend([
            "printf '__ICE2_COMMAND_%03d_START__\\n'" % number,
            command,
            "printf '\\n__ICE2_COMMAND_%03d_END__\\n'" % number,
        ])
    return "\n".join(chunks)


def load_profile(path: Path) -> Dict[str, str]:
    parser = configparser.ConfigParser(interpolation=None)
    if not parser.read(path, encoding="utf-8"):
        raise ValueError("ICE2 profile does not exist: %s" % path)
    if not parser.has_section("ice2"):
        raise ValueError("ICE2 profile must contain an [ice2] section: %s" % path)
    profile = {key: value.strip() for key, value in parser.items("ice2") if value.strip()}
    required = {"ssh_user", "jump_host", "jump_user", "keychain_service", "keychain_account"}
    missing = required - set(profile)
    if missing:
        raise ValueError("ICE2 profile is missing: %s" % ", ".join(sorted(missing)))
    return profile


def keychain_password(profile: Dict[str, str]) -> str:
    if not shutil.which("security"):
        raise RuntimeError("macOS Keychain command (security) is required for --profile")
    completed = subprocess.run(
        ["security", "find-generic-password", "-s", profile["keychain_service"], "-a", profile["keychain_account"], "-w"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode:
        raise RuntimeError("Unable to read the ICE2 switch password from macOS Keychain. Run configure_ice2_profile.sh again.")
    password = completed.stdout.rstrip("\n")
    if not password:
        raise RuntimeError("The ICE2 switch password stored in macOS Keychain is empty.")
    return password


def resolve_management_ips(names: List[str]) -> Dict[str, str]:
    """Resolve switch names on the jumpbox; failures remain explicit."""
    resolved: Dict[str, str] = {}
    for name in names:
        try:
            values = socket.getaddrinfo(name, None, family=socket.AF_INET, type=socket.SOCK_STREAM)
            addresses = [entry[4][0] for entry in values]
            if addresses:
                resolved[name] = addresses[0]
        except socket.gaierror:
            continue
    return resolved


def load_devices(device_file: Path, address_file: Path, allow_missing: bool, resolve_missing: bool) -> List[Device]:
    names = [row["hostname"] for row in rows(device_file, "hostname") if row["hostname"]]
    if len(names) != len(set(names)):
        raise ValueError("devices.csv contains duplicate hostnames")
    mapping: Dict[str, str] = {}
    for row in rows(address_file, "hostname", "management_address"):
        name, value = row["hostname"], row["management_address"]
        if not name or not value:
            continue
        if name in mapping:
            raise ValueError("management_addresses.csv duplicates %s" % name)
        address = value.split("/", 1)[0]
        try:
            ipaddress.ip_address(address)
        except ValueError:
            raise ValueError("Invalid management IP for %s: %s" % (name, value))
        mapping[name] = address
    duplicate_ips = sorted({ip for ip in mapping.values() if list(mapping.values()).count(ip) > 1})
    if duplicate_ips:
        raise ValueError("Duplicate management IPs: %s" % ", ".join(duplicate_ips))
    missing = [name for name in names if name not in mapping]
    if missing and resolve_missing:
        resolved = resolve_management_ips(missing)
        mapping.update(resolved)
        if resolved:
            print("Resolved %d missing management IP(s) from jumpbox DNS." % len(resolved), file=sys.stderr)
        missing = [name for name in names if name not in mapping]
    if missing and not allow_missing:
        preview = ", ".join(missing[:10]) + (" …" if len(missing) > 10 else "")
        raise ValueError("Missing management IPs for %d of %d devices: %s" % (len(missing), len(names), preview))
    if missing:
        print("Warning: skipping %d device(s) without management IPs." % len(missing), file=sys.stderr)
    return [Device(name, mapping[name]) for name in names if name in mapping]


def ssh_command(device: Device, user: str, known_hosts: Optional[str], jump_host: Optional[str], jump_user: Optional[str], remote_command: str) -> List[str]:
    command = ["ssh", "-o", "ConnectTimeout=15", "-o", "StrictHostKeyChecking=yes", "-o", "NumberOfPasswordPrompts=1"]
    if known_hosts:
        command.extend(["-o", "UserKnownHostsFile=%s" % known_hosts])
    command.extend(["%s@%s" % (user, device.address), remote_command])
    if not jump_host:
        return command
    # Run the switch SSH command from the approved Teleport jump host. The
    # target address is validated before this point and shell-quoted here so
    # that the remote command cannot interpret it as shell syntax.
    remote_command = " ".join(shlex.quote(part) for part in command)
    # The inner SSH password prompt requires a TTY across the Teleport hop.
    return ["tsh", "ssh", "--tty", "--login", jump_user or user, jump_host, "--", remote_command]


def stage_known_hosts(local_file: Path, jump_host: str, jump_user: str) -> str:
    """Copy approved keys to a private temporary file on the jump host."""
    created = subprocess.run(
        ["tsh", "ssh", "--login", jump_user, jump_host, "--", "umask 077; mktemp /tmp/ice2-known-hosts.XXXXXX"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    remote_file = created.stdout.strip()
    if created.returncode or not remote_file.startswith("/tmp/ice2-known-hosts."):
        raise RuntimeError("Unable to create temporary approved host-key file on %s: %s" % (jump_host, created.stderr.strip()))
    copied = subprocess.run(
        ["tsh", "scp", str(local_file), "%s@%s:%s" % (jump_user, jump_host, remote_file)],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if copied.returncode:
        subprocess.run(["tsh", "ssh", "--login", jump_user, jump_host, "--", "rm -f %s" % shlex.quote(remote_file)], check=False)
        raise RuntimeError("Unable to copy approved host keys to %s: %s" % (jump_host, copied.stderr.strip()))
    return remote_file


def remove_staged_known_hosts(remote_file: Optional[str], jump_host: Optional[str], jump_user: Optional[str]) -> None:
    if remote_file and jump_host and jump_user:
        subprocess.run(
            ["tsh", "ssh", "--login", jump_user, jump_host, "--", "rm -f %s" % shlex.quote(remote_file)],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )


def collect_one(device: Device, user: str, password: str, known_hosts: Optional[str], jump_host: Optional[str], jump_user: Optional[str], remote_command: str) -> Result:
    """Run SSH under a PTY and answer its one password prompt from memory."""
    master_fd: Optional[int] = None
    slave_fd: Optional[int] = None
    try:
        command = ssh_command(device, user, known_hosts, jump_host, jump_user, remote_command)
        master_fd, slave_fd = pty.openpty()
        child = subprocess.Popen(command, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd, close_fds=True)
        os.close(slave_fd)
        slave_fd = None
        output_parts: List[str] = []
        password_sent = False
        deadline = time.monotonic() + 60
        while True:
            if time.monotonic() >= deadline and child.poll() is None:
                child.terminate()
                child.wait(timeout=5)
                return Result(device, 124, "", "Timed out before SSH completed")
            ready, _, _ = select.select([master_fd], [], [], 0.5)
            if ready:
                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    data = b""
                if data:
                    output_parts.append(data.decode("utf-8", errors="replace"))
                    if not password_sent and "password:" in "".join(output_parts).lower():
                        os.write(master_fd, password.encode("utf-8") + b"\n")
                        password_sent = True
            if child.poll() is not None:
                # A closed PTY reports EIO on macOS; the loop has already
                # captured all command output available before process exit.
                break
        output = "".join(output_parts)
        code = child.returncode or 0
        if code == 0:
            return Result(device, code, output, "")
        return Result(device, code, "", output or "SSH exited without output")
    except Exception as error:
        return Result(device, 1, "", str(error))
    finally:
        if master_fd is not None:
            os.close(master_fd)
        if slave_fd is not None:
            os.close(slave_fd)


def save(output_dir: Path, result: Result) -> None:
    (output_dir / "raw" / (result.device.hostname + ".txt")).write_text(result.stdout, encoding="utf-8")
    (output_dir / "errors" / (result.device.hostname + ".txt")).write_text(result.stderr, encoding="utf-8")


def build_xlsx(script_dir: Path, output_dir: Path, args: argparse.Namespace) -> Optional[Path]:
    node = shutil.which("node")
    builder = script_dir / "build_ntp_report.mjs"
    if not node or not builder.exists():
        reason = "Node.js is unavailable" if not node else "build_ntp_report.mjs is unavailable"
        (output_dir / "xlsx_build_error.txt").write_text(reason + "\n", encoding="utf-8")
        return None
    localstorage_file = Path(tempfile.gettempdir()) / "ice2-artifact-localstorage"
    completed = subprocess.run(
        [node, "--localstorage-file=%s" % localstorage_file, str(builder), str(args.devices), str(output_dir), str(args.expected_ntp), args.drift_threshold],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if completed.returncode:
        (output_dir / "xlsx_build_error.txt").write_text(completed.stderr or completed.stdout, encoding="utf-8")
        return None
    report = output_dir / "ICE2_NTP_Audit.xlsx"
    if report.is_file():
        return report
    (output_dir / "xlsx_build_error.txt").write_text(
        "The report builder completed without creating ICE2_NTP_Audit.xlsx.\n", encoding="utf-8"
    )
    return None


def build_command_xlsx(script_dir: Path, output_dir: Path, args: argparse.Namespace) -> Optional[Path]:
    node = shutil.which("node")
    builder = script_dir / "build_command_report.mjs"
    if not node or not builder.exists():
        reason = "Node.js is unavailable" if not node else "build_command_report.mjs is unavailable"
        (output_dir / "xlsx_build_error.txt").write_text(reason + "\n", encoding="utf-8")
        return None
    localstorage_file = Path(tempfile.gettempdir()) / "ice2-artifact-localstorage"
    completed = subprocess.run(
        [node, "--localstorage-file=%s" % localstorage_file, str(builder), str(args.devices), str(output_dir)],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if completed.returncode:
        (output_dir / "xlsx_build_error.txt").write_text(completed.stderr or completed.stdout, encoding="utf-8")
        return None
    report = output_dir / "ICE2_Command_Evidence.xlsx"
    return report if report.is_file() else None


def main() -> int:
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Read-only, password-once ICE2 NTP audit.")
    parser.add_argument("--devices", type=Path, default=root / "devices.csv")
    parser.add_argument("--addresses", type=Path, default=root / "management_addresses.csv")
    parser.add_argument("--expected-ntp", type=Path, default=root / "expected_ntp_servers.csv")
    parser.add_argument("--commands-file", type=Path, default=root / "commands" / "ntp_commands.txt", help="One shell command per line. Blank lines and # comments are ignored.")
    parser.add_argument("--report-format", choices=["ntp", "command", "none"], default="ntp", help="Use ntp only with default NTP commands; command builds a generic evidence workbook; none saves raw evidence only.")
    parser.add_argument("--profile", type=Path, help="Private ICE2 profile created by configure_ice2_profile.sh.")
    parser.add_argument("--ssh-user", help="Switch SSH username.")
    parser.add_argument("--jump-host", help="Approved Teleport jump host used to reach switch management addresses.")
    parser.add_argument("--jump-user", help="Teleport login for --jump-host; defaults to --ssh-user.")
    parser.add_argument("--known-hosts", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--parallel", type=int, default=10)
    parser.add_argument("--allow-missing", action="store_true", help="Use only for an intentional limited test.")
    parser.add_argument("--no-resolve-missing", action="store_true", help="Use only when DNS must not be used as an IP fallback.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--drift-threshold", default="0.5")
    args = parser.parse_args()
    commands = load_commands(args.commands_file)
    ntp_commands = commands == DEFAULT_NTP_COMMANDS
    if args.report_format == "ntp" and not ntp_commands:
        raise ValueError("--report-format ntp requires the default commands/ntp_commands.txt commands. Use --report-format none for a custom command file.")
    remote_command = build_remote_command(commands, args.report_format == "ntp")
    profile = load_profile(args.profile) if args.profile else None
    args.ssh_user = args.ssh_user or (profile or {}).get("ssh_user")
    args.jump_host = args.jump_host or (profile or {}).get("jump_host")
    args.jump_user = args.jump_user or (profile or {}).get("jump_user")
    if not args.ssh_user:
        raise ValueError("Provide --ssh-user or a profile created by scripts/configure_device_access.sh.")
    if args.jump_host and not args.known_hosts:
        args.known_hosts = root / "ice2_known_hosts"
    if not 1 <= args.parallel <= 25:
        raise ValueError("--parallel must be between 1 and 25")
    if args.known_hosts and not args.known_hosts.is_file():
        raise ValueError("Approved known-hosts file does not exist: %s" % args.known_hosts)
    if args.jump_host and not shutil.which("tsh"):
        raise RuntimeError("Teleport CLI (tsh) is required with --jump-host")
    devices = load_devices(args.devices, args.addresses, args.allow_missing, not args.no_resolve_missing)
    if not devices:
        raise ValueError("No usable device/IP mappings")
    if args.dry_run:
        print("Validated %d device/IP mappings." % len(devices))
        return 0
    output_dir = args.output_dir or root / "output" / ("ice2-ntp-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    (output_dir / "raw").mkdir(parents=True, exist_ok=False)
    (output_dir / "errors").mkdir(exist_ok=True)
    password = keychain_password(profile) if profile else getpass.getpass("Switch SSH password (not saved): ")
    if not password:
        raise ValueError("Password was not entered")
    remote_known_hosts = stage_known_hosts(args.known_hosts, args.jump_host, args.jump_user) if args.jump_host else str(args.known_hosts) if args.known_hosts else None
    results: List[Result] = []
    try:
        print("Collecting NTP evidence from %d devices (parallel=%d)." % (len(devices), args.parallel))
        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
            futures = [pool.submit(collect_one, device, args.ssh_user, password, remote_known_hosts, args.jump_host, args.jump_user, remote_command) for device in devices]
            for future in as_completed(futures):
                result = future.result()
                save(output_dir, result)
                results.append(result)
                print("%s: %s" % (result.device.hostname, "ok" if result.code == 0 else "failed"))
    finally:
        remove_staged_known_hosts(remote_known_hosts if args.jump_host else None, args.jump_host, args.jump_user)
    with (output_dir / "collection_summary.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["hostname", "management_address", "collection_status", "exit_code", "raw_evidence", "error_evidence"])
        for result in sorted(results, key=lambda value: value.device.hostname):
            writer.writerow([result.device.hostname, result.device.address, "Collected" if result.code == 0 else "Failed", result.code, "raw/%s.txt" % result.device.hostname, "errors/%s.txt" % result.device.hostname])
    report = build_xlsx(root, output_dir, args) if args.report_format == "ntp" else build_command_xlsx(root, output_dir, args) if args.report_format == "command" else None
    failed = sum(result.code != 0 for result in results)
    print("Saved evidence to %s. Success=%d Failed=%d" % (output_dir, len(results) - failed, failed))
    if report:
        print("Excel report created: %s" % report)
    elif args.report_format != "none":
        print("Excel report was not created. See %s" % (output_dir / "xlsx_build_error.txt"), file=sys.stderr)
    else:
        print("Raw command evidence saved; no NTP workbook was requested.")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as error:
        print("Error: %s" % error, file=sys.stderr)
        raise SystemExit(2)
