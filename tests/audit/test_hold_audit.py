"""A supervisor hold changes decisions, so it is in the audit log (AUDIT-1 finding 15), written by a
trigger in the hold's own transaction. Through the real API as the least-privilege role `ctl_api`,
which cannot write `audit_log` itself.
"""
from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

from controller.api.app import create_app


@pytest.fixture()
def client(world):
    with TestClient(create_app(world["api_dsn"], world["schema"])) as c:
        yield c


def _rows(world, hold_id: int) -> list[tuple]:
    with psycopg.connect(world["dsn"], options=f"-c search_path={world['schema']}") as conn:
        return [tuple(r) for r in conn.execute(
            """SELECT action, actor, sim_minute, summary FROM audit_log
               WHERE subject_type = 'hold' AND subject_id = %s ORDER BY audit_id""",
            (str(hold_id),))]


def test_set_and_release_each_write_one_audit_row(client, world):
    r = client.post("/supervisor/holds", json={
        "tenant_id": "northwind_life", "workflow_id": "nw_renewal", "set_by": "Asha",
        "reason": "board review", "until": None})
    assert r.status_code == 201, r.text
    hold_id = r.json()["hold_id"]
    rows = _rows(world, hold_id)
    assert [(a, b) for a, b, _, _ in rows] == [("hold_set", "Asha")]
    assert "board review" in rows[0][3]

    r = client.post(f"/supervisor/holds/{hold_id}/release", json={"released_by": "Ravi"})
    assert r.status_code == 200, r.text
    assert [(a, b) for a, b, _, _ in _rows(world, hold_id)] == [("hold_set", "Asha"),
                                                                ("hold_released", "Ravi")]


def test_the_api_role_still_cannot_write_the_audit_log(world):
    with psycopg.connect(world["api_dsn"], options=f"-c search_path={world['schema']}") as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("""INSERT INTO audit_log (sim_minute, actor, action, subject_type, subject_id,
                                                   summary) VALUES (0, 'x', 'x', 'x', 'x', 'x')""")
