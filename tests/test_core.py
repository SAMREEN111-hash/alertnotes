"""
AlertNotes - Core Tests
Run with: pytest tests/ -v
"""

import os
import json
import tempfile
import pytest

# Use a temp DB for all tests
tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["ALERTNOTES_DB_PATH"] = tmp.name

from alertnotes import database as db
from alertnotes.enrichment import summarize_context


@pytest.fixture(autouse=True)
def fresh_db():
    """Re-init DB before each test."""
    db.init_db()
    yield
    # Clean up between tests
    with db.get_conn() as conn:
        conn.executescript("""
            DELETE FROM resolutions;
            DELETE FROM alerts;
            DELETE FROM pending_resolutions;
        """)


def test_fingerprint_is_stable():
    """Same alert always produces same fingerprint."""
    labels = {"alertname": "HighCPU", "service": "api", "pod": "api-xyz-123"}
    fp1 = db.compute_fingerprint("HighCPU", "api", "production", labels)
    # Different pod name — fingerprint should still be same
    labels2 = {"alertname": "HighCPU", "service": "api", "pod": "api-abc-456"}
    fp2 = db.compute_fingerprint("HighCPU", "api", "production", labels2)
    assert fp1 == fp2, "Fingerprint should be stable across pod name changes"


def test_fingerprint_differs_by_service():
    """Different services produce different fingerprints."""
    fp1 = db.compute_fingerprint("HighCPU", "api", "production", {})
    fp2 = db.compute_fingerprint("HighCPU", "worker", "production", {})
    assert fp1 != fp2


def test_fingerprint_differs_by_environment():
    fp1 = db.compute_fingerprint("HighCPU", "api", "production", {})
    fp2 = db.compute_fingerprint("HighCPU", "api", "staging", {})
    assert fp1 != fp2


def test_upsert_alert_creates_and_increments():
    fp = db.compute_fingerprint("TestAlert", "svc", "prod", {})
    id1 = db.upsert_alert(fp, "TestAlert", "svc", "prod", "warning", {})
    id2 = db.upsert_alert(fp, "TestAlert", "svc", "prod", "warning", {})
    assert id1 == id2, "Same alert should not create duplicate rows"

    alerts = db.get_alert_history(search="TestAlert")
    assert len(alerts) == 1
    assert alerts[0]["fire_count"] == 2


def test_save_and_retrieve_resolution():
    fp = db.compute_fingerprint("OOMKill", "image-processor", "prod", {})
    alert_id = db.upsert_alert(fp, "OOMKill", "image-processor", "prod", "critical", {})

    db.save_resolution(
        fingerprint=fp,
        alert_id=alert_id,
        fired_at="2024-01-15T02:30:00",
        resolved_at="2024-01-15T03:00:00",
        cause="Memory spike during batch image resize job",
        fix="Increased memory limit from 512MB to 2GB in values.yaml",
        resolved_by="rahul",
        auto_context={},
        commits=[{"sha": "abc1234", "message": "fix: increase memory limits"}],
    )

    resolutions = db.get_resolutions(fp)
    assert len(resolutions) == 1
    r = resolutions[0]
    assert r["cause"] == "Memory spike during batch image resize job"
    assert r["fix"] == "Increased memory limit from 512MB to 2GB in values.yaml"
    assert r["resolved_by"] == "rahul"
    assert r["duration_secs"] == 1800  # 30 minutes


def test_no_resolutions_returns_empty():
    fp = db.compute_fingerprint("NeverFired", "svc", "prod", {})
    db.upsert_alert(fp, "NeverFired", "svc", "prod", "warning", {})
    resolutions = db.get_resolutions(fp)
    assert resolutions == []


def test_resolutions_ordered_newest_first():
    fp = db.compute_fingerprint("FlappyAlert", "svc", "prod", {})
    alert_id = db.upsert_alert(fp, "FlappyAlert", "svc", "prod", "warning", {})

    for i in range(3):
        db.save_resolution(
            fingerprint=fp, alert_id=alert_id,
            fired_at=f"2024-0{i+1}-01T00:00:00",
            resolved_at=f"2024-0{i+1}-01T01:00:00",
            cause=f"Cause {i}", fix=f"Fix {i}",
            resolved_by="engineer", auto_context={}, commits=[],
        )

    resolutions = db.get_resolutions(fp)
    # Should come back newest first
    assert resolutions[0]["cause"] == "Cause 2"
    assert resolutions[1]["cause"] == "Cause 1"
    assert resolutions[2]["cause"] == "Cause 0"


def test_stats():
    fp = db.compute_fingerprint("StatAlert", "svc", "prod", {})
    alert_id = db.upsert_alert(fp, "StatAlert", "svc", "prod", "critical", {})
    db.save_resolution(
        fingerprint=fp, alert_id=alert_id,
        fired_at="2024-01-01T00:00:00",
        resolved_at="2024-01-01T01:00:00",
        cause="test", fix="test fix",
        resolved_by="tester", auto_context={}, commits=[],
    )

    stats = db.get_stats()
    assert stats["total_alerts"] >= 1
    assert stats["total_resolutions"] >= 1
    assert stats["avg_resolution_mins"] > 0


def test_create_and_use_pending():
    fp = db.compute_fingerprint("PendingAlert", "svc", "prod", {})
    token = db.create_pending(
        fingerprint=fp,
        alert_name="PendingAlert",
        service="svc",
        fired_at="2024-01-01T00:00:00",
        payload={"alert_id": 1, "resolved_at": "2024-01-01T01:00:00", "auto_context": {}},
    )
    assert len(token) > 20

    # Each call generates a unique token
    token2 = db.create_pending(
        fingerprint=fp, alert_name="PendingAlert", service="svc",
        fired_at="2024-01-02T00:00:00",
        payload={"alert_id": 1, "resolved_at": "2024-01-02T01:00:00", "auto_context": {}},
    )
    assert token != token2


def test_mark_helpful():
    fp = db.compute_fingerprint("HelpfulAlert", "svc", "prod", {})
    alert_id = db.upsert_alert(fp, "HelpfulAlert", "svc", "prod", "warning", {})
    res_id = db.save_resolution(
        fingerprint=fp, alert_id=alert_id,
        fired_at="2024-01-01T00:00:00",
        resolved_at="2024-01-01T00:30:00",
        cause="test", fix="test",
        resolved_by="tester", auto_context={}, commits=[],
    )
    db.mark_helpful(res_id, True)
    res = db.get_resolution_by_id(res_id)
    assert res["was_helpful"] == 1


def test_context_summary_with_commits():
    context = {
        "pre_fire_commits": [
            {"sha": "abc1234", "message": "deploy: update image tag", "author": "ci-bot"},
        ],
        "commits": [],
        "slack_messages": [
            {"text": "looks like the deploy is causing OOM errors", "user": "U123"},
        ],
    }
    summary = summarize_context(context)
    assert "abc1234" in summary
    assert "OOM" in summary