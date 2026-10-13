"""Read-only access to Grafana (Mimir / VictoriaMetrics / Loki) and PagerDuty through `nsr`.

`nsr` (nscaledev/netops-misc, subdirectory nsr) owns authentication: Teleport mTLS for
Grafana and its own Keychain entries for PagerDuty. This module never sees a token. It
only runs a fixed allow-list of read verbs and parses their `--json` output:

    nsr --json -q grafana query <promql> --datasource <uid>
    nsr --json -q grafana loki  <logql>  --since 1h --limit N [--datasource <uid>]
    nsr --json -q pd incidents --status triggered --status acknowledged [--service ID]

nsr is used for metrics, logs and incidents only. Topology comes from NetBox and UFM
directly (netbox_source.py, ufm_direct.py), as in the ICE2 / LON14 dashboards.

Every call has a timeout, and at most `max_parallel` nsr processes run at once so a page
load never fans out into dozens of simultaneous Grafana queries.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

# The only nsr verbs this dashboard may run. Anything else is a programming error.
ALLOWED = {("grafana", "query"), ("grafana", "loki"), ("pd", "incidents")}


class NsrError(RuntimeError):
    """An nsr call failed; `hint` says what the user can do about it."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


def default_nsr() -> str:
    """nsr on PATH, else uv's per-user tool directory (where `uv tool install` puts it)."""
    for candidate in (shutil.which("nsr"), str(Path.home() / ".local/bin/nsr")):
        if candidate and Path(candidate).is_file():
            return candidate
    return "nsr"


def _hint(stderr: str) -> str:
    text = stderr.lower()
    if "tsh" in text and ("login" in text or "expired" in text or "not logged" in text):
        return "Teleport login expired: run `tsh login --proxy=nscale.teleport.sh`, then refresh."
    if "certificate" in text or "tls" in text:
        return "Teleport certificate problem: run `tsh login`, then `nsr doctor`."
    if "pagerduty" in text and ("token" in text or "setup" in text or "401" in text):
        return "PagerDuty token missing or rejected: run `nsr setup pd` once."
    if "400" in text or "parse error" in text:
        return "Grafana rejected the query (label or metric differs at this site)."
    return "Run `nsr doctor` to see what is unreachable."


class Nsr:
    def __init__(self, binary: str | None = None, max_parallel: int = 6, timeout: int = 40) -> None:
        self.binary = binary or default_nsr()
        self.timeout = timeout
        self.slots = threading.BoundedSemaphore(max_parallel)
        self.calls = 0
        self.lock = threading.Lock()

    # -- low level --------------------------------------------------------
    def run(self, verb: tuple[str, str], *args: str, timeout: int | None = None) -> object:
        if verb not in ALLOWED:
            raise ValueError("nsr verb %r is not allowed (read-only allow-list)" % (verb,))
        argv = [self.binary, "--json", "-q", *verb, *args]
        with self.slots:
            with self.lock:
                self.calls += 1
            try:
                done = subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      stdin=subprocess.DEVNULL, timeout=timeout or self.timeout, check=False)
            except FileNotFoundError as error:
                raise NsrError("nsr is not installed or not on PATH (%s)" % self.binary,
                               'Install it: uv tool install "git+ssh://git@github.com/nscaledev/'
                               'netops-misc.git#subdirectory=nsr", or pass --nsr /path/to/nsr') from error
            except subprocess.TimeoutExpired as error:
                raise NsrError("nsr timed out after %ss" % (timeout or self.timeout),
                               "Grafana is slow or unreachable; try again or run `nsr doctor`.") from error
        if done.returncode:
            lines = [line for line in done.stderr.strip().splitlines() if line.strip()]
            message = lines[-1] if lines else "nsr exited with status %d" % done.returncode
            raise NsrError(message[:400], _hint(done.stderr))
        try:
            return json.loads(done.stdout or "null")
        except json.JSONDecodeError as error:
            raise NsrError("nsr returned output that is not JSON", "Upgrade nsr: `uv tool upgrade nsr`.") from error

    # -- typed helpers ----------------------------------------------------
    def query(self, promql: str, datasource: str | None = None) -> list[dict]:
        """Instant PromQL query; returns the Prometheus `result` list (vector rows)."""
        args = [promql] + (["--datasource", datasource] if datasource else [])
        data = self.run(("grafana", "query"), *args)
        if not isinstance(data, dict):
            raise NsrError("unexpected Grafana response")
        if data.get("status") not in (None, "success"):
            raise NsrError(str(data.get("error") or data.get("status")), _hint(str(data.get("error"))))
        body = data.get("data") or {}
        result = body.get("result") or []
        if body.get("resultType") == "scalar":
            return [{"metric": {}, "value": result}]
        return result if isinstance(result, list) else []

    def loki(self, logql: str, since: str = "1h", limit: int = 50, datasource: str | None = None) -> list[dict]:
        """Log lines, newest first: [{"ts": epoch_s, "labels": {...}, "line": str}]."""
        args = [logql, "--since", since, "--limit", str(limit)]
        if datasource:
            args += ["--datasource", datasource]
        data = self.run(("grafana", "loki"), *args)
        streams = ((data or {}).get("data") or {}).get("result") or []
        lines = []
        for stream in streams:
            labels = stream.get("stream") or stream.get("metric") or {}
            for ts, line in stream.get("values") or []:
                try:
                    when = int(ts) / 1e9
                except (TypeError, ValueError):
                    when = 0.0
                lines.append({"ts": when, "labels": labels, "line": " ".join(str(line).split())[:600]})
        lines.sort(key=lambda row: row["ts"], reverse=True)
        return lines[:limit]

    def pd_incidents(self, service: str | None = None, limit: int = 50) -> list[dict]:
        args = ["--status", "triggered", "--status", "acknowledged", "--limit", str(limit)]
        if service:
            args += ["--service", service]
        data = self.run(("pd", "incidents"), *args)
        return data if isinstance(data, list) else []


class Cache:
    """Tiny TTL cache so a page refresh never re-runs every query."""

    def __init__(self, ttl: float) -> None:
        self.ttl, self.items, self.lock = ttl, {}, threading.Lock()
        self.key_locks: dict = {}

    def get(self, key, compute, force: bool = False):
        with self.lock:
            key_lock = self.key_locks.setdefault(key, threading.Lock())
        with key_lock:  # one computation per key at a time; others wait and reuse it
            with self.lock:
                hit = self.items.get(key)
            if hit and not force and time.monotonic() - hit[0] < self.ttl:
                return hit[1]
            value = compute()
            with self.lock:
                self.items[key] = (time.monotonic(), value)
            return value
