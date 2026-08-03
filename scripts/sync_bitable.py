#!/usr/bin/env python3
"""
sync_bitable.py — 每小时从生产库查询待标注指标，更新到飞书多维表格

用法:
  python3 scripts/sync_bitable.py          # 正常执行
  python3 scripts/sync_bitable.py --dry-run # 只查询不写入

部署方式:
  macOS launchd 或 crontab 每小时执行一次
  服务器 crontab 需调整 MySQL 连接方式（去掉 SSH tunnel）
"""

import json
import os
import subprocess
import sys
import time

import pymysql

# ─── 配置 ────────────────────────────────────────────────
SSH_HOST = os.getenv("ANNOTATION_SSH_HOST", "")
SSH_USER = os.getenv("ANNOTATION_SSH_USER", "")
SSH_MYSQL_PORT = int(os.getenv("ANNOTATION_SSH_MYSQL_PORT", "3306"))
LOCAL_MYSQL_PORT = int(os.getenv("ANNOTATION_LOCAL_MYSQL_PORT", "44060"))
MYSQL_HOST = os.getenv("ANNOTATION_MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("ANNOTATION_MYSQL_PORT", "3306"))
USE_SSH_TUNNEL = os.getenv("ANNOTATION_USE_SSH_TUNNEL", "0") == "1"

MYSQL_USER = os.getenv("ANNOTATION_MYSQL_USER", "annotation_reader")
MYSQL_DATABASE = os.getenv("ANNOTATION_MYSQL_DATABASE", "annotation_db")
KEYCHAIN_ACCOUNT = os.getenv("ANNOTATION_KEYCHAIN_ACCOUNT", "")
KEYCHAIN_SERVICE = os.getenv("ANNOTATION_KEYCHAIN_SERVICE", "")

# 飞书多维表格
BASE_TOKEN = os.getenv("ANNOTATION_BASE_TOKEN", "")
TABLE_ID = os.getenv("ANNOTATION_METRICS_TABLE_ID", "")
LARK_CLI = os.getenv("LARK_CLI", "lark-cli")

# 记录 ID 映射：code → record_id
RECORD_MAP = json.loads(os.getenv("ANNOTATION_RECORD_MAP_JSON", "{}"))
METRIC_CODES = (
    "trail",
    "fallDetect_school",
    "fallDetect_external",
    "climbDetect",
    "smokeAlarm",
    "calling",
    "strongExercise",
)

# ─── SQL 查询 ─────────────────────────────────────────────

# 人员轨迹待标注量：带人体序列且状态为 pending 的轨迹条数。
SQL_TRAIL_SAMPLEABLE = """
SELECT COUNT(*) AS pending_trajectory_count
FROM trace t
JOIN person p ON p.id = t.person_id
JOIN organizations o ON o.id = p.org_id
WHERE o.security_enabled = 1
  AND t.has_body_series = 1
  AND t.trail_validity = 'pending'
"""

SQL_TRAIL_ANNOTATION_METRICS = """
SELECT
  COUNT(*) AS annotated_count,
  SUM(t.trail_validity = 'valid') AS positive_count,
  SUM(
    t.trail_validity = 'valid'
    AND t.updated_at >= DATE_SUB(CURDATE(), INTERVAL WEEKDAY(CURDATE()) DAY)
    AND t.updated_at < DATE_ADD(CURDATE(), INTERVAL 1 DAY)
  ) AS weekly_positive_count,
  COUNT(DISTINCT CASE WHEN t.trail_validity = 'valid' THEN p.id END)
    AS positive_student_count,
  COUNT(DISTINCT CASE
    WHEN t.trail_validity = 'valid'
      AND t.updated_at >= DATE_SUB(CURDATE(), INTERVAL WEEKDAY(CURDATE()) DAY)
      AND t.updated_at < DATE_ADD(CURDATE(), INTERVAL 1 DAY)
    THEN p.id
  END) AS weekly_positive_student_count
FROM trace t
JOIN person p ON p.id = t.person_id
JOIN organizations o ON o.id = p.org_id
WHERE o.security_enabled = 1
  AND t.has_body_series = 1
  AND t.trail_validity IN ('valid', 'invalid')
"""

SQL_TRAIL_UNCOVERED_PENDING_STUDENTS = """
SELECT COUNT(DISTINCT pending_trace.person_id) AS uncovered_pending_student_count
FROM trace pending_trace
JOIN person p ON p.id = pending_trace.person_id
JOIN organizations o ON o.id = p.org_id
WHERE o.security_enabled = 1
  AND pending_trace.has_body_series = 1
  AND pending_trace.trail_validity = 'pending'
  AND NOT EXISTS (
    SELECT 1
    FROM trace valid_trace
    WHERE valid_trace.person_id = pending_trace.person_id
      AND valid_trace.has_body_series = 1
      AND valid_trace.trail_validity = 'valid'
  )
"""

# 异常行为：各类型剩余待标注 = pending + NOT in annotation_task OR behavior_cleaning_task
SQL_BEHAVIOR_SAMPLEABLE = """
SELECT
  CASE
    WHEN abt.code = 'fallDetect' AND ab.event_source = 'EXTERNAL_SOURCE'
      THEN 'fallDetect_external'
    WHEN abt.code = 'fallDetect'
      AND (ab.event_source IS NULL OR ab.event_source <> 'EXTERNAL_SOURCE')
      THEN 'fallDetect_school'
    ELSE abt.code
  END AS behavior_code,
  abt.name AS behavior_name,
  COUNT(ab.id) AS sampleable_count
FROM abnormal_behavior ab
JOIN abnormal_behavior_type abt ON abt.id = ab.behavior_type_id
JOIN organizations o ON o.id = ab.org_id
WHERE o.security_enabled = 1
  AND ab.validity = 'pending'
  AND NOT EXISTS (
    SELECT 1 FROM annotation_task_item ati
    JOIN annotation_task_v2 at2 ON at2.id = ati.task_id
    WHERE at2.task_kind IN ('climb_detect', 'external', 'strong_exercise')
      AND ati.item_ref = ab.id
  )
  AND NOT EXISTS (
    SELECT 1 FROM behavior_cleaning_task_behavior bctb
    WHERE bctb.behavior_id = ab.id
  )
  AND abt.code IN ('fallDetect','climbDetect','smokeAlarm','calling','strongExercise')
GROUP BY behavior_code, abt.id, abt.code, abt.name
ORDER BY abt.sort_order
"""

SQL_BEHAVIOR_ANNOTATION_METRICS = """
SELECT
  CASE
    WHEN abt.code = 'fallDetect' AND ab.event_source = 'EXTERNAL_SOURCE'
      THEN 'fallDetect_external'
    WHEN abt.code = 'fallDetect'
      AND (ab.event_source IS NULL OR ab.event_source <> 'EXTERNAL_SOURCE')
      THEN 'fallDetect_school'
    ELSE abt.code
  END AS behavior_code,
  COUNT(*) AS annotated_count,
  SUM(ab.validity = 'valid') AS positive_count,
  SUM(
    ab.validity = 'valid'
      AND ab.updated_at >= DATE_SUB(CURDATE(), INTERVAL WEEKDAY(CURDATE()) DAY)
      AND ab.updated_at < DATE_ADD(CURDATE(), INTERVAL 1 DAY)
  ) AS weekly_positive_count
FROM abnormal_behavior ab
JOIN abnormal_behavior_type abt ON abt.id = ab.behavior_type_id
JOIN organizations o ON o.id = ab.org_id
WHERE o.security_enabled = 1
  AND ab.validity IN ('valid', 'invalid')
  AND abt.code IN ('fallDetect','climbDetect','smokeAlarm','calling','strongExercise')
GROUP BY behavior_code, abt.id, abt.code
ORDER BY abt.sort_order
"""

SQL_FALL_RECONCILIATION = """
SELECT
  COUNT(*) AS all_fall,
  SUM(ab.event_source = 'EXTERNAL_SOURCE') AS external_fall,
  SUM(ab.event_source IS NULL OR ab.event_source <> 'EXTERNAL_SOURCE') AS school_fall,
  SUM(
    ab.validity = 'pending'
    AND NOT EXISTS (
      SELECT 1 FROM annotation_task_item ati
      JOIN annotation_task_v2 at2 ON at2.id = ati.task_id
      WHERE at2.task_kind IN ('climb_detect', 'external', 'strong_exercise')
        AND ati.item_ref = ab.id
    )
    AND NOT EXISTS (
      SELECT 1 FROM behavior_cleaning_task_behavior bctb
      WHERE bctb.behavior_id = ab.id
    )
  ) AS sampleable_all,
  SUM(
    ab.event_source = 'EXTERNAL_SOURCE'
    AND ab.validity = 'pending'
    AND NOT EXISTS (
      SELECT 1 FROM annotation_task_item ati
      JOIN annotation_task_v2 at2 ON at2.id = ati.task_id
      WHERE at2.task_kind IN ('climb_detect', 'external', 'strong_exercise')
        AND ati.item_ref = ab.id
    )
    AND NOT EXISTS (
      SELECT 1 FROM behavior_cleaning_task_behavior bctb
      WHERE bctb.behavior_id = ab.id
    )
  ) AS sampleable_external,
  SUM(
    (ab.event_source IS NULL OR ab.event_source <> 'EXTERNAL_SOURCE')
    AND ab.validity = 'pending'
    AND NOT EXISTS (
      SELECT 1 FROM annotation_task_item ati
      JOIN annotation_task_v2 at2 ON at2.id = ati.task_id
      WHERE at2.task_kind IN ('climb_detect', 'external', 'strong_exercise')
        AND ati.item_ref = ab.id
    )
    AND NOT EXISTS (
      SELECT 1 FROM behavior_cleaning_task_behavior bctb
      WHERE bctb.behavior_id = ab.id
    )
  ) AS sampleable_school,
  SUM(ab.validity IN ('valid', 'invalid')) AS annotated_all,
  SUM(
    ab.event_source = 'EXTERNAL_SOURCE'
    AND ab.validity IN ('valid', 'invalid')
  ) AS annotated_external,
  SUM(
    (ab.event_source IS NULL OR ab.event_source <> 'EXTERNAL_SOURCE')
    AND ab.validity IN ('valid', 'invalid')
  ) AS annotated_school,
  SUM(ab.validity = 'valid') AS positive_all,
  SUM(ab.event_source = 'EXTERNAL_SOURCE' AND ab.validity = 'valid') AS positive_external,
  SUM(
    (ab.event_source IS NULL OR ab.event_source <> 'EXTERNAL_SOURCE')
    AND ab.validity = 'valid'
  ) AS positive_school,
  SUM(
    ab.validity = 'valid'
    AND ab.updated_at >= DATE_SUB(CURDATE(), INTERVAL WEEKDAY(CURDATE()) DAY)
    AND ab.updated_at < DATE_ADD(CURDATE(), INTERVAL 1 DAY)
  ) AS weekly_positive_all,
  SUM(
    ab.event_source = 'EXTERNAL_SOURCE'
    AND ab.validity = 'valid'
    AND ab.updated_at >= DATE_SUB(CURDATE(), INTERVAL WEEKDAY(CURDATE()) DAY)
    AND ab.updated_at < DATE_ADD(CURDATE(), INTERVAL 1 DAY)
  ) AS weekly_positive_external,
  SUM(
    (ab.event_source IS NULL OR ab.event_source <> 'EXTERNAL_SOURCE')
    AND ab.validity = 'valid'
    AND ab.updated_at >= DATE_SUB(CURDATE(), INTERVAL WEEKDAY(CURDATE()) DAY)
    AND ab.updated_at < DATE_ADD(CURDATE(), INTERVAL 1 DAY)
  ) AS weekly_positive_school,
  SUM(ab.event_source IS NULL) AS null_source,
  SUM(ab.event_source IS NOT NULL AND ab.event_source <> 'EXTERNAL_SOURCE')
    AS other_named_source
FROM abnormal_behavior ab
JOIN abnormal_behavior_type abt ON abt.id = ab.behavior_type_id
JOIN organizations o ON o.id = ab.org_id
WHERE o.security_enabled = 1
  AND abt.code = 'fallDetect'
"""


def validate_fall_reconciliation(reconciliation):
    """校验人员倒地两个来源与未拆分口径严格对账。"""
    checks = (
        ("总量", "all_fall", "external_fall", "school_fall"),
        ("待标注", "sampleable_all", "sampleable_external", "sampleable_school"),
        ("已标注", "annotated_all", "annotated_external", "annotated_school"),
        ("正样本", "positive_all", "positive_external", "positive_school"),
        (
            "本周正样本",
            "weekly_positive_all",
            "weekly_positive_external",
            "weekly_positive_school",
        ),
    )
    for label, total_key, external_key, school_key in checks:
        total = int(reconciliation.get(total_key) or 0)
        external = int(reconciliation.get(external_key) or 0)
        school = int(reconciliation.get(school_key) or 0)
        if total != external + school:
            raise RuntimeError(
                f"人员倒地{label}对账失败: 全部={total}, "
                f"外部数据源={external}, 学校={school}"
            )


# ─── SSH Tunnel ────────────────────────────────────────────

def load_db_password():
    """从进程环境或 macOS 钥匙串读取生产库密码。"""
    password = os.getenv("ANNOTATION_DB_PASSWORD")
    if password:
        return password

    if not KEYCHAIN_ACCOUNT or not KEYCHAIN_SERVICE:
        raise RuntimeError(
            "请设置 ANNOTATION_DB_PASSWORD，或配置 macOS 钥匙串账户与服务名"
        )

    result = subprocess.run(
        [
            "security",
            "find-generic-password",
            "-a",
            KEYCHAIN_ACCOUNT,
            "-s",
            KEYCHAIN_SERVICE,
            "-w",
        ],
        capture_output=True,
        text=True,
    )
    password = result.stdout.strip()
    if result.returncode != 0 or not password:
        raise RuntimeError(
            "未能从 macOS 钥匙串读取数据库凭据"
        )
    return password


def setup_ssh_tunnel():
    """建立 SSH 隧道，返回 tunnel 进程 PID"""
    if not USE_SSH_TUNNEL:
        print(f"✅ 已跳过 SSH tunnel，直接连接 {MYSQL_HOST}:{MYSQL_PORT}")
        return False

    if not SSH_HOST or not SSH_USER:
        raise RuntimeError(
            "启用 SSH 隧道时必须设置 ANNOTATION_SSH_HOST 和 ANNOTATION_SSH_USER"
        )

    # 先杀可能残留的旧 tunnel
    subprocess.run(
        ["pkill", "-f", f"-L {LOCAL_MYSQL_PORT}:127.0.0.1:{SSH_MYSQL_PORT}"],
        capture_output=True
    )
    time.sleep(2)

    cmd = [
        "ssh", "-o", "ExitOnForwardFailure=yes",
        "-f", "-N",
        "-L", f"{LOCAL_MYSQL_PORT}:127.0.0.1:{SSH_MYSQL_PORT}",
        f"{SSH_USER}@{SSH_HOST}"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    time.sleep(3)

    # 验证 tunnel 是否存活 — 用端口连接测试
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(3)
        sock.connect(("127.0.0.1", LOCAL_MYSQL_PORT))
        sock.close()
    except (socket.timeout, ConnectionRefusedError, OSError):
        print(f"⚠️  SSH tunnel 未启动成功: {result.stderr.strip()}")
        print(f"⚠️  将尝试直接连接 {MYSQL_HOST}:{MYSQL_PORT}")
        return False

    print(f"✅ SSH tunnel 已建立 (local:{LOCAL_MYSQL_PORT} → {SSH_HOST}:{SSH_MYSQL_PORT})")
    return True


def teardown_ssh_tunnel():
    """关闭 SSH 隧道"""
    subprocess.run(
        ["pkill", "-f", f"-L {LOCAL_MYSQL_PORT}:127.0.0.1:{SSH_MYSQL_PORT}"],
        capture_output=True
    )
    print("✅ SSH tunnel 已关闭")


# ─── 数据查询 ──────────────────────────────────────────────

def query_metrics(mysql_host, mysql_port, db_password):
    """查询生产库，返回各指标 dict"""
    conn = pymysql.connect(
        host=mysql_host,
        port=mysql_port,
        user=MYSQL_USER,
        password=db_password,
        database=MYSQL_DATABASE,
        read_timeout=60
    )

    metrics = {
        code: {
            "待标注": 0,
            "已标注": 0,
            "正样本": 0,
            "本周正样本": 0,
            "正样本覆盖人数": None,
            "本周正样本覆盖学生": None,
            "待标注数据中没有正样本的学生人数": None,
        }
        for code in METRIC_CODES
    }

    # 1. 人员轨迹：轨迹量与学生覆盖数分开统计
    with conn.cursor() as cur:
        print(
            "query_source: trace + person + organizations — "
            "security-enabled body-series pending trajectory count"
        )
        cur.execute(SQL_TRAIL_SAMPLEABLE)
        row = cur.fetchone()
        metrics["trail"]["待标注"] = int(row[0])

    with conn.cursor() as cur:
        print(
            "query_source: trace + person + organizations — "
            "valid/invalid annotation volume and positive-student coverage"
        )
        cur.execute(SQL_TRAIL_ANNOTATION_METRICS)
        row = cur.fetchone()
        metrics["trail"].update({
            "已标注": int(row[0] or 0),
            "正样本": int(row[1] or 0),
            "本周正样本": int(row[2] or 0),
            "正样本覆盖人数": int(row[3] or 0),
            "本周正样本覆盖学生": int(row[4] or 0),
        })

    with conn.cursor() as cur:
        print(
            "query_source: trace + person + organizations — "
            "pending students without any valid body-series trajectory"
        )
        cur.execute(SQL_TRAIL_UNCOVERED_PENDING_STUDENTS)
        row = cur.fetchone()
        metrics["trail"]["待标注数据中没有正样本的学生人数"] = int(
            row[0] or 0
        )

    # 2. 异常行为各类型剩余待标注
    with conn.cursor() as cur:
        print(
            "query_source: abnormal_behavior + abnormal_behavior_type + "
            "organizations — unassigned pending workload"
        )
        cur.execute(SQL_BEHAVIOR_SAMPLEABLE)
        rows = cur.fetchall()
        for row in rows:
            code = row[0]
            count = int(row[2])
            metrics[code]["待标注"] = count

    with conn.cursor() as cur:
        print(
            "query_source: abnormal_behavior + abnormal_behavior_type + "
            "organizations — valid/invalid annotation volume"
        )
        cur.execute(SQL_BEHAVIOR_ANNOTATION_METRICS)
        rows = cur.fetchall()
        for row in rows:
            code = row[0]
            metrics[code].update({
                "已标注": int(row[1] or 0),
                "正样本": int(row[2] or 0),
                "本周正样本": int(row[3] or 0),
            })

    with conn.cursor() as cur:
        print(
            "query_source: abnormal_behavior + abnormal_behavior_type + "
            "organizations — fallDetect source reconciliation"
        )
        cur.execute(SQL_FALL_RECONCILIATION)
        row = cur.fetchone()
        reconciliation = {
            description[0]: int(value or 0)
            for description, value in zip(cur.description, row)
        }
        validate_fall_reconciliation(reconciliation)
        print(
            "  人员倒地来源对账通过: "
            f"全部={reconciliation['all_fall']}, "
            f"外部数据源={reconciliation['external_fall']}, "
            f"学校={reconciliation['school_fall']}, "
            f"空来源={reconciliation['null_source']}, "
            f"其他来源={reconciliation['other_named_source']}"
        )

    conn.close()

    print("📊 查询结果:")
    for key, fields in metrics.items():
        name = {
            "trail": "人员轨迹",
            "fallDetect_school": "人员倒地-学校",
            "fallDetect_external": "人员倒地-外部数据源",
            "climbDetect": "攀高",
            "smokeAlarm": "吸烟",
            "calling": "打电话",
            "strongExercise": "剧烈运动",
        }.get(key, key)
        values = ", ".join(f"{field}={value}" for field, value in fields.items())
        print(f"  {name} ({key}): {values}")

    return metrics


# ─── 飞书多维表格写入 ──────────────────────────────────────

def update_bitable(metrics):
    """用 lark-cli 更新飞书多维表格"""
    if not BASE_TOKEN or not TABLE_ID or not RECORD_MAP:
        raise RuntimeError(
            "请设置 ANNOTATION_BASE_TOKEN、ANNOTATION_METRICS_TABLE_ID 和 "
            "ANNOTATION_RECORD_MAP_JSON"
        )
    success_count = 0
    fail_count = 0

    for code, fields in metrics.items():
        record_id = RECORD_MAP.get(code)
        if not record_id:
            print(f"⚠️  未找到 {code} 的 record_id，跳过")
            continue

        cmd = [
            LARK_CLI, "base", "+record-upsert",
            "--base-token", BASE_TOKEN,
            "--table-id", TABLE_ID,
            "--record-id", record_id,
            "--json", json.dumps(fields, ensure_ascii=False),
            "--as", "user"
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        try:
            resp = json.loads(result.stdout)
            if resp.get("ok"):
                values = ", ".join(f"{field}={value}" for field, value in fields.items())
                print(f"  ✅ {code}: {values} 已写入")
                success_count += 1
            else:
                print(f"  ❌ {code}: 写入失败 — {resp.get('error', {}).get('message', result.stderr)}")
                fail_count += 1
        except json.JSONDecodeError:
            print(f"  ❌ {code}: lark-cli 输出异常 — {result.stderr}")
            fail_count += 1

    print(f"\n写入汇总: ✅ {success_count} 成功, ❌ {fail_count} 失败")
    return fail_count == 0


# ─── 主流程 ──────────────────────────────────────────────

def main():
    dry_run = "--dry-run" in sys.argv

    print(f"{'[DRY RUN] ' if dry_run else ''}开始同步 — {time.strftime('%Y-%m-%d %H:%M:%S')}")

    try:
        db_password = load_db_password()
    except RuntimeError as exc:
        print(f"❌ 凭据读取失败: {exc}")
        sys.exit(1)

    # 1. 建立 SSH 隧道
    tunnel_started = setup_ssh_tunnel()
    if tunnel_started:
        conn_host = "127.0.0.1"
        conn_port = LOCAL_MYSQL_PORT
    else:
        conn_host = MYSQL_HOST
        conn_port = MYSQL_PORT

    # 2. 查询数据
    try:
        metrics = query_metrics(conn_host, conn_port, db_password)
    except Exception as e:
        print(f"❌ 数据查询失败: {e}")
        teardown_ssh_tunnel()
        sys.exit(1)

    # 3. 写入飞书
    if dry_run:
        print("\n[DRY RUN] 跳过飞书写入")
        teardown_ssh_tunnel()
        return

    try:
        ok = update_bitable(metrics)
    except Exception as e:
        print(f"❌ 飞书写入失败: {e}")
        ok = False

    # 4. 关闭 SSH 隧道
    teardown_ssh_tunnel()

    if ok:
        print(f"\n✅ 同步完成 — {time.strftime('%Y-%m-%d %H:%M:%S')}")
    else:
        print(f"\n⚠️  同步部分失败，请检查")
        sys.exit(1)


if __name__ == "__main__":
    main()
