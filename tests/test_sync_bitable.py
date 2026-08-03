import importlib.util
import sqlite3
from pathlib import Path
from unittest.mock import Mock, patch

import pytest


def _load_sync_module():
    script_path = Path(__file__).parents[1] / "scripts" / "sync_bitable.py"
    spec = importlib.util.spec_from_file_location("sync_bitable", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_trail_pending_query_counts_body_series_trajectories():
    sync_bitable = _load_sync_module()
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE organizations (id INTEGER PRIMARY KEY, security_enabled INTEGER);
        CREATE TABLE person (id INTEGER PRIMARY KEY, org_id INTEGER);
        CREATE TABLE trace (
            id INTEGER PRIMARY KEY,
            person_id INTEGER,
            trail_validity TEXT,
            has_body_series INTEGER
        );
        CREATE TABLE annotation_task_v2 (id INTEGER PRIMARY KEY, task_kind TEXT);
        CREATE TABLE annotation_task_item (task_id INTEGER, item_ref INTEGER);

        INSERT INTO organizations VALUES (1, 1);
        INSERT INTO person VALUES (10, 1), (20, 1), (30, 1);
        INSERT INTO trace VALUES
            (101, 10, 'pending', 1),
            (102, 10, 'pending', 1),
            (201, 20, 'pending', 1),
            (202, 20, 'valid', 1),
            (301, 30, 'pending', 0);
        INSERT INTO annotation_task_v2 VALUES (1, 'trail_cleaning');
        INSERT INTO annotation_task_item VALUES (1, 101);
        """
    )

    count = conn.execute(sync_bitable.SQL_TRAIL_SAMPLEABLE).fetchone()[0]

    assert count == 3


def test_update_bitable_writes_all_requested_metric_fields():
    sync_bitable = _load_sync_module()
    sync_bitable.BASE_TOKEN = "base_example"
    sync_bitable.TABLE_ID = "table_example"
    sync_bitable.RECORD_MAP = {"trail": "record_example"}
    fields = {
        "待标注": 3508,
        "已标注": 100,
        "正样本": 40,
        "本周正样本": 5,
    }
    result = Mock(stdout='{"ok": true}', stderr="")

    with patch.object(sync_bitable.subprocess, "run", return_value=result) as run:
        assert sync_bitable.update_bitable({"trail": fields}) is True

    command = run.call_args.args[0]
    payload = command[command.index("--json") + 1]
    assert sync_bitable.json.loads(payload) == fields


def test_trail_annotation_metrics_keep_trajectory_and_student_counts_separate():
    sync_bitable = _load_sync_module()

    assert "COUNT(*) AS annotated_count" in (
        sync_bitable.SQL_TRAIL_ANNOTATION_METRICS
    )
    assert "SUM(t.trail_validity = 'valid') AS positive_count" in (
        sync_bitable.SQL_TRAIL_ANNOTATION_METRICS
    )
    assert "COUNT(DISTINCT CASE WHEN t.trail_validity = 'valid' THEN p.id END)" in (
        sync_bitable.SQL_TRAIL_ANNOTATION_METRICS
    )
    assert "weekly_positive_student_count" in sync_bitable.SQL_TRAIL_ANNOTATION_METRICS


def test_weekly_metrics_start_on_monday():
    sync_bitable = _load_sync_module()
    monday_start = "WEEKDAY(CURDATE())"

    assert monday_start in sync_bitable.SQL_TRAIL_ANNOTATION_METRICS
    assert monday_start in sync_bitable.SQL_BEHAVIOR_ANNOTATION_METRICS


def test_record_ids_are_loaded_from_environment(monkeypatch):
    monkeypatch.setenv(
        "ANNOTATION_RECORD_MAP_JSON",
        '{"fallDetect_school":"record_school","fallDetect_external":"record_external"}',
    )
    sync_bitable = _load_sync_module()

    assert "fallDetect" not in sync_bitable.RECORD_MAP
    assert sync_bitable.RECORD_MAP["fallDetect_school"] == "record_school"
    assert sync_bitable.RECORD_MAP["fallDetect_external"] == "record_external"


def test_behavior_queries_split_person_fall_by_event_source():
    sync_bitable = _load_sync_module()

    for sql in (
        sync_bitable.SQL_BEHAVIOR_SAMPLEABLE,
        sync_bitable.SQL_BEHAVIOR_ANNOTATION_METRICS,
    ):
        assert "fallDetect_external" in sql
        assert "fallDetect_school" in sql
        assert "ab.event_source = 'EXTERNAL_SOURCE'" in sql


def test_person_fall_reconciliation_covers_every_written_count_scope():
    sync_bitable = _load_sync_module()

    for alias in (
        "all_fall",
        "sampleable_all",
        "annotated_all",
        "positive_all",
        "weekly_positive_all",
        "null_source",
        "other_named_source",
    ):
        assert alias in sync_bitable.SQL_FALL_RECONCILIATION


def test_person_fall_reconciliation_rejects_mismatch():
    sync_bitable = _load_sync_module()
    reconciliation = {
        "all_fall": 10,
        "external_fall": 4,
        "school_fall": 6,
        "sampleable_all": 5,
        "sampleable_external": 2,
        "sampleable_school": 3,
        "annotated_all": 5,
        "annotated_external": 2,
        "annotated_school": 3,
        "positive_all": 4,
        "positive_external": 2,
        "positive_school": 2,
        "weekly_positive_all": 2,
        "weekly_positive_external": 1,
        "weekly_positive_school": 0,
    }

    with pytest.raises(RuntimeError, match="本周正样本"):
        sync_bitable.validate_fall_reconciliation(reconciliation)
