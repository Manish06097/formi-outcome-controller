"""What the controller does when the world is not the one it was written against, end to end:
a policy that changes between restarts (AUDIT-1 finding 12) and a directive naming a tenant no
workflow has (finding 7). A real simulator over HTTP throughout.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import httpx
import pytest
import yaml

from controller.simclient import SimClient
from tests.e2e.platform.helpers import REPO, SHORT, query, registry, run_controller

pytestmark = [pytest.mark.e2e, pytest.mark.pg]


class PolicyProxy:
    """Forwards everything to the simulator, except that it can rewrite one governance field in the
    workflow documents: the API-visible change a restart could see (DESIGN.md 9.7)."""

    def __init__(self, target: str):
        self.target = target
        self.rewrite: dict[str, object] = {}
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:
                pass

            def _forward(self) -> None:
                n = int(self.headers.get("content-length") or 0)
                r = httpx.request(self.command, proxy.target + self.path,
                                  content=self.rfile.read(n) if n else None,
                                  headers={"content-type": "application/json"}, timeout=300)
                body = r.content
                if proxy.rewrite and self.path.startswith("/workflows") and r.status_code == 200 \
                        and "/config" not in self.path:
                    body = json.dumps(proxy._rewritten(r.json())).encode()
                self.send_response(r.status_code)
                self.send_header("content-type", r.headers.get("content-type", "application/json"))
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_PUT = _forward

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _rewritten(self, doc: object) -> object:
        if isinstance(doc, list):
            return [self._rewritten(d) for d in doc]
        if isinstance(doc, dict):
            if "workflows" in doc:
                return {**doc, "workflows": self._rewritten(doc["workflows"])}
            if "governance" in doc:
                return {**doc, "governance": {**doc["governance"], **self.rewrite}}
        return doc

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def proxy_of(sim_factory) -> Iterator:
    made: list[PolicyProxy] = []

    def make(target: str) -> PolicyProxy:
        made.append(PolicyProxy(target))
        return made[-1]

    yield make
    for p in made:
        p.close()


def test_a_policy_change_between_restarts_resumes_the_same_run(sim_factory, proxy_of, db_dsn,
                                                               tmp_path):
    """Before the fix the fingerprint included the governance block: this restart computed a new one,
    abandoned nothing but started over, so pending approvals and learned state were lost."""
    sim = sim_factory(overrides=SHORT)
    proxy = proxy_of(sim.url)
    log = tmp_path / "controller.log"
    first = run_controller(proxy.url, db_dsn, log, {"CTL_STOP_AFTER_STEPS": "5"})
    assert first.exit_codes == [1], first.log[-2000:]
    ((schema, fp, status, _),) = registry(db_dsn)
    assert status == "active"

    wf = SimClient(proxy.url).workflows()[0]
    new_ttl = int(wf.raw["governance"]["ttl_hours"]) + 1
    proxy.rewrite = {"ttl_hours": new_ttl}
    assert SimClient(proxy.url).workflows()[0].raw["governance"]["ttl_hours"] == new_ttl

    second = run_controller(proxy.url, db_dsn, log)
    assert second.exit_codes == [0], second.log[-2000:]
    rows = registry(db_dsn)
    assert [r[0] for r in rows] == [schema], "the same run schema resumes"
    assert rows[0][1] == fp and rows[0][2] == "done"
    assert f"run {schema} resumed" in second.log
    assert query(db_dsn, schema, "SELECT count(*) FROM alert WHERE code = 'NEW_WORLD'") == [(0,)]


def _renamed_tenant(tmp_path: Path) -> Path:
    """public_01 with one tenant renamed, the way a hidden scenario could."""
    text = (REPO / "scenarios" / "public_01_dev.yaml").read_text()
    assert "kestrel_finance" in text
    out = tmp_path / "renamed.yaml"
    out.write_text(text.replace("kestrel_finance", "hidden_lender"))
    return out


def test_a_directive_matching_no_workflow_is_paged_and_audited(sim_factory, db_dsn, tmp_path):
    sim = sim_factory(scenario=str(_renamed_tenant(tmp_path)), overrides=SHORT)
    log = tmp_path / "controller.log"
    first = run_controller(sim.url, db_dsn, log, {"CTL_STOP_AFTER_STEPS": "1"})
    assert first.exit_codes == [1], first.log[-2000:]
    second = run_controller(sim.url, db_dsn, log, {"CTL_STOP_AFTER_STEPS": "1"})
    assert second.exit_codes == [1]
    schema = registry(db_dsn)[0][0]

    audit = query(db_dsn, schema, """SELECT subject_id, summary FROM audit_log
                                     WHERE action = 'directive_unmatched'""")
    assert len(audit) == 1, "one audit row per rule, per run, however many restarts"
    assert audit[0][0] == "kestrel_finance/use_case~cancel"
    assert "memo 04" in audit[0][1] and "hidden_lender" in audit[0][1]
    alerts = query(db_dsn, schema, """SELECT severity, dedupe_key, runbook FROM alert
                                      WHERE code = 'DIRECTIVE_UNMATCHED' AND closed_wall IS NULL""")
    assert alerts == [("page", "kestrel_finance/use_case~cancel",
                       "docs/RUNBOOK.md#DIRECTIVE_UNMATCHED")]


def test_the_public_scenarios_match_every_directive(sim_factory, db_dsn, tmp_path):
    sim = sim_factory(overrides=SHORT)
    first = run_controller(sim.url, db_dsn, tmp_path / "c.log", {"CTL_STOP_AFTER_STEPS": "1"})
    assert first.exit_codes == [1], first.log[-2000:]
    schema = registry(db_dsn)[0][0]
    assert query(db_dsn, schema, "SELECT count(*) FROM alert WHERE code = 'DIRECTIVE_UNMATCHED'") == [
        (0,)]
    assert yaml.safe_load((REPO / "controller" / "directives.yaml").read_text())["version"] == 1
