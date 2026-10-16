"""A tiny stand-in for the NetBox REST API (GET only), serving the invented OSL1-like
fabric from fake_nsr.py, for tests and the offline demo:  python3 tests/fake_netbox.py 18444"""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fake_nsr  # noqa: E402

DEVICES = {}
for role in ("be-spine-switch", "be-leaf-switch"):
    for d in fake_nsr.osl1_devices(role):
        DEVICES[d["name"]] = {**d, "id": len(DEVICES) + 1, "role": {"slug": role}}
BY_ID = {d["id"]: d for d in DEVICES.values()}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, body, code=200):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if not (self.headers.get("Authorization") or "").startswith("Token "):
            return self.send({"detail": "no token"}, 403)
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if url.path == "/api/dcim/devices/":
            rows = [d for d in DEVICES.values() if q.get("site") == ["osl1"] and d["role"]["slug"] in q.get("role", [])]
            return self.send({"count": len(rows), "next": None, "results": rows})
        if url.path == "/api/dcim/interfaces/":
            ids = {int(i) for i in q.get("device_id", [])}
            names = {BY_ID[i]["name"] for i in ids if i in BY_ID}
            rows = []
            for c in fake_nsr.osl1_cables():
                if c["device"] in names:
                    rows.append({"id": c["cable"], "name": c["interface"], "device": {"name": c["device"]},
                                 "cable": {"id": c["cable"]}, "connected_endpoints_reachable": True,
                                 "connected_endpoints": [{"name": c["remote_interface"], "device": {"name": c["remote_device"]}}]})
            return self.send({"count": len(rows), "next": None, "results": rows})
        if url.path == "/api/dcim/cables/":  # one leaf cable is still planned
            ids = {int(i) for i in q.get("device_id", [])}
            names = {BY_ID[i]["name"] for i in ids if i in BY_ID}
            first = next((c for c in fake_nsr.osl1_cables() if c["device"] in names), None)
            rows = [{"id": first["cable"], "status": {"value": "planned", "label": "Planned"}}] if first and q.get("status__n") == ["connected"] else []
            return self.send({"count": len(rows), "next": None, "results": rows})
        return self.send({"detail": "not found"}, 404)


def serve(port: int = 0) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    server = serve(int(sys.argv[1]) if len(sys.argv) > 1 else 18444)
    print("fake NetBox on http://127.0.0.1:%d" % server.server_address[1])
    server.serve_forever()
