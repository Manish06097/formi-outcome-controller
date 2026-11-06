"""A workflow that appears mid-run must not crash-loop the controller (AUDIT-1 finding 13).

The real controller drives a real simulator through a small HTTP proxy that behaves like a platform
where someone adds a workflow while the run is going: from some point on, `GET /workflows` also lists
a new id. Everything else passes through untouched. The controller reads the policy from that listing
every step, so before the fix the step after the appearance raised `KeyError` in the catalog, and every
restart hit it again until the run died unfinished.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from controller.simclient import SimClient
from tests.e2e.platform.helpers import (SHORT, SIM_PORT, create_database, drop_database,
                                        latest_schema, query, run_controller)
from tests.support.sim_process import SimProcess

pytestmark = [pytest.mark.e2e, pytest.mark.pg]

NEW_ID = "wf_added_midrun"
APPEARS_AFTER = 3
"""`POST /sim/advance` calls before the new id shows up: well inside the run."""


class AddWorkflowProxy:
    """Forwards everything to the simulator; adds one workflow to `GET /workflows` later on."""

    def __init__(self, target: str):
        self.target = target
        self.advances = 0
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _forward(self, method: str) -> None:
                if method == "POST" and self.path.split("?")[0] == "/sim/advance":
                    proxy.advances += 1
                n = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(n) if n else None
                req = urllib.request.Request(proxy.target + self.path, data=body, method=method)
                for k, v in self.headers.items():
                    if k.lower() not in ("host", "content-length", "connection"):
                        req.add_header(k, v)
                try:
                    with urllib.request.urlopen(req, timeout=600) as r:
                        status, data, ctype = r.status, r.read(), r.headers.get("content-type")
                except urllib.error.HTTPError as e:
                    status, data, ctype = e.code, e.read(), e.headers.get("content-type")
                if method == "GET" and self.path.split("?")[0] == "/workflows":
                    data = proxy.with_new_workflow(data)
                self.send_response(status)
                self.send_header("content-type", ctype or "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                self._forward("GET")

            def do_POST(self) -> None:
                self._forward("POST")

            def do_PUT(self) -> None:
                self._forward("PUT")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def with_new_workflow(self, data: bytes) -> bytes:
        if self.advances <= APPEARS_AFTER:
            return data
        listed = json.loads(data)
        return json.dumps(listed + [{**listed[0], "workflow_id": NEW_ID}]).encode()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def test_a_workflow_listed_mid_run_is_reported_and_left_alone(pg_dsn, tmp_path):
    dsn = create_database(pg_dsn)
    sim = SimProcess("scenarios/public_01_dev.yaml", tmp_path, port=SIM_PORT, overrides=SHORT,
                     ready_timeout_s=300).start()
    proxy = AddWorkflowProxy(sim.url)
    try:
        run = run_controller(proxy.url, dsn, tmp_path / "controller.log",
                             {"CTL_LEARNER": "probe"}, max_restarts=3)
        assert proxy.advances > APPEARS_AFTER, "the new workflow never appeared in the run"
        assert run.exit_codes == [0], run.log[-3000:]
        assert SimClient(sim.url).state().finished
        schema = latest_schema(dsn)
        alerts = query(dsn, schema, """SELECT code, dedupe_key FROM alert
                                       WHERE code = 'UNKNOWN_WORKFLOW'""")
        assert alerts == [("UNKNOWN_WORKFLOW", NEW_ID)]
        rows = query(dsn, schema, """SELECT count(*) FROM audit_log
                                     WHERE action = 'workflow_ignored'""")
        assert rows[0][0] == 1
        known = {r[0] for r in query(dsn, schema, "SELECT workflow_id FROM workflow")}
        assert NEW_ID not in known
    finally:
        proxy.close()
        sim.stop()
        drop_database(pg_dsn, dsn)
