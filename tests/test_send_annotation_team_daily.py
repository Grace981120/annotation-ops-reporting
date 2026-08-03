import importlib.util
import json
import sys
from datetime import date
from pathlib import Path
from unittest.mock import call, patch


def _load_daily_module():
    script_path = Path(__file__).parents[1] / "scripts" / "send_annotation_team_daily.py"
    spec = importlib.util.spec_from_file_location(
        "send_annotation_team_daily", script_path
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _record(
    *,
    submitter: str,
    task_name: str,
    baseline_rate: float,
    input_days: float,
    submitted_date: date,
    task_type: str = "数据标注",
):
    return {
        "submitter": submitter,
        "task_name": task_name,
        "baseline_rate": baseline_rate,
        "input_days": input_days,
        "submitted_date": submitted_date,
        "resolved_task_type": task_type,
    }


def _daily_report(daily, *, report_count: int):
    return {
        "sync_result": daily.SyncResult(True, "同步成功", "已对账"),
        "submitted_count": 0 if report_count == 0 else 1,
        "report_count": report_count,
        "unsubmitted_count": 0,
        "ranking": [],
    }


def _detailed_report(daily):
    return {
        "date": "2026-07-30",
        "sync_result": daily.SyncResult(True, "同步成功", "已对账"),
        "submitted_count": 3,
        "report_count": 4,
        "total_input_days": 1.0,
        "unsubmitted_count": 1,
        "unsubmitted_people": ["未提交同学"],
        "ranking": [
            {
                "submitter": "甲",
                "baseline_rate": 1.2,
                "task_name": "商品图片分类",
            },
            {
                "submitter": "乙",
                "baseline_rate": 1.0,
                "task_name": "道路目标框选",
            },
            {
                "submitter": "丙",
                "baseline_rate": 0.9,
                "task_name": "客服文本实体标注",
            },
        ],
        "low_baseline_records": [
            {
                "submitter": "丁",
                "task_name": "会议音频转写",
                "completed": 20,
                "resolved_unit": "个视频",
                "input_days": 1.0,
                "baseline_rate": 0.4,
            }
        ],
        "task_input_distribution": [
            {
                "name": "商品图片分类（2人参与）",
                "value": 2.0,
                "task_name": "商品图片分类",
            }
        ],
        "metrics_fallback_updated_at": "-",
        "metrics_records": [
            {
                "task_name": task_name,
                "current_numerator": 80,
                "target": 100,
                "current_progress": 0.8,
                "positive_rate": 0.5,
                "pending_display": 20,
                "predicted_positive": 90,
                "target_person_days": 14,
                "completion_person_days": 20,
                "baseline": 16,
                "baseline_unit": "个视频",
            }
            for task_name in daily.TARGET_TASK_NAMES
        ],
        "detailed_records_sorted": [
            {
                "submitter": "甲",
                "task_name": "商品图片分类",
                "resolved_task_type": "数据标注",
                "completed": 120,
                "resolved_unit": "人",
                "input_days": 1.0,
                "baseline_rate": 1.2,
            }
        ],
    }


def test_weekly_period_is_previous_friday_through_thursday():
    daily = _load_daily_module()

    assert daily.weekly_period(date(2026, 7, 30)) == (
        date(2026, 7, 24),
        date(2026, 7, 30),
    )


def test_working_staff_uses_public_template_field_name():
    daily = _load_daily_module()
    response = {
        "fields": ["人员", "模拟人员", "是否在岗"],
        "data": [[[{"name": "甲"}], "", "工作中"]],
    }

    with patch.object(daily, "run_lark_base", return_value=response) as run:
        assert daily.get_working_staff() == ["甲"]

    args = run.call_args.args[0]
    assert "人员" in args
    assert "人员 (人员 )" not in args


def test_working_staff_falls_back_to_mock_name():
    daily = _load_daily_module()
    response = {
        "fields": ["人员", "模拟人员", "是否在岗"],
        "data": [[[], "标注员甲", "工作中"]],
    }

    with patch.object(daily, "run_lark_base", return_value=response):
        assert daily.get_working_staff() == ["标注员甲"]


def test_progress_records_fall_back_to_mock_submitter():
    daily = _load_daily_module()
    fields = [
        "提交人",
        "模拟提交人",
        "关联任务",
        "任务类型",
        "当日完成量",
        "单位",
        "当日投入时间(天)",
        "基线达成率",
        "提交日期",
    ]
    response = {
        "fields": fields,
        "record_id_list": ["record_demo"],
        "data": [
            [
                [],
                "标注员甲",
                [],
                "数据标注",
                800,
                "张",
                1,
                1,
                "2026-08-03",
            ]
        ],
    }

    with patch.object(daily, "run_lark_base", return_value=response):
        records = daily.get_daily_records(date(2026, 8, 3))

    assert records[0]["submitters"] == ["标注员甲"]


def test_progress_range_uses_supported_exclusive_date_operators():
    daily = _load_daily_module()
    empty_result = {"fields": [], "record_id_list": [], "data": []}

    with patch.object(daily, "run_lark_base", return_value=empty_result) as run:
        daily.get_progress_records(date(2026, 7, 24), date(2026, 7, 30))

    args = run.call_args.args[0]
    filter_json = json.loads(args[args.index("--filter-json") + 1])
    assert filter_json["conditions"] == [
        ["提交日期", ">", "ExactDate(2026-07-23)"],
        ["提交日期", "<", "ExactDate(2026-07-31)"],
    ]


def test_weekly_efficiency_averages_each_person_data_annotation_records_only():
    daily = _load_daily_module()
    records = [
        _record(
            submitter="甲",
            task_name="轨迹",
            baseline_rate=0.6,
            input_days=0.5,
            submitted_date=date(2026, 7, 24),
        ),
        _record(
            submitter="甲",
            task_name="倒地",
            baseline_rate=1.0,
            input_days=1.0,
            submitted_date=date(2026, 7, 25),
        ),
        _record(
            submitter="乙",
            task_name="开发",
            baseline_rate=2.0,
            input_days=1.0,
            submitted_date=date(2026, 7, 25),
            task_type="开发",
        ),
    ]

    rows = daily.build_efficiency_rows(records)

    assert rows == [
        {
            "submitter": "甲",
            "tasks": ["倒地", "轨迹"],
            "record_count": 2,
            "input_days": 1.5,
            "average_rate": 0.8,
            "low_count": 1,
        }
    ]


def test_task_input_distribution_is_sorted_by_share_and_counts_unique_people():
    daily = _load_daily_module()
    records = [
        _record(
            submitter="甲",
            task_name="任务B",
            baseline_rate=1.0,
            input_days=0.5,
            submitted_date=date(2026, 7, 24),
        ),
        _record(
            submitter="甲",
            task_name="任务A",
            baseline_rate=1.0,
            input_days=1.0,
            submitted_date=date(2026, 7, 24),
        ),
        _record(
            submitter="乙",
            task_name="任务A",
            baseline_rate=1.0,
            input_days=1.0,
            submitted_date=date(2026, 7, 25),
        ),
    ]

    distribution = daily.build_task_input_distribution(records)

    assert distribution == [
        {"name": "任务A（2人参与）", "value": 2.0, "task_name": "任务A"},
        {"name": "任务B（1人参与）", "value": 0.5, "task_name": "任务B"},
    ]


def test_weekly_daily_reviews_include_thursday_end_date():
    daily = _load_daily_module()
    thursday = date(2026, 7, 30)
    records = [
        _record(
            submitter="甲",
            task_name="商品图片分类",
            baseline_rate=1.0,
            input_days=1.0,
            submitted_date=thursday,
        )
    ]

    reviews = daily.build_daily_review_summaries(records, date(2026, 7, 24), thursday)

    assert len(reviews) == 7
    assert reviews[-1]["date"] == thursday
    assert reviews[-1]["submitted_count"] == 1
    assert reviews[-1]["report_count"] == 1


def test_task_input_chart_uses_person_day_unit():
    daily = _load_daily_module()

    chart = daily.make_chart(
        [{"name": "任务A（1人参与）", "value": 1.0, "task_name": "任务A"}]
    )

    assert chart["chart_spec"]["title"]["text"] == "按任务投入分布（人天）"


def test_compact_person_days_uses_integer_or_one_decimal():
    daily = _load_daily_module()

    assert daily.format_person_days(35.0) == "35"
    assert daily.format_person_days(28.2) == "28.2"


def test_metrics_query_reads_estimated_person_days():
    daily = _load_daily_module()
    empty_result = {"fields": [], "record_id_list": [], "data": []}

    with patch.object(daily, "run_lark_base", return_value=empty_result) as run:
        daily.get_metrics_records()

    args = run.call_args.args[0]
    assert "达到目标还需天数" in args
    assert "标注完毕还需天数" in args


def test_prediction_uses_target_effort_when_positive_samples_can_reach_target():
    daily = _load_daily_module()
    item = {
        "predicted_positive": 120,
        "target": 100,
        "target_person_days": 14,
        "completion_person_days": 20,
    }

    assert daily.format_target_prediction(item) == (
        "预计工期：14人天\n预计正样本 120 / 100"
    )


def test_prediction_uses_completion_effort_when_positive_samples_fall_short():
    daily = _load_daily_module()
    item = {
        "predicted_positive": 90,
        "target": 100,
        "target_person_days": 14,
        "completion_person_days": 20,
    }

    assert daily.format_target_prediction(item) == (
        "预计工期：20人天\n预计正样本 90 / 100"
    )


def test_detailed_card_uses_skill_layout_and_preserves_report_details():
    daily = _load_daily_module()
    report = _detailed_report(daily)

    card = daily.optimized_detailed_card(report)
    daily.validate_optimized_detailed_card(card, report)
    production_card = daily.detailed_card(report)
    daily.validate_detailed_card(production_card, report)

    assert card["header"]["icon"]["token"] == "wiki-bitable_colorful"
    assert production_card["header"]["icon"]["token"] == "chart_colorful"
    assert production_card != card
    assert "corner_radius" not in json.dumps(card, ensure_ascii=False)
    assert card["body"]["direction"] == "vertical"
    assert card["body"]["vertical_spacing"] == "12px"
    assert card["body"]["padding"] == "12px 12px 20px 12px"

    elements = card["body"]["elements"]
    metrics = next(
        element for element in elements if element.get("element_id") == "daily_metrics"
    )
    assert metrics["flex_mode"] == "none"
    assert [column["background_style"] for column in metrics["columns"]] == [
        "blue-50",
        "violet-50",
        "purple-50",
        "green-50",
    ]
    assert all(
        column["elements"][0]["content"].startswith("## ")
        for column in metrics["columns"]
    )

    ranking = next(
        element for element in elements if element.get("element_id") == "daily_ranking"
    )
    assert ranking["flex_mode"] == "none"
    assert all(
        column["elements"][1]["text"]["text_size"] == "normal"
        for column in ranking["columns"]
    )
    assert "仅数据标注任务参与" in json.dumps(card, ensure_ascii=False)

    warning_table = next(
        element for element in elements if element.get("element_id") == "warning_table"
    )
    assert warning_table["columns"][-1]["data_type"] == "options"
    assert warning_table["rows"][0]["rate"] == [{"text": "40.0%", "color": "red"}]

    kv_table = next(
        element
        for element in elements
        if element.get("element_id") == "metrics_table"
    )
    assert [column["width"] for column in kv_table["columns"]] == [
        "16%",
        "20%",
        "20%",
        "28%",
        "16%",
    ]
    assert kv_table["rows"][0]["predict"] == (
        "预计工期：20人天\n预计正样本 90 / 100"
    )

    detail_table = next(
        element for element in elements if element.get("element_id") == "detail_table"
    )
    assert detail_table["columns"][2]["data_type"] == "options"
    assert detail_table["rows"][0]["task_type"] == [
        {"text": "数据标注", "color": "blue"}
    ]

    buttons = elements[-1]["columns"]
    assert [column["elements"][0]["type"] for column in buttons] == [
        "primary_filled",
        "default",
    ]


def test_optimized_detailed_card_allows_empty_input_distribution():
    daily = _load_daily_module()
    report = _detailed_report(daily)
    report["task_input_distribution"] = []

    card = daily.optimized_detailed_card(report)

    daily.validate_optimized_detailed_card(card, report)
    assert all(element.get("tag") != "chart" for element in card["body"]["elements"])


def test_person_trail_prediction_uses_bitable_value_directly():
    daily = _load_daily_module()
    record = {
        "task_name": "商品图片分类",
        "positive": 13123,
        "pending": 714,
        "positive_rate": 0.608,
        "predicted_positive": 99999,
    }

    prediction = daily.resolve_predicted_positive(record)

    assert prediction == 99999


def test_non_person_trail_prediction_keeps_bitable_value():
    daily = _load_daily_module()
    record = {
        "task_name": "道路目标框选",
        "positive": 100,
        "pending": 50,
        "positive_rate": 0.5,
        "predicted_positive": 321,
    }

    assert daily.resolve_predicted_positive(record) == 321


def test_weekly_summary_separates_video_and_image_capacity():
    daily = _load_daily_module()
    records = [
        {
            **_record(
                submitter="甲",
                task_name="视频任务",
                baseline_rate=1.0,
                input_days=1.0,
                submitted_date=date(2026, 7, 24),
            ),
            "completed": 100,
            "resolved_unit": "个视频",
        },
        {
            **_record(
                submitter="乙",
                task_name="图片任务",
                baseline_rate=1.0,
                input_days=0.5,
                submitted_date=date(2026, 7, 25),
            ),
            "completed": 80,
            "resolved_unit": "张图片",
        },
        {
            **_record(
                submitter="甲",
                task_name="开发任务",
                baseline_rate=1.0,
                input_days=0.25,
                submitted_date=date(2026, 7, 25),
                task_type="开发",
            ),
            "completed": 999,
            "resolved_unit": "个视频",
        },
    ]

    assert daily.build_weekly_summary(records) == {
        "task_count": 3,
        "video_capacity": 100.0,
        "image_capacity": 80.0,
        "submitter_count": 2,
        "total_input_days": 1.75,
    }


def test_main_skips_daily_cards_when_no_daily_reports():
    daily = _load_daily_module()
    report = _daily_report(daily, report_count=0)

    with (
        patch.object(sys, "argv", ["daily", "--date", "2026-07-29", "--skip-sync"]),
        patch.object(daily, "build_report_dataset", return_value=report),
        patch.object(daily, "detailed_card") as detailed_card,
        patch.object(daily, "formal_card") as formal_card,
        patch.object(daily, "send_with_retries") as send,
    ):
        result = daily.main()

    assert result == 0
    detailed_card.assert_not_called()
    formal_card.assert_not_called()
    send.assert_not_called()


def test_main_skips_thursday_weekly_card_when_period_has_no_reports():
    daily = _load_daily_module()
    report = _daily_report(daily, report_count=0)

    with (
        patch.object(sys, "argv", ["daily", "--date", "2026-07-30", "--skip-sync"]),
        patch.object(daily, "build_report_dataset", return_value=report),
        patch.object(
            daily, "build_weekly_report_dataset", return_value={"records": []}
        ),
        patch.object(daily, "weekly_card") as weekly_card,
        patch.object(daily, "send_with_retries") as send,
    ):
        result = daily.main()

    assert result == 0
    weekly_card.assert_not_called()
    send.assert_not_called()


def test_main_sends_weekly_when_period_has_reports_even_if_thursday_daily_is_empty():
    daily = _load_daily_module()
    report = _daily_report(daily, report_count=0)
    weekly_report = {"records": [{"record_id": "rec1"}]}
    weekly = {"schema": "2.0"}

    with (
        patch.object(sys, "argv", ["daily", "--date", "2026-07-30", "--skip-sync"]),
        patch.object(daily, "build_report_dataset", return_value=report),
        patch.object(daily, "build_weekly_report_dataset", return_value=weekly_report),
        patch.object(daily, "weekly_card", return_value=weekly),
        patch.object(daily, "validate_weekly_card"),
        patch.object(
            daily, "send_with_retries", return_value=(True, 200, "success")
        ) as send,
    ):
        result = daily.main()

    assert result == 0
    send.assert_called_once_with(
        weekly,
        daily.WEEKLY_WEBHOOK_ACCOUNT,
        daily.WEEKLY_WEBHOOK_SERVICE,
    )


def test_main_sends_weekly_after_both_daily_cards():
    daily = _load_daily_module()
    report = _daily_report(daily, report_count=1)
    detailed = {"schema": "2.0", "name": "detailed"}
    formal = {"schema": "2.0", "name": "formal"}
    weekly_report = {"records": [{"record_id": "rec1"}]}
    weekly = {"schema": "2.0", "name": "weekly"}

    with (
        patch.object(sys, "argv", ["daily", "--date", "2026-07-30", "--skip-sync"]),
        patch.object(daily, "build_report_dataset", return_value=report),
        patch.object(daily, "detailed_card", return_value=detailed),
        patch.object(daily, "formal_card", return_value=formal),
        patch.object(daily, "validate_detailed_card"),
        patch.object(daily, "validate_formal_card"),
        patch.object(daily, "build_weekly_report_dataset", return_value=weekly_report),
        patch.object(daily, "weekly_card", return_value=weekly),
        patch.object(daily, "validate_weekly_card"),
        patch.object(
            daily, "send_with_retries", return_value=(True, 200, "success")
        ) as send,
    ):
        result = daily.main()

    assert result == 0
    assert send.call_args_list == [
        call(detailed, daily.DETAIL_WEBHOOK_ACCOUNT, daily.DETAIL_WEBHOOK_SERVICE),
        call(formal, daily.FORMAL_WEBHOOK_ACCOUNT, daily.FORMAL_WEBHOOK_SERVICE),
        call(weekly, daily.WEEKLY_WEBHOOK_ACCOUNT, daily.WEEKLY_WEBHOOK_SERVICE),
    ]


def test_weekly_card_warns_on_shortfall_and_uses_local_collapsible_reviews():
    daily = _load_daily_module()
    report = {
        "start_date": date(2026, 7, 24),
        "end_date": date(2026, 7, 30),
        "sync_result": daily.SyncResult(True, "同步成功", "已对账"),
        "metrics_fallback_updated_at": "-",
        "summary": {
            "task_count": 12,
            "video_capacity": 5912,
            "image_capacity": 4616,
            "submitter_count": 8,
            "total_input_days": 28.2,
        },
        "metrics_records": [
            {
                "task_name": "商品图片分类",
                "current_numerator": 80,
                "target": 100,
                "current_progress": 0.8,
                "positive_rate": 0.5,
                "pending_display": 20,
                "predicted_positive": 90,
                "target_person_days": 14,
                "completion_person_days": 20,
                "baseline": 16,
                "baseline_unit": "人",
            }
        ],
        "task_input_distribution": [
            {"name": "商品图片分类（1人参与）", "value": 1.0, "task_name": "商品图片分类"}
        ],
        "efficiency_rows": [
            {
                "submitter": "甲",
                "tasks": ["商品图片分类"],
                "record_count": 1,
                "input_days": 1.0,
                "average_rate": 0.9,
                "low_count": 0,
            }
        ],
        "daily_reviews": [
            {
                "date": date(2026, 7, 24),
                "submitted_count": 1,
                "report_count": 1,
                "input_days": 1.0,
                "ranking": [
                    {"submitter": "甲", "baseline_rate": 0.9, "task_name": "商品图片分类"}
                ],
                "top_tasks": [
                    {
                        "name": "商品图片分类（1人参与）",
                        "value": 1.0,
                        "task_name": "商品图片分类",
                    }
                ],
            }
        ],
    }

    card = daily.weekly_card(report)
    daily.validate_weekly_card(card, report)
    rendered = daily.json.dumps(card, ensure_ascii=False)

    kv_table = next(
        element
        for element in card["body"]["elements"]
        if element.get("element_id") == "weekly_kv_table"
    )
    assert kv_table["rows"][0]["predict"] == (
        "预计工期：20人天\n预计正样本 90 / 100"
    )
    assert "预计工期为达到目标所需投入人天" in rendered
    assert "任务基线" not in rendered
    assert "视频标注产能" in rendered
    assert "图片标注产能" in rendered
    assert "商品图片分类 1.00人天" in rendered
    assert "投入分布（天）" not in rendered
    assert "weekly_eff_table" not in rendered
    assert [column["name"] for column in kv_table["columns"]] == [
        "task",
        "progress",
        "quality",
        "predict",
    ]
    assert [column["width"] for column in kv_table["columns"]] == [
        "18%",
        "20%",
        "20%",
        "42%",
    ]
    section_titles = [
        element["text"]["content"]
        for element in card["body"]["elements"]
        if element.get("element_id", "").startswith("weekly_")
        and element.get("tag") == "div"
    ]
    assert section_titles[:5] == [
        "01  本周核心指标",
        "02  投入分布",
        "03  标注平台 综合进度",
        "04  效能排行",
        "05  本周日报回顾",
    ]
    section_icons = [
        element["icon"]["token"]
        for element in card["body"]["elements"]
        if element.get("element_id") in {"weekly_core", "weekly_input"}
    ]
    assert section_icons == ["member_outlined", "info_outlined"]
    metric_sets = [
        element
        for element in card["body"]["elements"]
        if element.get("tag") == "column_set"
    ]
    assert [len(metric_sets[0]["columns"]), len(metric_sets[1]["columns"])] == [3, 2]
    ranking_name = next(
        element
        for column in metric_sets[2]["columns"]
        for element in column["elements"]
        if element.get("tag") == "div"
    )
    assert ranking_name["text"]["text_size"] == "normal"
    panels = [
        element
        for element in card["body"]["elements"]
        if element.get("tag") == "collapsible_panel"
    ]
    assert panels
    assert panels[0]["header"]["icon_expanded_angle"] == 180
    assert '"tag": "button"' not in rendered


def test_all_sent_cards_use_estimated_person_days_prediction():
    daily = _load_daily_module()
    report = _detailed_report(daily)

    detailed = daily.detailed_card(report)
    formal = daily.formal_card(report)

    detailed_kv = next(
        element
        for element in detailed["body"]["elements"]
        if element.get("element_id") == "metrics_table"
    )
    formal_kv = next(
        element
        for element in formal["body"]["elements"]
        if element.get("element_id") == "metrics_table_formal"
    )
    expected = "预计工期：20人天\n预计正样本 90 / 100"
    assert detailed_kv["rows"][0]["predict"] == expected
    assert formal_kv["rows"][0]["predict"] == expected


def test_weekly_total_input_days_uses_compact_format():
    daily = _load_daily_module()
    report = {
        "start_date": date(2026, 7, 24),
        "end_date": date(2026, 7, 30),
        "sync_result": daily.SyncResult(True, "同步成功", "已对账"),
        "metrics_fallback_updated_at": "-",
        "summary": {
            "task_count": 0,
            "video_capacity": 0,
            "image_capacity": 0,
            "submitter_count": 0,
            "total_input_days": 35.0,
        },
        "metrics_records": [],
        "task_input_distribution": [],
        "efficiency_rows": [],
        "daily_reviews": [],
    }

    card = daily.weekly_card(report)

    metrics = next(
        element
        for element in card["body"]["elements"]
        if element.get("element_id") == "weekly_secondary_metrics"
    )
    assert metrics["columns"][1]["elements"][0]["content"] == (
        "## <font color='violet'>35</font>"
    )
