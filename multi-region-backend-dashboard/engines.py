"""Site engines behind the single dashboard service.

ICE2 and LON14 already have a full NetBox + switches + UFM engine
(`<site>-netbox-dashboard/app/netbox_live_sync.py`). The single service starts each one
as a child process on an internal loopback port and serves it under /<site>/, so there is
one URL, one port and one start command for every region, while each engine keeps its
own topology, cabling check, Sync fabric, Refresh NetBox and inspector unchanged.

Proxying keeps the engines' own guards working: the Host and Origin headers are rewritten
to the child's address, and the page gets a small fetch() shim so its absolute /api/...
calls go to /<site>/api/....
"""

from __future__ import annotations

import http.client
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "host", "origin", "referer", "accept-encoding", "content-length"}


class Engine:
    def __init__(self, site: str, spec: dict, repo: Path, netbox_url: str, log_dir: Path) -> None:
        self.site, self.spec = site, spec
        self.dir = (repo / spec["dir"]).resolve()
        self.port = int(spec["port"])
        self.netbox_url = netbox_url
        self.log = log_dir / ("%s-engine.log" % site)
        self.proc: subprocess.Popen | None = None
        self.extra: list[subprocess.Popen] = []
        self.error = ""
        self.lock = threading.Lock()

    def profile(self) -> str:
        return os.path.expanduser(os.path.expandvars(self.spec.get("profile", "~/.config/idc-automation/device-access.ini")))

    def command(self, port: int, args: list[str]) -> list[str]:
        return [sys.executable, "app/netbox_live_sync.py", "--port", str(port), "--netbox-url", self.netbox_url,
                "--device-profile", self.profile(), *args]

    def healthy(self, port: int | None = None) -> bool:
        """Is an engine answering HTTP on that port? Its /api/health may say 500 when NetBox is
        unreachable; the engine is still serving, so any HTTP answer counts."""
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/api/health" % (port or self.port), timeout=3) as response:
                return response.status < 600
        except urllib.error.HTTPError:
            return True
        except Exception:  # noqa: BLE001
            return False

    def start(self) -> None:
        with self.lock:
            if self.healthy():
                return  # already serving (started by an earlier run of this service)
            if not (self.dir / "app" / "netbox_live_sync.py").is_file():
                self.error = "%s not found" % (self.dir / "app" / "netbox_live_sync.py")
                return
            self.log.parent.mkdir(exist_ok=True)
            out = self.log.open("ab")
            self.proc = subprocess.Popen(self.command(self.port, self.spec.get("args", [])), cwd=self.dir,
                                         stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
            for extra in self.spec.get("extra", []):  # e.g. LON14 SYS2 on its usual port
                if not self.healthy(int(extra["port"])):
                    self.extra.append(subprocess.Popen(self.command(int(extra["port"]), extra.get("args", [])),
                                                       cwd=self.dir, stdout=out, stderr=subprocess.STDOUT,
                                                       stdin=subprocess.DEVNULL))
            for _ in range(40):
                if self.healthy():
                    self.error = ""
                    return
                if self.proc.poll() is not None:
                    break
                time.sleep(0.5)
            self.error = "did not start; see %s" % self.log

    def status(self) -> dict:
        running = self.healthy()
        return {"site": self.site, "configured": True, "running": running, "url": "/%s/" % self.site,
                "port": self.port, "dir": str(self.dir), "error": "" if running else self.error, "log": str(self.log)}

    def stop(self) -> None:
        for proc in [self.proc, *self.extra]:
            if proc and proc.poll() is None:
                proc.terminate()


SHIM = ('<script>(function(){var B="/%s";var F=window.fetch;window.fetch=function(u,o){'
        'if(typeof u==="string"&&u.charAt(0)==="/"&&u.charAt(1)!=="/"&&u.indexOf(B+"/")!==0){u=B+u}'
        'return F.call(this,u,o)};})();</script>')


def rewrite_html(html: str, site: str) -> str:
    """Make an engine page work under /<site>/: prefix root-relative links and fetches."""
    html = re.sub(r'(href|src|action)="/(?!/)', r'\1="/%s/' % site, html)
    shim = SHIM % site
    return html.replace("<head>", "<head>" + shim, 1) if "<head>" in html else shim + html


def forward(engine: Engine, handler, method: str, rest: str) -> None:
    """Proxy one request to the engine and write the answer to `handler`."""
    length = int(handler.headers.get("Content-Length") or 0)
    body = handler.rfile.read(length) if length else None
    headers = {k: v for k, v in handler.headers.items() if k.lower() not in HOP}
    headers["Host"] = "127.0.0.1:%d" % engine.port
    if handler.headers.get("Origin"):
        headers["Origin"] = "http://127.0.0.1:%d" % engine.port
    if body is not None:
        headers["Content-Length"] = str(len(body))
    conn = http.client.HTTPConnection("127.0.0.1", engine.port, timeout=600)
    try:
        conn.request(method, rest, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
    except OSError as error:
        handler.respond(502, {"error": "The %s engine is not answering (%s)." % (engine.site.upper(), error),
                              "hint": "It is starting, or failed: see %s" % engine.log})
        return
    finally:
        conn.close()
    ctype = resp.getheader("Content-Type") or ""
    if "text/html" in ctype:
        data = rewrite_html(data.decode("utf-8", "replace"), engine.site).encode("utf-8")
    handler.send_response(resp.status)
    for key, value in resp.getheaders():
        low = key.lower()
        if low in HOP or low == "content-length":
            continue
        if low == "location" and value.startswith("/"):
            value = "/%s%s" % (engine.site, value)
        handler.send_header(key, value)
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    if method != "HEAD":
        handler.wfile.write(data)
