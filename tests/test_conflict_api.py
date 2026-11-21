"""End-to-end tests for the conflict-diagnosis API."""

import time

from app import database
from tests.test_api import ROSTER, TARGETS, _upload_roster, _upload_targets, _wait_finished


def _start_and_wait(client):
    client.post("/weighting/jobs")
    return _wait_finished(client)


def test_diagnose_unbalanced_job_returns_certificate(client):
    _upload_roster(client, ROSTER)
    impossible = [
        {"dimension": "district", "group_name": "D1", "target_count": 100.0},
        {"dimension": "district", "group_name": "D2", "target_count": 1.0},
        {"dimension": "size_band", "group_name": "S1", "target_count": 100.0},
        {"dimension": "size_band", "group_name": "S2", "target_count": 1.0},
    ]
    _upload_targets(client, impossible)
    final = _start_and_wait(client)
    assert final["status"] == "unbalanced"
    job_id = final["job_id"]

    resp = client.get(f"/weighting/jobs/{job_id}/conflict-diagnosis")
    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == job_id
    assert body["job_status"] == "unbalanced"
    assert body["respondent_version"] == 1
    assert body["target_version"] == 1
    assert body["conflict_found"] is True

    groups = body["selected_groups"]
    assert groups["districts"] or groups["size_bands"]
    # All required figures are present and internally consistent.
    assert abs(
        body["target_difference"]
        - (body["target_sum_districts"] - body["target_sum_size_bands"])
    ) < 1e-12
    breakdown = body["upper_bound_breakdown"]
    ub = (
        breakdown["district_only_factor"]
        * breakdown["district_only_init_weight"]
        + breakdown["band_only_factor"] * breakdown["band_only_init_weight"]
    )
    assert abs(ub - body["reachable_upper_bound"]) < 1e-12
    assert abs(
        body["target_difference"] - body["reachable_upper_bound"]
        - body["excess"]
    ) < 1e-12
    assert body["excess"] > 0.0
    assert body["combinations_scanned"] >= 1


def test_balanced_job_reports_no_conflict_evidence(client):
    _upload_roster(client, ROSTER)
    _upload_targets(client, TARGETS)
    final = _start_and_wait(client)
    assert final["status"] == "balanced"

    resp = client.get(f"/weighting/jobs/{final['job_id']}/conflict-diagnosis")
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "job_status_conflict"
    assert body["details"]["actual_status"] == "balanced"
    assert body["details"]["required_status"] == "unbalanced"


def test_running_job_not_diagnosable(client, monkeypatch):
    from app import service as service_mod

    _upload_roster(client, ROSTER)
    _upload_targets(client, TARGETS)
    original = service_mod.weighting.solve

    def slow_solve(households, target_rows):
        time.sleep(1.0)
        return original(households, target_rows)

    monkeypatch.setattr(service_mod.weighting, "solve", slow_solve)
    start = client.post("/weighting/jobs")
    job_id = start.json()["job_id"]

    resp = client.get(f"/weighting/jobs/{job_id}/conflict-diagnosis")
    assert resp.status_code == 409
    assert resp.json()["error"] == "job_status_conflict"
    assert resp.json()["details"]["actual_status"] == "running"
    _wait_finished(client)


def test_failed_job_not_diagnosable(client, monkeypatch):
    from app import service as service_mod

    _upload_roster(client, ROSTER)
    _upload_targets(client, TARGETS)

    def failing_solve(households, target_rows):
        from app.weighting import SolveResult

        return SolveResult(
            weights=[], balanced=False, gap=float("inf"),
            group_diffs=[], failure_reason="solver boom",
        )

    monkeypatch.setattr(service_mod.weighting, "solve", failing_solve)
    final = _start_and_wait(client)
    assert final["status"] == "failed"

    resp = client.get(f"/weighting/jobs/{final['job_id']}/conflict-diagnosis")
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "job_status_conflict"
    assert body["details"]["actual_status"] == "failed"


def test_unknown_job_id_returns_404(client):
    resp = client.get("/weighting/jobs/99999/conflict-diagnosis")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"] == "job_not_found"
    assert body["details"]["job_id"] == 99999


def test_non_numeric_job_id_returns_400(client):
    resp = client.get("/weighting/jobs/abc/conflict-diagnosis")
    assert resp.status_code == 400


