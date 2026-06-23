"""
AlertNotes - Database Layer
Handles all SQLite operations for alert resolution storage and retrieval.
"""

import sqlite3
import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager
from typing import Optional

DB_PATH = Path("/data/alertnotes.db")


def get_db_path() -> Path:
    import os
    return Path(os.environ.get("ALERTNOTES_DB_PATH", str(DB_PATH)))


@contextmanager
def get_conn():
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS alerts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint     TEXT NOT NULL,
                alert_name      TEXT NOT NULL,
                service         TEXT,
                environment     TEXT DEFAULT 'production',
                severity        TEXT DEFAULT 'warning',
                labels          TEXT DEFAULT '{}',
                first_seen      TEXT NOT NULL,
                last_seen       TEXT NOT NULL,
                fire_count      INTEGER DEFAULT 1,
                UNIQUE(fingerprint)
            );

            CREATE TABLE IF NOT EXISTS resolutions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_id        INTEGER NOT NULL REFERENCES alerts(id),
                fingerprint     TEXT NOT NULL,
                fired_at        TEXT NOT NULL,
                resolved_at     TEXT,
                duration_secs   INTEGER,
                cause           TEXT,
                fix             TEXT,
                resolved_by     TEXT,
                auto_context    TEXT DEFAULT '{}',
                commits         TEXT DEFAULT '[]',
                slack_thread    TEXT,
                was_helpful     INTEGER,
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY(alert_id) REFERENCES alerts(id)
            );

            CREATE TABLE IF NOT EXISTS pending_resolutions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint     TEXT NOT NULL,
                alert_name      TEXT NOT NULL,
                service         TEXT,
                fired_at        TEXT NOT NULL,
                payload         TEXT NOT NULL,
                token           TEXT NOT NULL UNIQUE,
                created_at      TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_resolutions_fingerprint
                ON resolutions(fingerprint);
            CREATE INDEX IF NOT EXISTS idx_resolutions_alert_id
                ON resolutions(alert_id);
            CREATE INDEX IF NOT EXISTS idx_alerts_fingerprint
                ON alerts(fingerprint);
        """)


def compute_fingerprint(alert_name: str, service: str, environment: str, labels: dict) -> str:
    """
    Stable fingerprint for an alert type.
    Same alert name + service + env always produce the same fingerprint,
    regardless of incident-specific labels like pod name or timestamp.
    """
    stable_labels = {
        k: v for k, v in sorted(labels.items())
        if k not in {"pod", "instance", "timestamp", "start_time", "end_time", "id"}
    }
    key = f"{alert_name}|{service}|{environment}|{json.dumps(stable_labels, sort_keys=True)}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def upsert_alert(fingerprint: str, alert_name: str, service: str,
                 environment: str, severity: str, labels: dict) -> int:
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM alerts WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()

        if existing:
            conn.execute("""
                UPDATE alerts SET last_seen = ?, fire_count = fire_count + 1
                WHERE fingerprint = ?
            """, (now, fingerprint))
            return existing["id"]
        else:
            cur = conn.execute("""
                INSERT INTO alerts
                    (fingerprint, alert_name, service, environment, severity, labels, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (fingerprint, alert_name, service, environment, severity,
                  json.dumps(labels), now, now))
            return cur.lastrowid


def create_pending(fingerprint: str, alert_name: str, service: str,
                   fired_at: str, payload: dict) -> str:
    import secrets
    token = secrets.token_urlsafe(32)
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO pending_resolutions
                (fingerprint, alert_name, service, fired_at, payload, token)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (fingerprint, alert_name, service, fired_at, json.dumps(payload), token))
    return token


def save_resolution(fingerprint: str, alert_id: int, fired_at: str,
                    resolved_at: str, cause: str, fix: str,
                    resolved_by: str, auto_context: dict,
                    commits: list, slack_thread: Optional[str] = None) -> int:
    fired_dt = datetime.fromisoformat(fired_at)
    resolved_dt = datetime.fromisoformat(resolved_at)
    duration = int((resolved_dt - fired_dt).total_seconds())

    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO resolutions
                (alert_id, fingerprint, fired_at, resolved_at, duration_secs,
                 cause, fix, resolved_by, auto_context, commits, slack_thread)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (alert_id, fingerprint, fired_at, resolved_at, duration,
              cause, fix, resolved_by,
              json.dumps(auto_context), json.dumps(commits), slack_thread))
        return cur.lastrowid


def get_resolutions(fingerprint: str, limit: int = 5) -> list:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT r.*, a.alert_name, a.service, a.environment, a.fire_count
            FROM resolutions r
            JOIN alerts a ON a.id = r.alert_id
            WHERE r.fingerprint = ?
              AND r.cause IS NOT NULL
            ORDER BY r.fired_at DESC
            LIMIT ?
        """, (fingerprint, limit)).fetchall()
        return [dict(r) for r in rows]


def get_alert_history(limit: int = 50, search: Optional[str] = None) -> list:
    with get_conn() as conn:
        if search:
            rows = conn.execute("""
                SELECT a.*, COUNT(r.id) as resolution_count,
                       MAX(r.resolved_at) as last_resolved
                FROM alerts a
                LEFT JOIN resolutions r ON r.alert_id = a.id
                WHERE a.alert_name LIKE ? OR a.service LIKE ?
                GROUP BY a.id
                ORDER BY a.last_seen DESC
                LIMIT ?
            """, (f"%{search}%", f"%{search}%", limit)).fetchall()
        else:
            rows = conn.execute("""
                SELECT a.*, COUNT(r.id) as resolution_count,
                       MAX(r.resolved_at) as last_resolved
                FROM alerts a
                LEFT JOIN resolutions r ON r.alert_id = a.id
                GROUP BY a.id
                ORDER BY a.last_seen DESC
                LIMIT ?
            """, (limit,)).fetchall()
        return [dict(r) for r in rows]


def get_resolution_by_id(resolution_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("""
            SELECT r.*, a.alert_name, a.service, a.environment
            FROM resolutions r
            JOIN alerts a ON a.id = r.alert_id
            WHERE r.id = ?
        """, (resolution_id,)).fetchone()
        return dict(row) if row else None


def mark_helpful(resolution_id: int, helpful: bool):
    with get_conn() as conn:
        conn.execute(
            "UPDATE resolutions SET was_helpful = ? WHERE id = ?",
            (1 if helpful else 0, resolution_id)
        )


def get_stats() -> dict:
    with get_conn() as conn:
        total_alerts = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
        total_resolutions = conn.execute(
            "SELECT COUNT(*) FROM resolutions WHERE cause IS NOT NULL"
        ).fetchone()[0]
        avg_duration = conn.execute(
            "SELECT AVG(duration_secs) FROM resolutions WHERE duration_secs IS NOT NULL"
        ).fetchone()[0]
        helpful_count = conn.execute(
            "SELECT COUNT(*) FROM resolutions WHERE was_helpful = 1"
        ).fetchone()[0]
        top_alerts = conn.execute("""
            SELECT alert_name, service, fire_count
            FROM alerts ORDER BY fire_count DESC LIMIT 5
        """).fetchall()

        return {
            "total_alerts": total_alerts,
            "total_resolutions": total_resolutions,
            "avg_resolution_mins": round((avg_duration or 0) / 60, 1),
            "helpful_count": helpful_count,
            "top_recurring_alerts": [dict(r) for r in top_alerts],
        }
