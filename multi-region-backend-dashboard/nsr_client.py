"""Read-only access to Grafana (Mimir / VictoriaMetrics / Loki) and PagerDuty.

Grafana queries go straight to Grafana's datasource proxy over HTTPS, inside this process,
with the same Teleport app certificate nsr uses (`tsh apps config observability-grafana`)
and kept-alive connections: no process per query. If that path fails for a reason other than
the query itself (no certificate, Teleport down), the query falls back to `nsr` below.

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

import http.client
import json
import shutil
import ssl
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path

GRAFANA_HOST = "observability-grafana.nscale.teleport.sh"
GRAFANA_APP = "observability-grafana"
TELEPORT_PROXY = "nscale.teleport.sh"
DEFAULT_PROM_UID = "infra-mimir"
DEFAULT_LOKI_UID = "infra-loki"

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


class GrafanaDirect:
    """Grafana datasource proxy over mTLS, one kept-alive HTTPS connection per worker thread."""

    def __init__(self, host: str = GRAFANA_HOST, app: str = GRAFANA_APP, proxy: str = TELEPORT_PROXY,
                 timeout: int = 40) -> None:
        self.host, self.app, self.proxy, self.timeout = host, app, proxy, timeout
        self.context: ssl.SSLContext | None = None
        self.lock = threading.Lock()
        self.local = threading.local()

    def _tsh(self, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
        tsh = shutil.which("tsh")
        if not tsh:
            raise NsrError("tsh is not installed", "brew install teleport")
        return subprocess.run([tsh, *args], capture_output=True, text=True, timeout=timeout, check=False)

    def _load_context(self, renew: bool = False) -> ssl.SSLContext:
        """The Teleport app certificate for Grafana; `tsh apps login` once if missing or rejected."""
        with self.lock:
            if self.context is not None and not renew:
                return self.context
            def paths():
                cert = self._tsh("apps", "config", "--format", "cert", self.app)
                key = self._tsh("apps", "config", "--format", "key", self.app)
                return (cert.stdout.strip(), key.stdout.strip()) if not (cert.returncode or key.returncode) else None
            found = None if renew else paths()
            if not found:
                login = self._tsh("apps", "login", "--proxy=" + self.proxy, self.app, timeout=120)
                if login.returncode:
                    raise NsrError("tsh apps login %s failed: %s" % (self.app, (login.stderr or login.stdout).strip()[:200]),
                                   "Teleport login expired: run `tsh login --proxy=nscale.teleport.sh`, then refresh.")
                found = paths()
                if not found:
                    raise NsrError("no Teleport certificate for %s" % self.app, "Run `tsh apps login %s`." % self.app)
            context = ssl.create_default_context()
            context.load_cert_chain(certfile=found[0], keyfile=found[1])
            self.context = context
            self.local = threading.local()  # drop connections made with the old certificate
            return context

    def _conn(self) -> http.client.HTTPSConnection:
        conn = getattr(self.local, "conn", None)
        if conn is None:
            conn = http.client.HTTPSConnection(self.host, 443, timeout=self.timeout, context=self._load_context())
            self.local.conn = conn
        return conn

    def get(self, path: str, params: dict) -> object:
        url = path + "?" + urllib.parse.urlencode(params, doseq=True)
        for attempt in range(3):
            conn = self._conn()
            try:
                conn.request("GET", url, headers={"Accept": "application/json", "Connection": "keep-alive"})
                resp = conn.getresponse()
                raw = resp.read().decode("utf-8", "replace")
            except ssl.SSLError as error:
                self.local.conn = None
                conn.close()
                if attempt == 0 and ("EXPIRED" in str(error).upper() or "CERTIFICATE" in str(error).upper()):
                    self._load_context(renew=True)  # Teleport app certs last about an hour
                    continue
                raise NsrError("TLS failure talking to Grafana: %s" % error, _hint("tls certificate")) from error
            except (OSError, http.client.HTTPException):
                self.local.conn = None  # stale kept-alive connection: reconnect once
                conn.close()
                if attempt < 2:
                    continue
                raise
            if resp.status in (401, 403) and attempt == 0:
                self._load_context(renew=True)
                self.local.conn = None
                continue
            if resp.status >= 400:
                raise QueryError("Grafana HTTP %d: %s" % (resp.status, raw[:300]), _hint("400 " + raw[:200]))
            try:
                return json.loads(raw or "null")
            except json.JSONDecodeError as error:
                raise NsrError("Grafana returned output that is not JSON") from error
        raise NsrError("Grafana did not answer")

    def ds(self, uid: str, path: str, params: dict) -> object:
        return self.get("/api/datasources/proxy/uid/%s%s" % (urllib.parse.quote(uid, safe=""), path), params)


class QueryError(NsrError):
    """Grafana answered and rejected the query itself: no point retrying it through nsr."""


class Nsr:
    def __init__(self, binary: str | None = None, max_parallel: int = 6, timeout: int = 40,
                 direct: bool = True) -> None:
        self.binary = binary or default_nsr()
        self.timeout = timeout
        self.slots = threading.BoundedSemaphore(max_parallel)
        self.calls = 0          # nsr processes started
        self.direct_calls = 0   # Grafana queries answered in-process
        self.lock = threading.Lock()
        self.grafana = GrafanaDirect(timeout=timeout) if direct else None
        self.direct_error = ""  # why the last direct query fell back to nsr, if it did

    def _direct(self, uid: str, path: str, params: dict) -> object | None:
        """The query in-process, or None to fall back to nsr (query errors are raised)."""
        if self.grafana is None:
            return None
        with self.slots:
            try:
                data = self.grafana.ds(uid, path, params)
            except QueryError:
                raise
            except (NsrError, OSError, http.client.HTTPException) as error:
                self.direct_error = str(error)[:300]
                return None
            with self.lock:
                self.direct_calls += 1
            self.direct_error = ""
            return data

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
        data = self._direct(datasource or DEFAULT_PROM_UID, "/api/v1/query", {"query": promql})
        if data is None:
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
        end = time.time()
        data = self._direct(datasource or DEFAULT_LOKI_UID, "/loki/api/v1/query_range", {
            "query": logql, "start": int((end - _seconds(since)) * 1e9), "end": int(end * 1e9),
            "limit": limit, "direction": "backward"})
        if data is None:
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


def _seconds(text: str) -> float:
    """'90s', '15m', '1h', '2d' -> seconds (the --since form nsr accepts)."""
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    text = str(text).strip()
    try:
        return float(text[:-1]) * units[text[-1]] if text and text[-1] in units else float(text)
    except ValueError:
        return 3600.0


class Cache:
    """Shared answers for every tab: stale-while-revalidate.

    A request always gets the stored answer at once. When it is older than `ttl`, one
    background refresh starts (never two for the same key), so N open tabs cost one query
    set per `ttl`, and nothing runs while no page is asking. Only the very first request for
    a key waits for the answer. `force` (the page's Refresh button) recomputes now, but at
    most once per `min_force` seconds per key.
    """

    def __init__(self, ttl: float, min_force: float = 30.0) -> None:
        self.ttl, self.min_force = ttl, min_force
        self.items: dict = {}
        self.running: set = set()
        self.lock = threading.Lock()
        self.key_locks: dict = {}

    def _compute(self, key, compute):
        try:
            value = compute()
            with self.lock:
                self.items[key] = (time.monotonic(), value)
            return value
        finally:
            with self.lock:
                self.running.discard(key)

    def get(self, key, compute, force: bool = False):
        with self.lock:
            hit = self.items.get(key)
            age = time.monotonic() - hit[0] if hit else None
            key_lock = self.key_locks.setdefault(key, threading.Lock())
        if hit and force and age >= self.min_force:
            with key_lock:
                with self.lock:
                    self.running.add(key)
                return self._compute(key, compute)
        if hit:
            if age >= self.ttl:
                with self.lock:
                    start = key not in self.running
                    self.running.add(key)
                if start:
                    def refresh():
                        with key_lock:
                            try:
                                self._compute(key, compute)
                            except Exception:  # noqa: BLE001 - keep serving the last answer
                                pass
                    threading.Thread(target=refresh, daemon=True).start()
            return hit[1]
        with key_lock:  # first request: wait once; concurrent first requests share the result
            with self.lock:
                hit = self.items.get(key)
                if hit:
                    return hit[1]
                self.running.add(key)
            return self._compute(key, compute)