def test_historical_job_uses_its_own_snapshots_after_replacement(client):
    # v1 pair -> unbalanced job.
    _upload_roster(client, ROSTER)
    impossible = [
        {"dimension": "district", "group_name": "D1", "target_count": 100.0},
        {"dimension": "district", "group_name": "D2", "target_count": 1.0},
        {"dimension": "size_band", "group_name": "S1", "target_count": 100.0},
        {"dimension": "size_band", "group_name": "S2", "target_count": 1.0},
    ]
    _upload_targets(client, impossible)
    old_job = _start_and_wait(client)
    assert old_job["status"] == "unbalanced"
    old_job_id = old_job["job_id"]

    # Replace BOTH tables (a handover); current pair becomes v2/v2.
    new_roster = [
        {"household_id": "N1", "district": "D1", "size_band": "S1",
         "init_weight": 2.0},
        {"household_id": "N2", "district": "D2", "size_band": "S2",
         "init_weight": 2.0},
    ]
    r1 = _upload_roster(client, new_roster)
    r2 = _upload_targets(
        client,
        [
            {"dimension": "district", "group_name": "D1", "target_count": 2.0},
            {"dimension": "district", "group_name": "D2", "target_count": 2.0},
            {"dimension": "size_band", "group_name": "S1", "target_count": 2.0},
            {"dimension": "size_band", "group_name": "S2", "target_count": 2.0},
        ],
    )
    assert r1.json()["version"] == 2
    assert r2.json()["version"] == 2

    # The historical unbalanced job is still diagnosable against v1/v1.
    resp = client.get(f"/weighting/jobs/{old_job_id}/conflict-diagnosis")
    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == old_job_id
    assert body["respondent_version"] == 1
    assert body["target_version"] == 1
    assert body["conflict_found"] is True

    # The current pair computes and balances independently on v2/v2.
    current = _start_and_wait(client)
    assert current["status"] == "balanced"
    assert current["respondent_version"] == 2
    assert current["target_version"] == 2
    # The balanced current job cannot be diagnosed.
    resp2 = client.get(
        f"/weighting/jobs/{current['job_id']}/conflict-diagnosis"
    )
    assert resp2.status_code == 409


def test_missing_snapshot_reports_snapshot_missing(client, service):
    _upload_roster(client, ROSTER)
    impossible = [
        {"dimension": "district", "group_name": "D1", "target_count": 100.0},
        {"dimension": "district", "group_name": "D2", "target_count": 1.0},
        {"dimension": "size_band", "group_name": "S1", "target_count": 100.0},
        {"dimension": "size_band", "group_name": "S2", "target_count": 1.0},
    ]
    _upload_targets(client, impossible)
    job = _start_and_wait(client)
    assert job["status"] == "unbalanced"
    job_id, r_ver, t_ver = job["job_id"], 1, 1

    # Simulate loss of the original target snapshot (retention/audit failure).
    with service._lock, database.db_session(service._db_path) as conn:
        conn.execute("DELETE FROM target_records WHERE version = ?", (t_ver,))
        conn.execute("DELETE FROM target_versions WHERE version = ?", (t_ver,))
        conn.commit()

    resp = client.get(f"/weighting/jobs/{job_id}/conflict-diagnosis")
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "snapshot_missing"
    missing = body["details"]["missing_snapshots"]
    assert {"table": "targets", "version": t_ver} in missing
    assert body["details"]["respondent_version"] == r_ver


def test_diagnosis_independent_of_current_status_pending(client):
    # A historical unbalanced job stays diagnosable even when the current
    # pair has no finished job (status pending).
    _upload_roster(client, ROSTER)
    impossible = [
        {"dimension": "district", "group_name": "D1", "target_count": 100.0},
        {"dimension": "district", "group_name": "D2", "target_count": 1.0},
        {"dimension": "size_band", "group_name": "S1", "target_count": 100.0},
        {"dimension": "size_band", "group_name": "S2", "target_count": 1.0},
    ]
    _upload_targets(client, impossible)
    job = _start_and_wait(client)
    job_id = job["job_id"]

    _upload_roster(
        client,
        [dict(r, household_id=f"N-{r['household_id']}") for r in ROSTER],
    )
    assert client.get("/weighting/status").json()["status"] == "pending"

    resp = client.get(f"/weighting/jobs/{job_id}/conflict-diagnosis")
    assert resp.status_code == 200
    assert resp.json()["conflict_found"] is True
