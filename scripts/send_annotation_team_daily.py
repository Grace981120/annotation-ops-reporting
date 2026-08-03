#!/usr/bin/env python3
"""Generate and send annotation-team daily cards and the Thursday weekly card."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo


BASE_TOKEN = os.getenv("ANNOTATION_BASE_TOKEN", "")
DAILY_TABLE_ID = os.getenv("ANNOTATION_DAILY_TABLE_ID", "")
STAFF_TABLE_ID = os.getenv("ANNOTATION_STAFF_TABLE_ID", "")
PROJECT_TABLE_ID = os.getenv("ANNOTATION_PROJECT_TABLE_ID", "")
METRICS_TABLE_ID = os.getenv("ANNOTATION_METRICS_TABLE_ID", "")

DETAIL_WEBHOOK_ACCOUNT = os.getenv(
    "ANNOTATION_DETAIL_KEYCHAIN_ACCOUNT", "annotation-reporting"
)
DETAIL_WEBHOOK_SERVICE = os.getenv(
    "ANNOTATION_DETAIL_KEYCHAIN_SERVICE", "annotation-detail-webhook"
)
FORMAL_WEBHOOK_ACCOUNT = os.getenv(
    "ANNOTATION_FORMAL_KEYCHAIN_ACCOUNT", "annotation-reporting"
)
FORMAL_WEBHOOK_SERVICE = os.getenv(
    "ANNOTATION_FORMAL_KEYCHAIN_SERVICE", "annotation-formal-webhook"
)
WEEKLY_WEBHOOK_ACCOUNT = os.getenv(
    "ANNOTATION_WEEKLY_KEYCHAIN_ACCOUNT", "annotation-reporting"
)
WEEKLY_WEBHOOK_SERVICE = os.getenv(
    "ANNOTATION_WEEKLY_KEYCHAIN_SERVICE", "annotation-weekly-webhook"
)

LARK_TENANT_DOMAIN = os.getenv("LARK_TENANT_DOMAIN", "feishu.cn")
BASE_URL = f"https://{LARK_TENANT_DOMAIN}/base/{BASE_TOKEN}"
TARGET_TASK_NAMES = [
    "人员轨迹",
    "人体检测",
    "人员倒地-学校",
    "人员倒地-外部数据源",
    "攀高",
    "吸烟",
    "打电话",
    "剧烈运动",
]


@dataclass
class SyncResult:
    success: bool
    summary: str
    fall_reconciliation: str


def run_command(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def run_lark_base(args: list[str]) -> dict[str, Any]:
    result = run_command(
        ["lark-cli", "base", *args, "--format", "json", "--as", "user"]
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "lark-cli failed")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Invalid JSON from lark-cli") from exc
    if not payload.get("ok"):
        raise RuntimeError(
            payload.get("error", {}).get("message", "lark-cli returned not ok")
        )
    return payload["data"]


def read_keychain_secret(account: str, service: str) -> str:
    env_name = {
        DETAIL_WEBHOOK_SERVICE: "ANNOTATION_DETAIL_WEBHOOK_URL",
        FORMAL_WEBHOOK_SERVICE: "ANNOTATION_FORMAL_WEBHOOK_URL",
        WEEKLY_WEBHOOK_SERVICE: "ANNOTATION_WEEKLY_WEBHOOK_URL",
    }.get(service)
    if env_name and os.getenv(env_name):
        return os.environ[env_name]
    if not account or not service:
        raise RuntimeError(
            "请设置对应的 ANNOTATION_*_WEBHOOK_URL，或配置 macOS 钥匙串账户与服务名"
        )
    result = run_command(
        [
            "security",
            "find-generic-password",
            "-a",
            account,
            "-s",
            service,
            "-w",
        ]
    )
    secret = result.stdout.strip()
    if result.returncode != 0 or not secret:
        raise RuntimeError(
            f"Failed to read macOS Keychain secret for {account}/{service}"
        )
    return secret


def run_sync_script() -> SyncResult:
    result = run_command([sys.executable, "scripts/sync_bitable.py"])
    output = f"{result.stdout}\n{result.stderr}".strip()
    success = result.returncode == 0
    fall_line = "未知"
    for line in output.splitlines():
        if "人员倒地来源对账通过" in line:
            fall_line = line.strip()
            break
    summary = "同步成功" if success else "本次平台刷新失败"
    return SyncResult(success=success, summary=summary, fall_reconciliation=fall_line)


def parse_record_date(value: Any) -> date | None:
    if value in (None, "", "-"):
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric > 10_000_000_000:
            return datetime.fromtimestamp(
                numeric / 1000, tz=ZoneInfo("Asia/Shanghai")
            ).date()
        if numeric > 1_000_000_000:
            return datetime.fromtimestamp(numeric, tz=ZoneInfo("Asia/Shanghai")).date()
        if 20_000 <= numeric <= 80_000:
            return date(1899, 12, 30) + timedelta(days=int(numeric))
    text = str(value).strip().replace("/", "-")
    for candidate in (text, text[:10]):
        try:
            return date.fromisoformat(candidate)
        except ValueError:
            continue
    return None


def parse_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def format_rate(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value * 100:.1f}%"


def format_baseline_rate(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value * 100:.1f}%"


def format_number(value: float | int | None, decimals: int = 0) -> str:
    if value is None:
        return "-"
    if decimals == 0:
        return f"{int(round(value)):,}"
    return f"{value:,.{decimals}f}"


def format_person_days(value: float | int | None) -> str:
    if value is None:
        return "-"
    rounded = round(float(value), 1)
    if rounded.is_integer():
        return f"{int(rounded):,}"
    return f"{rounded:,.1f}"


def format_target_prediction(item: dict[str, Any]) -> str:
    predicted_positive = item.get("predicted_positive")
    target = item.get("target")
    can_reach_target = (
        predicted_positive is not None
        and target is not None
        and predicted_positive >= target
    )
    estimated_person_days = item.get(
        "target_person_days" if can_reach_target else "completion_person_days"
    )
    if estimated_person_days is None:
        effort_text = "-"
    else:
        effort_text = f"{format_person_days(max(0.0, estimated_person_days))}人天"
    return (
        f"预计工期：{effort_text}\n"
        f"预计正样本 {format_number(item.get('predicted_positive'))} / "
        f"{format_number(item.get('target'))}"
    )


def format_daily_baseline(value: float | None, unit: str | None) -> str:
    if value is None:
        return "-"
    unit_text = unit or "-"
    return f"{value:.1f} {unit_text}/日"


def normalize_user_list(cell: Any) -> list[str]:
    if not isinstance(cell, list):
        return []
    names = []
    for item in cell:
        name = item.get("name") if isinstance(item, dict) else None
        if name:
            names.append(name)
    return names


def normalize_link_ids(cell: Any) -> list[str]:
    if not isinstance(cell, list):
        return []
    ids = []
    for item in cell:
        record_id = item.get("id") if isinstance(item, dict) else None
        if record_id:
            ids.append(record_id)
    return ids


def get_progress_records(start_date: date, end_date: date) -> list[dict[str, Any]]:
    if start_date == end_date:
        conditions = [["提交日期", "==", f"ExactDate({start_date.isoformat()})"]]
    else:
        conditions = [
            [
                "提交日期",
                ">",
                f"ExactDate({(start_date - timedelta(days=1)).isoformat()})",
            ],
            [
                "提交日期",
                "<",
                f"ExactDate({(end_date + timedelta(days=1)).isoformat()})",
            ],
        ]
    filter_json = json.dumps(
        {"logic": "and", "conditions": conditions}, ensure_ascii=False
    )
    data = run_lark_base(
        [
            "+record-list",
            "--base-token",
            BASE_TOKEN,
            "--table-id",
            DAILY_TABLE_ID,
            "--filter-json",
            filter_json,
            "--field-id",
            "提交人",
            "--field-id",
            "关联任务",
            "--field-id",
            "任务类型",
            "--field-id",
            "当日完成量",
            "--field-id",
            "单位",
            "--field-id",
            "当日投入时间(天)",
            "--field-id",
            "基线达成率",
            "--field-id",
            "提交日期",
            "--limit",
            "200",
        ]
    )
    fields = data["fields"]
    records: list[dict[str, Any]] = []
    for record_id, row in zip(data["record_id_list"], data["data"]):
        item = dict(zip(fields, row))
        records.append(
            {
                "record_id": record_id,
                "submitters": normalize_user_list(item["提交人"]),
                "task_ids": normalize_link_ids(item["关联任务"]),
                "task_type": item["任务类型"],
                "completed": parse_float(item["当日完成量"]) or 0.0,
                "unit": item["单位"],
                "input_days": parse_float(item["当日投入时间(天)"]),
                "baseline_rate": parse_float(item["基线达成率"]),
                "submitted_at": item["提交日期"],
                "submitted_date": parse_record_date(item["提交日期"]),
            }
        )
    return records


def get_daily_records(today: date) -> list[dict[str, Any]]:
    return get_progress_records(today, today)


def get_working_staff() -> list[str]:
    filter_json = json.dumps(
        {"logic": "and", "conditions": [["是否在岗", "==", "工作中"]]},
        ensure_ascii=False,
    )
    data = run_lark_base(
        [
            "+record-list",
            "--base-token",
            BASE_TOKEN,
            "--table-id",
            STAFF_TABLE_ID,
            "--filter-json",
            filter_json,
            "--field-id",
            "人员",
            "--field-id",
            "是否在岗",
            "--limit",
            "200",
        ]
    )
    users: list[str] = []
    for row in data["data"]:
        users.extend(normalize_user_list(row[0]))
    return sorted(set(users))


def get_metrics_records() -> list[dict[str, Any]]:
    data = run_lark_base(
        [
            "+record-list",
            "--base-token",
            BASE_TOKEN,
            "--table-id",
            METRICS_TABLE_ID,
            "--field-id",
            "任务名称",
            "--field-id",
            "已标注",
            "--field-id",
            "正样本",
            "--field-id",
            "本周正样本",
            "--field-id",
            "正样本率",
            "--field-id",
            "平均标注量",
            "--field-id",
            "待标注",
            "--field-id",
            "目标",
            "--field-id",
            "正样本覆盖人数",
            "--field-id",
            "待标注数据中没有正样本的学生人数",
            "--field-id",
            "预计可达到的正样本数",
            "--field-id",
            "达到目标还需天数",
            "--field-id",
            "标注完毕还需天数",
            "--field-id",
            "关联项目任务",
            "--field-id",
            "最后更新时间",
            "--limit",
            "100",
        ]
    )
    fields = data["fields"]
    records: list[dict[str, Any]] = []
    for record_id, row in zip(data["record_id_list"], data["data"]):
        item = dict(zip(fields, row))
        name = item["任务名称"]
        if name not in TARGET_TASK_NAMES:
            continue
        records.append(
            {
                "record_id": record_id,
                "task_name": name,
                "annotated": parse_float(item["已标注"]),
                "positive": parse_float(item["正样本"]),
                "weekly_positive": parse_float(item["本周正样本"]),
                "positive_rate": parse_float(item["正样本率"]),
                "average_annotation": parse_float(item["平均标注量"]),
                "pending": parse_float(item["待标注"]),
                "target": parse_float(item["目标"]),
                "positive_student_count": parse_float(item["正样本覆盖人数"]),
                "uncovered_pending_students": parse_float(
                    item["待标注数据中没有正样本的学生人数"]
                ),
                "predicted_positive": parse_float(item["预计可达到的正样本数"]),
                "target_person_days": parse_float(item["达到目标还需天数"]),
                "completion_person_days": parse_float(item["标注完毕还需天数"]),
                "project_task_ids": normalize_link_ids(item["关联项目任务"]),
                "last_updated": item["最后更新时间"] or "-",
            }
        )
    records.sort(key=lambda item: TARGET_TASK_NAMES.index(item["task_name"]))
    return records


def get_project_task_map(record_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not record_ids:
        return {}
    args = [
        "+record-get",
        "--base-token",
        BASE_TOKEN,
        "--table-id",
        PROJECT_TABLE_ID,
    ]
    for record_id in record_ids:
        args.extend(["--record-id", record_id])
    args.extend(
        [
            "--field-id",
            "任务/关键产物",
            "--field-id",
            "日任务基线",
            "--field-id",
            "单位",
            "--field-id",
            "任务分类",
        ]
    )
    data = run_lark_base(args)
    fields = data["fields"]
    task_map: dict[str, dict[str, Any]] = {}
    for record_id, row in zip(data["record_id_list"], data["data"]):
        item = dict(zip(fields, row))
        unit = (
            item["单位"][0]
            if isinstance(item["单位"], list) and item["单位"]
            else item["单位"]
        )
        task_type = (
            item["任务分类"][0]
            if isinstance(item["任务分类"], list) and item["任务分类"]
            else item["任务分类"]
        )
        task_map[record_id] = {
            "name": item["任务/关键产物"],
            "baseline": parse_float(item["日任务基线"]),
            "unit": unit,
            "task_type": task_type,
        }
    return task_map


def weekly_period(end_date: date) -> tuple[date, date]:
    return end_date - timedelta(days=6), end_date


def resolve_predicted_positive(record: dict[str, Any]) -> float | None:
    return record["predicted_positive"]


def enrich_progress_records(
    records: list[dict[str, Any]], project_task_map: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for record in records:
        task_id = record["task_ids"][0] if record["task_ids"] else None
        task_meta = project_task_map.get(task_id or "", {})
        submitter = record["submitters"][0] if record["submitters"] else "-"
        enriched.append(
            {
                **record,
                "submitter": submitter,
                "task_id": task_id,
                "task_name": task_meta.get("name", task_id or "-"),
                "resolved_unit": record["unit"] or task_meta.get("unit") or "-",
                "resolved_task_type": record["task_type"]
                or task_meta.get("task_type")
                or "-",
            }
        )
    return enriched


def build_ranking(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates = [
        record
        for record in records
        if record["resolved_task_type"] == "数据标注"
        and record["baseline_rate"] is not None
    ]
    top_per_person: dict[str, dict[str, Any]] = {}
    for record in candidates:
        current = top_per_person.get(record["submitter"])
        rate = record["baseline_rate"] or 0.0
        if current is None or rate > (current["baseline_rate"] or 0.0):
            top_per_person[record["submitter"]] = record
    return sorted(
        top_per_person.values(),
        key=lambda item: (
            -(item["baseline_rate"] or 0.0),
            item["submitter"],
            item["task_name"],
        ),
    )[:3]


def build_task_input_distribution(
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    task_input_map: dict[str, dict[str, Any]] = {}
    for record in records:
        input_days = record["input_days"] or 0.0
        if input_days <= 0:
            continue
        task_name = record["task_name"]
        item = task_input_map.setdefault(task_name, {"value": 0.0, "people": set()})
        item["value"] += input_days
        if record["submitter"] != "-":
            item["people"].add(record["submitter"])

    ordered = sorted(
        task_input_map.items(), key=lambda item: (-item[1]["value"], item[0])
    )
    return [
        {
            "name": f"{task_name}（{len(item['people'])}人参与）",
            "value": round(item["value"], 2),
            "task_name": task_name,
        }
        for task_name, item in ordered
    ]


def total_input_days(records: list[dict[str, Any]]) -> float:
    return round(sum((record["input_days"] or 0.0) for record in records), 2)


def build_efficiency_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    people: dict[str, dict[str, Any]] = {}
    for record in records:
        if (
            record["resolved_task_type"] != "数据标注"
            or record["baseline_rate"] is None
        ):
            continue
        submitter = record["submitter"]
        if submitter == "-":
            continue
        item = people.setdefault(
            submitter,
            {
                "rates": [],
                "tasks": set(),
                "record_count": 0,
                "input_days": 0.0,
                "low_count": 0,
            },
        )
        item["rates"].append(record["baseline_rate"])
        item["tasks"].add(record["task_name"])
        item["record_count"] += 1
        item["input_days"] += record["input_days"] or 0.0
        if record["baseline_rate"] < 0.8:
            item["low_count"] += 1

    rows = []
    for submitter, item in people.items():
        rows.append(
            {
                "submitter": submitter,
                "tasks": sorted(item["tasks"]),
                "record_count": item["record_count"],
                "input_days": round(item["input_days"], 2),
                "average_rate": sum(item["rates"]) / len(item["rates"]),
                "low_count": item["low_count"],
            }
        )
    return sorted(rows, key=lambda item: (-item["average_rate"], item["submitter"]))


def build_weekly_summary(records: list[dict[str, Any]]) -> dict[str, float | int]:
    data_annotation_records = [
        record for record in records if record["resolved_task_type"] == "数据标注"
    ]
    return {
        "task_count": len(
            {record["task_name"] for record in records if record["task_name"] != "-"}
        ),
        "video_capacity": sum(
            (record["completed"] or 0.0)
            for record in data_annotation_records
            if "视频" in record["resolved_unit"]
        ),
        "image_capacity": sum(
            (record["completed"] or 0.0)
            for record in data_annotation_records
            if "图片" in record["resolved_unit"]
        ),
        "submitter_count": len(
            {record["submitter"] for record in records if record["submitter"] != "-"}
        ),
        "total_input_days": total_input_days(records),
    }


def build_daily_review_summaries(
    records: list[dict[str, Any]], start_date: date, end_date: date
) -> list[dict[str, Any]]:
    records_by_date: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        if record["submitted_date"] is not None:
            records_by_date[record["submitted_date"]].append(record)

    summaries = []
    current = start_date
    while current <= end_date:
        day_records = records_by_date.get(current, [])
        submitted_people = {
            item["submitter"] for item in day_records if item["submitter"] != "-"
        }
        distribution = build_task_input_distribution(day_records)
        summaries.append(
            {
                "date": current,
                "submitted_count": len(submitted_people),
                "report_count": len(day_records),
                "input_days": round(
                    sum((item["input_days"] or 0.0) for item in day_records), 2
                ),
                "ranking": build_ranking(day_records),
                "top_tasks": distribution[:3],
            }
        )
        current += timedelta(days=1)
    return summaries


def build_report_dataset(today: date, sync_result: SyncResult) -> dict[str, Any]:
    daily_records = get_daily_records(today)
    working_staff = get_working_staff()
    metrics_records = get_metrics_records()

    all_task_ids = {
        task_id for record in daily_records for task_id in record["task_ids"]
    } | {
        task_id
        for record in metrics_records
        for task_id in record["project_task_ids"]
    }
    project_task_map = get_project_task_map(sorted(all_task_ids))

    enriched_daily = enrich_progress_records(daily_records, project_task_map)

    submitted_people = sorted(
        {record["submitter"] for record in enriched_daily if record["submitter"] != "-"}
    )
    unsubmitted_people = sorted(set(working_staff) - set(submitted_people))

    ranking = build_ranking(enriched_daily)

    low_baseline_records = [
        record
        for record in enriched_daily
        if record["resolved_task_type"] == "数据标注"
        and record["baseline_rate"] is not None
        and record["baseline_rate"] < 0.8
    ]
    low_baseline_records.sort(
        key=lambda item: (
            (item["baseline_rate"] or 0.0),
            item["submitter"],
            item["task_name"],
        )
    )

    task_input_distribution = build_task_input_distribution(enriched_daily)

    enriched_metrics: list[dict[str, Any]] = []
    fallback_updated_at = "-"
    for record in metrics_records:
        task_id = record["project_task_ids"][0] if record["project_task_ids"] else None
        task_meta = project_task_map.get(task_id or "", {})
        target = record["target"]
        if record["task_name"] == "人员轨迹":
            numerator = record["positive_student_count"]
            pending_value = record["uncovered_pending_students"]
        else:
            numerator = record["positive"]
            pending_value = record["pending"]
        progress = (
            None
            if not target
            else numerator / target if numerator is not None else None
        )
        predicted_positive = resolve_predicted_positive(record)
        fallback_updated_at = (
            record["last_updated"]
            if record["last_updated"] != "-"
            else fallback_updated_at
        )
        enriched_metrics.append(
            {
                **record,
                "predicted_positive": predicted_positive,
                "current_numerator": numerator,
                "current_progress": progress,
                "pending_display": pending_value,
                "baseline": task_meta.get("baseline"),
                "baseline_unit": task_meta.get("unit"),
            }
        )

    detailed_records_sorted = sorted(
        enriched_daily,
        key=lambda item: (
            1 if item["baseline_rate"] is None else 0,
            -(item["baseline_rate"] or -1.0),
            item["submitter"],
            item["task_name"],
        ),
    )

    return {
        "date": today.isoformat(),
        "sync_result": sync_result,
        "daily_records": enriched_daily,
        "working_staff": working_staff,
        "submitted_people": submitted_people,
        "unsubmitted_people": unsubmitted_people,
        "submitted_count": len(submitted_people),
        "report_count": len(enriched_daily),
        "total_input_days": total_input_days(enriched_daily),
        "unsubmitted_count": len(unsubmitted_people),
        "ranking": ranking,
        "low_baseline_records": low_baseline_records,
        "task_input_distribution": task_input_distribution,
        "metrics_records": enriched_metrics,
        "detailed_records_sorted": detailed_records_sorted,
        "metrics_fallback_updated_at": fallback_updated_at,
    }


def build_weekly_report_dataset(
    end_date: date, sync_result: SyncResult
) -> dict[str, Any]:
    start_date, end_date = weekly_period(end_date)
    progress_records = get_progress_records(start_date, end_date)
    metrics_records = get_metrics_records()
    all_task_ids = {
        task_id for record in progress_records for task_id in record["task_ids"]
    } | {
        task_id
        for record in metrics_records
        for task_id in record["project_task_ids"]
    }
    project_task_map = get_project_task_map(sorted(all_task_ids))
    enriched_records = enrich_progress_records(progress_records, project_task_map)

    enriched_metrics: list[dict[str, Any]] = []
    fallback_updated_at = "-"
    for record in metrics_records:
        target = record["target"]
        if record["task_name"] == "人员轨迹":
            numerator = record["positive_student_count"]
            pending_value = record["uncovered_pending_students"]
        else:
            numerator = record["positive"]
            pending_value = record["pending"]
        progress = (
            None
            if not target
            else numerator / target if numerator is not None else None
        )
        fallback_updated_at = (
            record["last_updated"]
            if record["last_updated"] != "-"
            else fallback_updated_at
        )
        predicted_positive = resolve_predicted_positive(record)
        enriched_metrics.append(
            {
                **record,
                "predicted_positive": predicted_positive,
                "current_numerator": numerator,
                "current_progress": progress,
                "pending_display": pending_value,
            }
        )

    return {
        "start_date": start_date,
        "end_date": end_date,
        "sync_result": sync_result,
        "records": enriched_records,
        "summary": build_weekly_summary(enriched_records),
        "task_input_distribution": build_task_input_distribution(enriched_records),
        "efficiency_rows": build_efficiency_rows(enriched_records),
        "metrics_records": enriched_metrics,
        "daily_reviews": build_daily_review_summaries(
            enriched_records, start_date, end_date
        ),
        "metrics_fallback_updated_at": fallback_updated_at,
    }


def markdown_block(
    text: str, *, margin: str = "0px 0px 12px 0px", text_size: str = "normal_v2"
) -> dict[str, Any]:
    return {
        "tag": "markdown",
        "content": text,
        "text_align": "left",
        "text_size": text_size,
        "margin": margin,
    }


def metric_column(
    label: str, value: str, note: str, background_style: str
) -> dict[str, Any]:
    return {
        "tag": "column",
        "width": "weighted",
        "weight": 1,
        "background_style": background_style,
        "padding": "12px 12px 12px 12px",
        "vertical_spacing": "4px",
        "elements": [
            markdown_block(f"**{label}**", margin="0px 0px 4px 0px"),
            markdown_block(f"# {value}", margin="0px 0px 4px 0px"),
            markdown_block(note, margin="0px 0px 0px 0px", text_size="notation"),
        ],
    }


def detailed_metric_column(
    label: str, value: str, note: str, color: str, background_style: str
) -> dict[str, Any]:
    return {
        "tag": "column",
        "width": "weighted",
        "weight": 1,
        "background_style": background_style,
        "padding": "12px 8px 12px 8px",
        "vertical_spacing": "2px",
        "elements": [
            markdown_block(
                f"## <font color='{color}'>{value}</font>",
                margin="0px",
            )
            | {"text_align": "center"},
            markdown_block(f"**{label}**", margin="0px") | {"text_align": "center"},
            markdown_block(
                f"<font color='grey'>{note}</font>",
                margin="0px",
                text_size="notation",
            )
            | {"text_align": "center"},
        ],
    }


def section_header(
    number: str, title: str, icon_token: str, element_id: str
) -> dict[str, Any]:
    return {
        "tag": "div",
        "element_id": element_id,
        "text": {
            "tag": "plain_text",
            "content": f"{number}  {title}",
            "text_size": "heading-4",
            "text_color": "default",
            "lines": 1,
        },
        "icon": {
            "tag": "standard_icon",
            "token": icon_token,
            "color": "blue",
        },
    }


def baseline_rate_option(rate: float | None) -> list[dict[str, str]]:
    if rate is None:
        return [{"text": "-", "color": "grey"}]
    if rate < 0.5:
        color = "red"
    elif rate < 0.8:
        color = "orange"
    elif rate < 1.0:
        color = "blue"
    else:
        color = "green"
    return [{"text": format_baseline_rate(rate), "color": color}]


def task_type_option(task_type: str) -> list[dict[str, str]]:
    colors = {
        "数据标注": "blue",
        "数据审核": "violet",
        "数据清洗": "purple",
    }
    return [{"text": task_type, "color": colors.get(task_type, "grey")}]


def make_table(
    columns: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    *,
    page_size: int,
    element_id: str,
    header_background_style: str = "none",
    freeze_first_column: bool = False,
    margin: str = "0px 0px 12px 0px",
) -> dict[str, Any]:
    table = {
        "tag": "table",
        "element_id": element_id,
        "page_size": page_size,
        "row_height": "auto",
        "header_style": {
            "text_align": "left",
            "text_size": "normal",
            "background_style": header_background_style,
            "text_color": "grey",
            "bold": True,
            "lines": 1,
        },
        "columns": columns,
        "rows": rows,
        "margin": margin,
    }
    if freeze_first_column:
        table["freeze_first_column"] = True
    return table


def make_chart(
    distribution: list[dict[str, Any]], *, margin: str = "0px 0px 12px 0px"
) -> dict[str, Any]:
    spec = {
        "type": "pie",
        "data": [
            {
                "id": "task_input",
                "values": [
                    {"name": item["name"], "value": item["value"]}
                    for item in distribution
                ],
            }
        ],
        "categoryField": "name",
        "valueField": "value",
        "innerRadius": 0.45,
        "title": {"visible": True, "text": "按任务投入分布（人天）"},
        "legend": {"visible": True, "orient": "bottom"},
        "tooltip": {"visible": True},
        "series": [
            {
                "type": "pie",
                "dataIndex": 0,
                "categoryField": "name",
                "valueField": "value",
                "innerRadius": 0.45,
                "padAngle": 0.01,
                "label": {"visible": True},
            }
        ],
    }
    return {
        "tag": "chart",
        "chart_spec": spec,
        "color_theme": "complementary",
        "height": "260px",
        "preview": True,
        "margin": margin,
    }


def optimized_detailed_card(report: dict[str, Any]) -> dict[str, Any]:
    elements: list[dict[str, Any]] = []
    sync_tag = "同步成功" if report["sync_result"].success else "平台刷新失败"
    elements.append(
        section_header("01", "提交概览", "member_outlined", "section_submit")
    )
    elements.append(
        {
            "tag": "column_set",
            "element_id": "daily_metrics",
            "flex_mode": "none",
            "horizontal_spacing": "12px",
            "columns": [
                detailed_metric_column(
                    "已交人数",
                    str(report["submitted_count"]),
                    "去重人数",
                    "blue",
                    "blue-50",
                ),
                detailed_metric_column(
                    "日报记录",
                    str(report["report_count"]),
                    "当日记录",
                    "violet",
                    "violet-50",
                ),
                detailed_metric_column(
                    "未提交人数",
                    str(report["unsubmitted_count"]),
                    "仅展示人数",
                    "purple",
                    "purple-50",
                ),
                detailed_metric_column(
                    "当日总投入",
                    format_person_days(report["total_input_days"]),
                    "逐条日报投入合计 · 人天",
                    "green",
                    "green-50",
                ),
            ],
        }
    )
    unsubmitted_text = (
        "、".join(report["unsubmitted_people"])
        if report["unsubmitted_people"]
        else "今日全员已提交"
    )
    elements.append(
        {
            "tag": "div",
            "text": {
                "tag": "plain_text",
                "content": f"未交名单：{unsubmitted_text}",
                "text_size": "notation",
                "text_color": "grey",
                "lines": 2,
            },
            "icon": {
                "tag": "standard_icon",
                "token": "info_outlined",
                "color": "grey",
            },
        }
    )

    elements.append(section_header("02", "效能洞察", "done_outlined", "section_effect"))
    elements.append(
        markdown_block(
            "<font color='grey'>仅数据标注任务参与；每人取当日最高基线达成率。</font>",
            margin="0px",
            text_size="notation",
        )
    )
    ranking = report["ranking"]
    if ranking:
        colors = ["blue-50", "violet-50", "purple-50"]
        medals = ["🥇", "🥈", "🥉"]
        columns = []
        for index, item in enumerate(ranking, start=1):
            columns.append(
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "background_style": colors[index - 1],
                    "padding": "12px 8px 12px 8px",
                    "vertical_spacing": "4px",
                    "elements": [
                        markdown_block(
                            f"**{medals[index - 1]} 第{index}名**",
                            margin="0px",
                        ),
                        {
                            "tag": "div",
                            "text": {
                                "tag": "plain_text",
                                "content": item["submitter"],
                                "text_size": "normal",
                                "lines": 1,
                            },
                        },
                        {
                            "tag": "div",
                            "text": {
                                "tag": "plain_text",
                                "content": (
                                    f"{format_baseline_rate(item['baseline_rate'])} · "
                                    f"{item['task_name']}"
                                ),
                                "text_size": "notation",
                                "text_color": "grey",
                                "lines": 2,
                            },
                        },
                    ],
                }
            )
        elements.append(
            {
                "tag": "column_set",
                "element_id": "daily_ranking",
                "flex_mode": "none",
                "horizontal_spacing": "12px",
                "columns": columns,
            }
        )
    else:
        elements.append(
            markdown_block(
                "<font color='grey'>今日暂无数据标注排行</font>", margin="0px"
            )
        )

    low_baseline_rows = []
    for item in report["low_baseline_records"]:
        low_baseline_rows.append(
            {
                "person": item["submitter"],
                "task": item["task_name"],
                "progress": (
                    f"{format_number(item['completed'])} {item['resolved_unit']}\n"
                    f"{item['input_days']:.1f} 人天"
                ),
                "rate": baseline_rate_option(item["baseline_rate"]),
            }
        )
    if low_baseline_rows:
        warning_people = len(
            {item["submitter"] for item in report["low_baseline_records"]}
        )
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "plain_text",
                    "content": (
                        f"低基线预警 · {len(low_baseline_rows)} 个任务 · "
                        f"{warning_people} 人"
                    ),
                    "text_size": "normal",
                    "text_color": "default",
                    "lines": 1,
                },
                "icon": {
                    "tag": "standard_icon",
                    "token": "warning_outlined",
                    "color": "red",
                },
            }
        )
        elements.append(
            make_table(
                [
                    {
                        "name": "person",
                        "display_name": "人员",
                        "data_type": "text",
                        "width": "18%",
                    },
                    {
                        "name": "task",
                        "display_name": "任务",
                        "data_type": "lark_md",
                        "width": "32%",
                    },
                    {
                        "name": "progress",
                        "display_name": "完成量 / 投入",
                        "data_type": "lark_md",
                        "width": "30%",
                    },
                    {
                        "name": "rate",
                        "display_name": "基线达成率",
                        "data_type": "options",
                        "width": "20%",
                    },
                ],
                low_baseline_rows,
                page_size=min(10, max(1, len(low_baseline_rows))),
                element_id="warning_table",
                header_background_style="grey",
                freeze_first_column=True,
                margin="0px",
            )
        )
    else:
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "plain_text",
                    "content": "今日数据标注任务暂无低于 80% 的基线预警",
                    "text_size": "notation",
                    "text_color": "grey",
                    "lines": 1,
                },
                "icon": {
                    "tag": "standard_icon",
                    "token": "done_outlined",
                    "color": "blue",
                },
            }
        )

    elements.append(section_header("03", "投入分布", "time_outlined", "section_input"))
    if report["task_input_distribution"]:
        elements.append(
            markdown_block(
                "<font color='grey'>仅统计当日日报中的非零投入，单位为人天；按占比降序。</font>",
                margin="0px",
                text_size="notation",
            )
        )
        elements.append(make_chart(report["task_input_distribution"], margin="0px"))
    else:
        elements.append(
            markdown_block(
                "<font color='grey'>今日暂无可统计的投入数据</font>", margin="0px"
            )
        )

    elements.append(
        section_header("04", "标注平台 综合进度", "setting_outlined", "section_kv")
    )
    if not report["sync_result"].success:
        elements.append(
            markdown_block(
                "<font color='orange'>本次平台刷新失败，以下数据使用多维表格"
                f"最后更新时间 {report['metrics_fallback_updated_at']}。</font>",
                margin="0px",
            )
        )
    elements.append(
        markdown_block(
            "<font color='grey'>人员轨迹按学生人数统计，其他任务按样本数量统计；"
            "预计工期为达到目标所需投入人天。</font>",
            margin="0px",
            text_size="notation",
        )
    )
    kv_rows = []
    for item in report["metrics_records"]:
        numerator_text = format_number(item["current_numerator"])
        target_text = format_number(item["target"])
        progress_text = (
            "-"
            if item["current_progress"] is None
            else f"{item['current_progress'] * 100:.1f}%"
        )
        kv_rows.append(
            {
                "task": item["task_name"],
                "progress": (
                    f"**{numerator_text} / {target_text}**\n"
                    f"<font color='grey'>进度 {progress_text}</font>"
                ),
                "quality": (
                    f"正样本率 {format_rate(item['positive_rate'])}\n"
                    f"待标注 {format_number(item['pending_display'])}"
                ),
                "predict": format_target_prediction(item),
                "baseline": format_daily_baseline(
                    item["baseline"], item["baseline_unit"]
                ),
            }
        )
    elements.append(
        make_table(
            [
                {
                    "name": "task",
                    "display_name": "任务",
                    "data_type": "text",
                    "width": "16%",
                },
                {
                    "name": "progress",
                    "display_name": "进度",
                    "data_type": "lark_md",
                    "width": "20%",
                },
                {
                    "name": "quality",
                    "display_name": "质量与库存",
                    "data_type": "lark_md",
                    "width": "20%",
                },
                {
                    "name": "predict",
                    "display_name": "目标预测",
                    "data_type": "lark_md",
                    "width": "28%",
                },
                {
                    "name": "baseline",
                    "display_name": "任务基线",
                    "data_type": "lark_md",
                    "width": "16%",
                },
            ],
            kv_rows,
            page_size=8,
            element_id="metrics_table",
            header_background_style="grey",
            freeze_first_column=True,
            margin="0px",
        )
    )

    elements.append(
        section_header("05", "日报详细记录", "approval_outlined", "section_detail")
    )
    elements.append(
        markdown_block(
            "<font color='grey'>按每条记录的基线达成率降序展示；任务类型使用标签区分。</font>",
            margin="0px",
            text_size="notation",
        )
    )
    detail_rows = []
    for item in report["detailed_records_sorted"]:
        input_text = (
            "-" if item["input_days"] is None else f"{item['input_days']:.1f} 人天"
        )
        detail_rows.append(
            {
                "person": item["submitter"],
                "task": item["task_name"],
                "task_type": task_type_option(item["resolved_task_type"]),
                "progress": (
                    f"**{format_number(item['completed'])} "
                    f"{item['resolved_unit']}**\n"
                    f"<font color='grey'>{input_text}</font>"
                ),
                "rate": baseline_rate_option(item["baseline_rate"]),
            }
        )
    elements.append(
        make_table(
            [
                {
                    "name": "person",
                    "display_name": "人员",
                    "data_type": "text",
                    "width": "12%",
                },
                {
                    "name": "task",
                    "display_name": "任务",
                    "data_type": "lark_md",
                    "width": "23%",
                },
                {
                    "name": "task_type",
                    "display_name": "任务类型",
                    "data_type": "options",
                    "width": "15%",
                },
                {
                    "name": "progress",
                    "display_name": "完成量 / 投入",
                    "data_type": "lark_md",
                    "width": "30%",
                },
                {
                    "name": "rate",
                    "display_name": "基线达成率",
                    "data_type": "options",
                    "width": "20%",
                },
            ],
            detail_rows,
            page_size=10,
            element_id="detail_table",
            header_background_style="grey",
            freeze_first_column=True,
            margin="0px",
        )
    )
    elements.append(
        {
            "tag": "column_set",
            "horizontal_spacing": "12px",
            "margin": "0px 0px 0px 0px",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "打开今日日报"},
                            "type": "primary_filled",
                            "width": "fill",
                            "size": "medium",
                            "behaviors": [
                                {
                                    "type": "open_url",
                                    "default_url": f"{BASE_URL}?table={DAILY_TABLE_ID}",
                                }
                            ],
                            "margin": "0px 0px 0px 0px",
                        }
                    ],
                },
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [
                        {
                            "tag": "button",
                            "text": {
                                "tag": "plain_text",
                                "content": "查看 标注平台 进度",
                            },
                            "type": "default",
                            "width": "fill",
                            "size": "medium",
                            "behaviors": [
                                {
                                    "type": "open_url",
                                    "default_url": f"{BASE_URL}?table={METRICS_TABLE_ID}",
                                }
                            ],
                            "margin": "0px 0px 0px 0px",
                        }
                    ],
                },
            ],
        }
    )
    return {
        "schema": "2.0",
        "config": {
            "width_mode": "fill",
            "update_multi": True,
            "summary": {
                "content": (
                    f"{report['date']} · 已交 {report['submitted_count']} 人 · "
                    f"日报 {report['report_count']} 条"
                )
            },
        },
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "数据标注团队日报（详细）"},
            "subtitle": {"tag": "plain_text", "content": report["date"]},
            "text_tag_list": [
                {
                    "tag": "text_tag",
                    "text": {"tag": "plain_text", "content": sync_tag},
                    "color": "blue",
                },
                {
                    "tag": "text_tag",
                    "text": {
                        "tag": "plain_text",
                        "content": f"未提交 {report['unsubmitted_count']} 人",
                    },
                    "color": "grey",
                },
            ],
            "icon": {
                "tag": "standard_icon",
                "token": "wiki-bitable_colorful",
            },
        },
        "body": {
            "direction": "vertical",
            "vertical_spacing": "12px",
            "padding": "12px 12px 20px 12px",
            "elements": elements,
        },
    }


def detailed_card(report: dict[str, Any]) -> dict[str, Any]:
    elements: list[dict[str, Any]] = []
    sync_tag = "同步成功" if report["sync_result"].success else "平台刷新失败"
    elements.append(markdown_block("**① 提交概览**"))
    elements.append(
        {
            "tag": "column_set",
            "flex_mode": "none",
            "horizontal_spacing": "12px",
            "margin": "0px 0px 12px 0px",
            "columns": [
                metric_column(
                    "已交人数",
                    str(report["submitted_count"]),
                    "去重提交人数",
                    "blue-50",
                ),
                metric_column(
                    "日报记录", str(report["report_count"]), "当日全部记录", "grey-50"
                ),
                metric_column(
                    "未提交人数",
                    str(report["unsubmitted_count"]),
                    "不展示应交人数",
                    "violet-50",
                ),
                metric_column(
                    "当日总投入",
                    format_person_days(report["total_input_days"]),
                    "逐条日报投入合计 · 人天",
                    "green-50",
                ),
            ],
        }
    )
    unsubmitted_text = (
        "、".join(report["unsubmitted_people"])
        if report["unsubmitted_people"]
        else "今日全员已提交"
    )
    elements.append(
        markdown_block(f"<font color='grey'>未交名单：{unsubmitted_text}</font>")
    )

    elements.append(markdown_block("**② 效能洞察**"))
    ranking = report["ranking"]
    if ranking:
        colors = ["yellow-50", "grey-50", "orange-50"]
        medals = ["🥇", "🥈", "🥉"]
        columns = []
        for index, item in enumerate(ranking, start=1):
            columns.append(
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "background_style": colors[index - 1],
                    "padding": "12px 12px 12px 12px",
                    "elements": [
                        markdown_block(
                            f"**{medals[index - 1]} 第{index}名**",
                            margin="0px 0px 4px 0px",
                        ),
                        markdown_block(item["submitter"], margin="0px 0px 4px 0px"),
                        markdown_block(
                            f"{format_baseline_rate(item['baseline_rate'])}\n"
                            f"{item['task_name']}",
                            margin="0px 0px 0px 0px",
                        ),
                    ],
                }
            )
        elements.append(
            {
                "tag": "column_set",
                "flex_mode": "bisect",
                "horizontal_spacing": "12px",
                "margin": "0px 0px 12px 0px",
                "columns": columns,
            }
        )
    else:
        elements.append(
            markdown_block("<font color='grey'>今日暂无数据标注排行</font>")
        )

    low_baseline_rows = []
    for item in report["low_baseline_records"]:
        marker = "🔴" if (item["baseline_rate"] or 0.0) < 0.5 else "🟠"
        low_baseline_rows.append(
            {
                "person": item["submitter"],
                "task": item["task_name"],
                "progress": (
                    f"{format_number(item['completed'])} {item['resolved_unit']}\n"
                    f"{item['input_days']:.1f} 人天"
                ),
                "rate": f"{marker} {format_baseline_rate(item['baseline_rate'])}",
            }
        )
    if low_baseline_rows:
        warning_people = len(
            {item["submitter"] for item in report["low_baseline_records"]}
        )
        elements.append(
            markdown_block(
                "<font color='orange'>低基线预警："
                f"预警任务数 {len(low_baseline_rows)}，"
                f"预警人员数 {warning_people}</font>"
            )
        )
        elements.append(
            make_table(
                [
                    {"name": "person", "display_name": "人员", "data_type": "text"},
                    {"name": "task", "display_name": "任务", "data_type": "lark_md"},
                    {
                        "name": "progress",
                        "display_name": "完成量 / 投入",
                        "data_type": "lark_md",
                    },
                    {
                        "name": "rate",
                        "display_name": "基线达成率",
                        "data_type": "lark_md",
                    },
                ],
                low_baseline_rows,
                page_size=min(10, max(1, len(low_baseline_rows))),
                element_id="warning_table",
            )
        )
    else:
        elements.append(
            markdown_block(
                "<font color='green'>今日数据标注任务暂无低于80%的基线预警</font>"
            )
        )

    elements.append(markdown_block("**③ 投入分布**"))
    if report["task_input_distribution"]:
        elements.append(
            markdown_block(
                "<font color='grey'>统计口径：仅统计当日日报中非零投入，单位为人天。</font>"
            )
        )
        elements.append(make_chart(report["task_input_distribution"]))
    else:
        elements.append(
            markdown_block("<font color='grey'>今日暂无可统计的投入数据</font>")
        )

    elements.append(markdown_block("**④ 标注平台综合进度**"))
    if not report["sync_result"].success:
        elements.append(
            markdown_block(
                "<font color='orange'>本次平台刷新失败，以下数据使用多维表格"
                f"最后更新时间 {report['metrics_fallback_updated_at']}。</font>"
            )
        )
    elements.append(
        markdown_block(
            "<font color='grey'>人员轨迹按学生人数统计，其他任务按样本数量统计；"
            "预计工期为达到目标所需投入人天。</font>"
        )
    )
    kv_rows = []
    for item in report["metrics_records"]:
        numerator_text = format_number(item["current_numerator"])
        target_text = format_number(item["target"])
        progress_text = (
            "-"
            if item["current_progress"] is None
            else f"{item['current_progress'] * 100:.1f}%"
        )
        kv_rows.append(
            {
                "task": item["task_name"],
                "progress": f"{numerator_text} / {target_text}\n进度 {progress_text}",
                "quality": (
                    f"正样本率 {format_rate(item['positive_rate'])}\n"
                    f"待标注 {format_number(item['pending_display'])}"
                ),
                "predict": format_target_prediction(item),
                "baseline": format_daily_baseline(
                    item["baseline"], item["baseline_unit"]
                ),
            }
        )
    elements.append(
        make_table(
            [
                {"name": "task", "display_name": "任务", "data_type": "text"},
                {
                    "name": "progress",
                    "display_name": "当前进度",
                    "data_type": "lark_md",
                },
                {
                    "name": "quality",
                    "display_name": "质量与库存",
                    "data_type": "lark_md",
                },
                {"name": "predict", "display_name": "目标预测", "data_type": "lark_md"},
                {
                    "name": "baseline",
                    "display_name": "任务基线",
                    "data_type": "lark_md",
                },
            ],
            kv_rows,
            page_size=8,
            element_id="metrics_table",
        )
    )

    elements.append(markdown_block("**⑤ 日报详细记录**"))
    detail_rows = []
    for item in report["detailed_records_sorted"]:
        input_text = (
            "-" if item["input_days"] is None else f"{item['input_days']:.1f} 人天"
        )
        detail_rows.append(
            {
                "person": item["submitter"],
                "task": item["task_name"],
                "task_type": item["resolved_task_type"],
                "progress": (
                    f"{format_number(item['completed'])} "
                    f"{item['resolved_unit']}\n{input_text}"
                ),
                "rate": format_baseline_rate(item["baseline_rate"]),
            }
        )
    elements.append(
        make_table(
            [
                {"name": "person", "display_name": "人员", "data_type": "text"},
                {"name": "task", "display_name": "任务", "data_type": "lark_md"},
                {"name": "task_type", "display_name": "任务类型", "data_type": "text"},
                {
                    "name": "progress",
                    "display_name": "完成量 / 投入",
                    "data_type": "lark_md",
                },
                {"name": "rate", "display_name": "基线达成率", "data_type": "text"},
            ],
            detail_rows,
            page_size=10,
            element_id="detail_table",
        )
    )
    elements.append(
        {
            "tag": "column_set",
            "horizontal_spacing": "12px",
            "margin": "0px 0px 0px 0px",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "查看今日日报"},
                            "type": "default",
                            "width": "fill",
                            "size": "medium",
                            "behaviors": [
                                {
                                    "type": "open_url",
                                    "default_url": f"{BASE_URL}?table={DAILY_TABLE_ID}",
                                }
                            ],
                            "margin": "0px 0px 0px 0px",
                        }
                    ],
                },
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "查看 标注平台"},
                            "type": "primary",
                            "width": "fill",
                            "size": "medium",
                            "behaviors": [
                                {
                                    "type": "open_url",
                                    "default_url": f"{BASE_URL}?table={METRICS_TABLE_ID}",
                                }
                            ],
                            "margin": "0px 0px 0px 0px",
                        }
                    ],
                },
            ],
        }
    )
    return {
        "schema": "2.0",
        "config": {"width_mode": "fill"},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "数据标注团队日报（详细）"},
            "subtitle": {"tag": "plain_text", "content": report["date"]},
            "text_tag_list": [
                {
                    "tag": "text_tag",
                    "text": {"tag": "plain_text", "content": sync_tag},
                    "color": "blue",
                },
                {
                    "tag": "text_tag",
                    "text": {
                        "tag": "plain_text",
                        "content": f"未提交 {report['unsubmitted_count']} 人",
                    },
                    "color": "grey",
                },
            ],
            "icon": {
                "tag": "standard_icon",
                "token": "chart_colorful",
                "color": "blue",
            },
        },
        "body": {"padding": "12px 12px 12px 12px", "elements": elements},
    }


def formal_card(report: dict[str, Any]) -> dict[str, Any]:
    elements: list[dict[str, Any]] = []
    sync_tag = "同步成功" if report["sync_result"].success else "平台刷新失败"
    elements.append(
        {
            "tag": "column_set",
            "flex_mode": "trisect",
            "horizontal_spacing": "12px",
            "margin": "0px 0px 12px 0px",
            "columns": [
                metric_column(
                    "已交人数",
                    str(report["submitted_count"]),
                    "去重提交人数",
                    "blue-50",
                ),
                metric_column(
                    "日报记录", str(report["report_count"]), "当日全部记录", "grey-50"
                ),
                metric_column(
                    "未提交人数",
                    str(report["unsubmitted_count"]),
                    "仅展示数量",
                    "violet-50",
                ),
            ],
        }
    )
    elements.append(markdown_block("**数据标注表现排行**"))
    elements.append(
        markdown_block(
            "<font color='grey'>仅‘数据标注’任务参与排名；每人取当日最高表现，不展示具体达成率</font>"
        )
    )
    ranking = report["ranking"]
    if ranking:
        colors = ["yellow-50", "grey-50", "orange-50"]
        medals = ["🥇", "🥈", "🥉"]
        columns = []
        for index, item in enumerate(ranking, start=1):
            columns.append(
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "background_style": colors[index - 1],
                    "padding": "12px 12px 12px 12px",
                    "elements": [
                        markdown_block(
                            f"**{medals[index - 1]} 第{index}名**",
                            margin="0px 0px 4px 0px",
                        ),
                        markdown_block(item["submitter"], margin="0px 0px 4px 0px"),
                        markdown_block(item["task_name"], margin="0px 0px 0px 0px"),
                    ],
                }
            )
        elements.append(
            {
                "tag": "column_set",
                "flex_mode": "bisect",
                "horizontal_spacing": "12px",
                "margin": "0px 0px 12px 0px",
                "columns": columns,
            }
        )
    else:
        elements.append(
            markdown_block("<font color='grey'>今日暂无数据标注排行</font>")
        )

    elements.append(markdown_block("**按任务投入分布（人天）**"))
    if report["task_input_distribution"]:
        elements.append(make_chart(report["task_input_distribution"]))
    else:
        elements.append(
            markdown_block("<font color='grey'>今日暂无可统计的投入数据</font>")
        )

    elements.append(markdown_block("**标注平台综合进度**"))
    elements.append(
        markdown_block(
            "<font color='grey'>人员轨迹按学生人数统计，其他任务按样本数量统计；"
            "预计工期为达到目标所需投入人天。</font>"
        )
    )
    kv_rows = []
    for item in report["metrics_records"]:
        numerator_text = format_number(item["current_numerator"])
        target_text = format_number(item["target"])
        progress_text = (
            "-"
            if item["current_progress"] is None
            else f"{item['current_progress'] * 100:.1f}%"
        )
        kv_rows.append(
            {
                "task": item["task_name"],
                "progress": f"{numerator_text} / {target_text}\n进度 {progress_text}",
                "quality": (
                    f"正样本率 {format_rate(item['positive_rate'])}\n"
                    f"待标注 {format_number(item['pending_display'])}"
                ),
                "predict": format_target_prediction(item),
            }
        )
    elements.append(
        make_table(
            [
                {"name": "task", "display_name": "任务", "data_type": "text"},
                {
                    "name": "progress",
                    "display_name": "当前进度",
                    "data_type": "lark_md",
                },
                {
                    "name": "quality",
                    "display_name": "质量与库存",
                    "data_type": "lark_md",
                },
                {
                    "name": "predict",
                    "display_name": "目标预测",
                    "data_type": "lark_md",
                },
            ],
            kv_rows,
            page_size=8,
            element_id="metrics_table_formal",
        )
    )
    return {
        "schema": "2.0",
        "config": {"width_mode": "fill"},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "数据标注团队日报"},
            "subtitle": {"tag": "plain_text", "content": report["date"]},
            "text_tag_list": [
                {
                    "tag": "text_tag",
                    "text": {"tag": "plain_text", "content": sync_tag},
                    "color": "blue",
                },
                {
                    "tag": "text_tag",
                    "text": {
                        "tag": "plain_text",
                        "content": f"未提交 {report['unsubmitted_count']} 人",
                    },
                    "color": "grey",
                },
            ],
            "icon": {
                "tag": "standard_icon",
                "token": "chart_colorful",
                "color": "blue",
            },
        },
        "body": {"padding": "12px 12px 12px 12px", "elements": elements},
    }


def weekly_card(report: dict[str, Any]) -> dict[str, Any]:
    elements: list[dict[str, Any]] = []
    sync_tag = "同步成功" if report["sync_result"].success else "平台刷新失败"
    summary = report["summary"]

    elements.append(
        section_header("01", "本周核心指标", "member_outlined", "weekly_core")
    )
    elements.append(
        {
            "tag": "column_set",
            "element_id": "weekly_primary_metrics",
            "flex_mode": "none",
            "horizontal_spacing": "12px",
            "margin": "0px 0px 12px 0px",
            "columns": [
                detailed_metric_column(
                    "本周任务个数",
                    str(summary["task_count"]),
                    "按任务名称去重",
                    "blue",
                    "blue-50",
                ),
                detailed_metric_column(
                    "视频标注产能",
                    format_number(summary["video_capacity"]),
                    "数据标注 · 个视频",
                    "violet",
                    "violet-50",
                ),
                detailed_metric_column(
                    "图片标注产能",
                    format_number(summary["image_capacity"]),
                    "数据标注 · 张图片",
                    "purple",
                    "purple-50",
                ),
            ],
        }
    )
    elements.append(
        {
            "tag": "column_set",
            "element_id": "weekly_secondary_metrics",
            "flex_mode": "none",
            "horizontal_spacing": "12px",
            "margin": "0px 0px 12px 0px",
            "columns": [
                detailed_metric_column(
                    "递交日报人数",
                    str(summary["submitter_count"]),
                    "周期内去重人数",
                    "blue",
                    "blue-50",
                ),
                detailed_metric_column(
                    "总投入人天",
                    format_person_days(summary["total_input_days"]),
                    "周期内日报投入合计",
                    "violet",
                    "violet-50",
                ),
            ],
        }
    )

    elements.append(section_header("02", "投入分布", "info_outlined", "weekly_input"))
    if report["task_input_distribution"]:
        elements.append(
            markdown_block(
                "<font color='grey'>统计上周五至本周四的非零投入，"
                "按投入占比降序；人数为周期内该任务参与人员去重数。</font>"
            )
        )
        elements.append(make_chart(report["task_input_distribution"]))
    else:
        elements.append(
            markdown_block("<font color='grey'>本周期暂无可统计的投入数据</font>")
        )

    elements.append(
        section_header(
            "03", "标注平台 综合进度", "wiki-bitable_colorful", "weekly_progress"
        )
    )
    if not report["sync_result"].success:
        elements.append(
            markdown_block(
                "<font color='orange'>本次平台刷新失败，以下数据使用多维表格"
                f"最后更新时间 {report['metrics_fallback_updated_at']}。</font>"
            )
        )
    elements.append(
        markdown_block(
            "<font color='grey'>人员轨迹按学生人数统计，其他任务按样本数量统计；"
            "预计工期为达到目标所需投入人天。</font>"
        )
    )
    kv_rows = []
    for item in report["metrics_records"]:
        numerator_text = format_number(item["current_numerator"])
        target_text = format_number(item["target"])
        progress_text = (
            "-"
            if item["current_progress"] is None
            else f"{item['current_progress'] * 100:.1f}%"
        )
        kv_rows.append(
            {
                "task": item["task_name"],
                "progress": f"{numerator_text} / {target_text}\n进度 {progress_text}",
                "quality": (
                    f"正样本率 {format_rate(item['positive_rate'])}\n"
                    f"待标注 {format_number(item['pending_display'])}"
                ),
                "predict": format_target_prediction(item),
            }
        )
    elements.append(
        make_table(
            [
                {
                    "name": "task",
                    "display_name": "任务",
                    "data_type": "text",
                    "width": "18%",
                },
                {
                    "name": "progress",
                    "display_name": "当前进度",
                    "data_type": "lark_md",
                    "width": "20%",
                },
                {
                    "name": "quality",
                    "display_name": "质量与库存",
                    "data_type": "lark_md",
                    "width": "20%",
                },
                {
                    "name": "predict",
                    "display_name": "目标预测",
                    "data_type": "lark_md",
                    "width": "42%",
                },
            ],
            kv_rows,
            page_size=8,
            element_id="weekly_kv_table",
        )
    )

    elements.append(section_header("04", "效能排行", "done_outlined", "weekly_effect"))
    elements.append(
        markdown_block(
            "<font color='grey'>仅展示“数据标注”任务前三名；个人平均值为该人员周期内所有有效日报记录的基线达成率算术平均。</font>"
        )
    )
    efficiency_top_three = report["efficiency_rows"][:3]
    if efficiency_top_three:
        colors = ["yellow-50", "grey-50", "orange-50"]
        medals = ["🥇", "🥈", "🥉"]
        columns = []
        for index, item in enumerate(efficiency_top_three):
            columns.append(
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "background_style": colors[index],
                    "padding": "12px 12px 12px 12px",
                    "vertical_spacing": "4px",
                    "elements": [
                        markdown_block(
                            f"**{medals[index]} 第{index + 1}名**",
                            margin="0px 0px 4px 0px",
                        ),
                        {
                            "tag": "div",
                            "text": {
                                "tag": "plain_text",
                                "content": item["submitter"],
                                "text_size": "normal",
                                "lines": 1,
                            },
                        },
                        markdown_block(
                            f"个人平均 {format_baseline_rate(item['average_rate'])}\n"
                            f"{item['record_count']} 条记录 · {item['input_days']:.2f} 人天\n"
                            f"{len(item['tasks'])} 个任务",
                            margin="0px 0px 0px 0px",
                            text_size="notation",
                        ),
                    ],
                }
            )
        elements.append(
            {
                "tag": "column_set",
                "flex_mode": "trisect",
                "horizontal_spacing": "12px",
                "margin": "0px 0px 12px 0px",
                "columns": columns,
            }
        )
    else:
        elements.append(
            markdown_block("<font color='grey'>本周期暂无数据标注效能数据</font>")
        )

    elements.append(
        section_header("05", "本周日报回顾", "calendar_colorful", "weekly_review")
    )
    elements.append(
        markdown_block(
            "<font color='grey'>点击日期标题即可在当前卡片内展开或收起摘要。</font>"
        )
    )
    weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    review_count = 0
    for summary in report["daily_reviews"]:
        if summary["report_count"] == 0:
            continue
        ranking_text = (
            "；".join(
                f"{index}.{item['submitter']} "
                f"{format_baseline_rate(item['baseline_rate'])} "
                f"{item['task_name']}"
                for index, item in enumerate(summary["ranking"], start=1)
            )
            or "当日暂无数据标注排行"
        )
        task_text = (
            "；".join(
                f"{item['task_name']} {item['value']:.2f}人天"
                for item in summary["top_tasks"]
            )
            or "当日暂无非零投入"
        )
        elements.append(
            {
                "tag": "collapsible_panel",
                "expanded": False,
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": (
                            f"{summary['date'].strftime('%m-%d')} "
                            f"{weekday_names[summary['date'].weekday()]} · "
                            f"{summary['submitted_count']}人提交 / {summary['report_count']}条"
                        ),
                    },
                    "background_color": "grey-50",
                    "width": "fill",
                    "icon": {
                        "tag": "standard_icon",
                        "token": "calendar_colorful",
                        "size": "small",
                    },
                    "icon_position": "right",
                    "icon_expanded_angle": 180,
                },
                "background_color": "grey-50",
                "border": {"color": "grey-200", "corner_radius": "8px"},
                "padding": "8px 12px 8px 12px",
                "margin": "0px 0px 8px 0px",
                "elements": [
                    {
                        "tag": "markdown",
                        "content": (
                            f"**提交与投入**  {summary['submitted_count']} 人 · "
                            f"{summary['report_count']} 条 · "
                            f"{summary['input_days']:.2f} 人天\n"
                            f"**数据标注前三**  {ranking_text}\n"
                            f"**投入前三任务**  {task_text}"
                        ),
                    }
                ],
            }
        )
        review_count += 1
    if review_count == 0:
        elements.append(
            markdown_block("<font color='grey'>本周前几天暂无日报记录</font>")
        )

    return {
        "schema": "2.0",
        "config": {"width_mode": "fill"},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "数据标注团队周报"},
            "subtitle": {
                "tag": "plain_text",
                "content": f"{report['start_date'].isoformat()} 至 {report['end_date'].isoformat()}",
            },
            "text_tag_list": [
                {
                    "tag": "text_tag",
                    "text": {"tag": "plain_text", "content": sync_tag},
                    "color": "blue",
                },
                {
                    "tag": "text_tag",
                    "text": {"tag": "plain_text", "content": "周会数据"},
                    "color": "violet",
                },
            ],
            "icon": {
                "tag": "standard_icon",
                "token": "chart_colorful",
                "color": "blue",
            },
        },
        "body": {
            "direction": "vertical",
            "vertical_spacing": "12px",
            "padding": "12px 12px 20px 12px",
            "elements": elements,
        },
    }


def validate_common_card(card: dict[str, Any], *, expected_chart_count: int) -> None:
    if card.get("schema") != "2.0":
        raise ValueError("P0 failed: schema must be 2.0")
    if card.get("config", {}).get("width_mode") != "fill":
        raise ValueError("P0 failed: width_mode must be fill")
    if card.get("header", {}).get("template") != "blue":
        raise ValueError("P0 failed: header template must be blue")
    elements = card.get("body", {}).get("elements", [])
    chart_count = sum(1 for element in elements if element.get("tag") == "chart")
    if chart_count != expected_chart_count:
        raise ValueError("P2 failed: unexpected chart count")
    for element in elements:
        if element.get("tag") == "table" and len(element.get("columns", [])) > 5:
            raise ValueError("P3 failed: table columns exceed five")


def validate_optimized_detailed_card(
    card: dict[str, Any], report: dict[str, Any]
) -> None:
    validate_common_card(
        card, expected_chart_count=1 if report["task_input_distribution"] else 0
    )
    elements = card["body"]["elements"]
    if card.get("header", {}).get("icon", {}).get("token") != "wiki-bitable_colorful":
        raise ValueError("P3 failed: detailed card header icon mismatch")
    body = card.get("body", {})
    if body.get("direction") != "vertical" or body.get("vertical_spacing") != "12px":
        raise ValueError("P5 failed: detailed card spacing mismatch")
    metrics = next(
        (
            element
            for element in elements
            if element.get("element_id") == "daily_metrics"
        ),
        None,
    )
    if metrics is None or metrics.get("flex_mode") != "none":
        raise ValueError("P1 failed: detailed card metrics mismatch")
    table_count = sum(1 for element in elements if element.get("tag") == "table")
    if table_count > 5:
        raise ValueError("P3 failed: too many tables")
    last_tag = elements[-1].get("tag")
    if last_tag != "column_set":
        raise ValueError("P4 failed: buttons must be last")
    kv_rows = next(
        element
        for element in elements
        if element.get("element_id") == "metrics_table"
    )["rows"]
    tasks = [row["task"] for row in kv_rows]
    if tasks != TARGET_TASK_NAMES:
        raise ValueError("P7 failed: 标注平台 rows mismatch")
    if report["ranking"]:
        ranking_index = next(
            index
            for index, element in enumerate(elements)
            if element.get("element_id") == "daily_ranking"
        )
        detail_title_index = next(
            index
            for index, element in enumerate(elements)
            if element.get("element_id") == "section_detail"
        )
        if ranking_index > detail_title_index:
            raise ValueError("P6 failed: ranking must appear before detail section")


def validate_detailed_card(card: dict[str, Any], report: dict[str, Any]) -> None:
    validate_common_card(card, expected_chart_count=1)
    elements = card["body"]["elements"]
    table_count = sum(1 for element in elements if element.get("tag") == "table")
    if table_count > 5:
        raise ValueError("P3 failed: too many tables")
    last_tag = elements[-1].get("tag")
    if last_tag != "column_set":
        raise ValueError("P4 failed: buttons must be last")
    kv_rows = next(
        element
        for element in elements
        if element.get("element_id") == "metrics_table"
    )["rows"]
    tasks = [row["task"] for row in kv_rows]
    if tasks != TARGET_TASK_NAMES:
        raise ValueError("P7 failed: 标注平台 rows mismatch")
    if report["ranking"]:
        ranking_index = next(
            index
            for index, element in enumerate(elements)
            if element.get("tag") == "column_set"
            and element.get("margin") == "0px 0px 12px 0px"
        )
        detail_title_index = next(
            index
            for index, element in enumerate(elements)
            if element.get("tag") == "markdown"
            and "日报详细记录" in element.get("content", "")
        )
        if ranking_index > detail_title_index:
            raise ValueError("P6 failed: ranking must appear before detail section")


def validate_formal_card(card: dict[str, Any]) -> None:
    validate_common_card(card, expected_chart_count=1)
    rendered = json.dumps(card, ensure_ascii=False)
    banned_terms = [
        "未交名单",
        "低基线预警",
        "日报详细记录",
        "任务基线",
        "具体基线达成率",
        "正式版",
        "精简版",
        "预览",
        "实习生版",
    ]
    for term in banned_terms:
        if term in rendered:
            raise ValueError(f"P5 failed: formal card contains banned term {term}")
    if '"tag": "button"' in rendered:
        raise ValueError("P4 failed: formal card must not contain buttons")


def validate_weekly_card(card: dict[str, Any], report: dict[str, Any]) -> None:
    validate_common_card(
        card, expected_chart_count=1 if report["task_input_distribution"] else 0
    )
    if card.get("header", {}).get("title", {}).get("content") != "数据标注团队周报":
        raise ValueError("P0 failed: weekly card title mismatch")
    rendered = json.dumps(card, ensure_ascii=False)
    for required in (
        "本周核心指标",
        "视频标注产能",
        "图片标注产能",
        "投入分布",
        "预计工期为达到目标所需投入人天",
        "个人平均值",
        "本周日报回顾",
    ):
        if required not in rendered:
            raise ValueError(f"P0 failed: weekly card missing {required}")
    if "老板" in rendered:
        raise ValueError("P0 failed: weekly card must not expose target audience")
    kv_table = next(
        element
        for element in card["body"]["elements"]
        if element.get("element_id") == "weekly_kv_table"
    )
    if len(kv_table["columns"]) != 4:
        raise ValueError("P3 failed: weekly 标注平台 table must have four columns")
    if "任务基线" in rendered:
        raise ValueError("P3 failed: weekly card must not show task baseline")
    if "weekly_eff_table" in rendered:
        raise ValueError("P3 failed: weekly efficiency must use top-three cards")
    section_titles = [
        element["text"]["content"]
        for element in card["body"]["elements"]
        if element.get("element_id", "").startswith("weekly_")
        and element.get("tag") == "div"
    ]
    expected_titles = [
        "01  本周核心指标",
        "02  投入分布",
        "03  标注平台 综合进度",
        "04  效能排行",
        "05  本周日报回顾",
    ]
    if section_titles[:5] != expected_titles:
        raise ValueError("P0 failed: weekly section order mismatch")


def post_card(webhook: str, card: dict[str, Any]) -> tuple[int, str]:
    body = json.dumps(
        {"msg_type": "interactive", "card": card}, ensure_ascii=False
    ).encode("utf-8")
    request = urllib.request.Request(
        webhook,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw)
                message = payload.get("msg") or payload.get("message") or raw
            except json.JSONDecodeError:
                message = raw
            return response.status, str(message)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
            message = payload.get("msg") or payload.get("message") or raw
        except json.JSONDecodeError:
            message = raw
        return exc.code, str(message)
    except urllib.error.URLError as exc:
        return 0, str(exc.reason)


def send_with_retries(
    card: dict[str, Any], account: str, service: str
) -> tuple[bool, int, str]:
    webhook = read_keychain_secret(account, service)
    try:
        last_status = 0
        last_message = ""
        for _ in range(3):
            status, message = post_card(webhook, card)
            last_status, last_message = status, message
            if 200 <= status < 300 and (
                "success" in message.lower() or "ok" in message.lower() or message == ""
            ):
                return True, status, message or "success"
            time.sleep(1)
        return False, last_status, last_message
    finally:
        webhook = ""


def summarize_ranking(report: dict[str, Any]) -> str:
    if not report["ranking"]:
        return "今日暂无数据标注排行"
    parts = []
    for index, item in enumerate(report["ranking"], start=1):
        parts.append(
            f"{index}.{item['submitter']} "
            f"{format_baseline_rate(item['baseline_rate'])} {item['task_name']}"
        )
    return "；".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--skip-sync", action="store_true")
    args = parser.parse_args()

    target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
    sync_result = (
        SyncResult(success=True, summary="同步成功", fall_reconciliation="外部已确认")
        if args.skip_sync
        else run_sync_script()
    )

    report = build_report_dataset(target_date, sync_result)
    daily_due = report["report_count"] > 0
    detail_ok = True
    detail_status = 0
    detail_message = "当日无日报，视为非工作日，已跳过"
    formal_ok = True
    formal_status = 0
    formal_message = detail_message
    if daily_due:
        detail = detailed_card(report)
        formal = formal_card(report)
        validate_detailed_card(detail, report)
        validate_formal_card(formal)

        detail_ok, detail_status, detail_message = send_with_retries(
            detail, DETAIL_WEBHOOK_ACCOUNT, DETAIL_WEBHOOK_SERVICE
        )
        formal_ok, formal_status, formal_message = send_with_retries(
            formal, FORMAL_WEBHOOK_ACCOUNT, FORMAL_WEBHOOK_SERVICE
        )

    weekly_due = target_date.weekday() == 3
    weekly_ok = True
    weekly_status = 0
    weekly_message = "非周四，已跳过"
    if weekly_due:
        weekly_report = build_weekly_report_dataset(target_date, sync_result)
        if weekly_report["records"]:
            weekly = weekly_card(weekly_report)
            validate_weekly_card(weekly, weekly_report)
            weekly_ok, weekly_status, weekly_message = send_with_retries(
                weekly, WEEKLY_WEBHOOK_ACCOUNT, WEEKLY_WEBHOOK_SERVICE
            )
        else:
            weekly_message = "本周无日报数据，已跳过"

    print(f"数据同步状态：{report['sync_result'].summary}")
    print(f"倒地来源对账：{report['sync_result'].fall_reconciliation}")
    print(f"已交人数：{report['submitted_count']}")
    print(f"日报记录数：{report['report_count']}")
    print(f"未提交人数：{report['unsubmitted_count']}")
    print(f"排行前三：{summarize_ranking(report)}")
    if daily_due:
        print(f"详细版 webhook 状态：HTTP {detail_status} - {detail_message}")
        print(f"正式精简版 webhook 状态：HTTP {formal_status} - {formal_message}")
    else:
        print(f"日报状态：{detail_message}")
    if weekly_due and weekly_status:
        print(f"周报 webhook 状态：HTTP {weekly_status} - {weekly_message}")
    else:
        print(f"周报状态：{weekly_message}")

    if detail_ok and formal_ok and weekly_ok:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
