# Annotation Ops Reporting

一套可自托管的数据标注运营报表工具：从 MySQL 汇总待标注量、已标注量、正样本与覆盖指标，同步到飞书多维表格，并向飞书群发送日报和周报卡片。

## 能力

- 定时查询标注库存、产出、正样本和本周增量
- 将指标 upsert 到飞书多维表格
- 汇总个人提交、任务投入、效率与目标预测
- 发送详细日报、管理摘要和周四周报
- 支持 dry-run、卡片结构校验、失败重试和指标对账
- 运行时配置与代码分离，不在仓库保存真实凭据或资源 ID

## 工作流

```text
MySQL (read-only)
  -> scripts/sync_bitable.py
  -> Feishu Base metrics table
  -> scripts/send_annotation_team_daily.py
  -> daily / management / weekly webhook cards
```

## 前置条件

- Python 3.11+
- `lark-cli`，并以用户身份完成飞书授权
- MySQL 只读账号
- 一个飞书多维表格，包含日报、人员、项目和指标数据表
- 三个飞书群机器人 webhook（可以指向同一个机器人）

安装：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'
cp .env.example .env
```

将 `.env` 中的占位值替换为自己的值，再加载环境变量。`.env` 已被忽略，不能提交。

## 多维表格配置

脚本按以下逻辑表读取数据：

| 环境变量 | 用途 |
| --- | --- |
| `ANNOTATION_DAILY_TABLE_ID` | 每日个人进度与投入 |
| `ANNOTATION_STAFF_TABLE_ID` | 在岗人员名单 |
| `ANNOTATION_PROJECT_TABLE_ID` | 项目目标、基准效率和任务类型 |
| `ANNOTATION_METRICS_TABLE_ID` | 各标注类型的库存与产出指标 |

`ANNOTATION_RECORD_MAP_JSON` 将指标 code 映射到指标表中的记录 ID。字段名和 SQL 体现了一个完整的参考实现；接入其他标注平台时，应在两个脚本的数据读取函数中适配自己的表名和字段名。

飞书 Base token、table ID、record ID 都属于部署配置，不应写进源码。仓库不会自动创建或复制包含真实运营数据的多维表格。

## 凭据

数据库密码优先读取 `ANNOTATION_DB_PASSWORD`。macOS 也可设置：

```bash
export ANNOTATION_KEYCHAIN_ACCOUNT=annotation-reporting
export ANNOTATION_KEYCHAIN_SERVICE=annotation-reporting-db
```

Webhook 可通过 `ANNOTATION_DETAIL_WEBHOOK_URL`、`ANNOTATION_FORMAL_WEBHOOK_URL`、`ANNOTATION_WEEKLY_WEBHOOK_URL` 提供。macOS 用户也可以改用对应的 `*_KEYCHAIN_ACCOUNT` 与 `*_KEYCHAIN_SERVICE`。

推荐使用密钥认证的 SSH 隧道；启用时设置 `ANNOTATION_USE_SSH_TUNNEL=1`、`ANNOTATION_SSH_HOST` 和 `ANNOTATION_SSH_USER`。

## 运行

只查询数据库，不写飞书：

```bash
python scripts/sync_bitable.py --dry-run
```

同步多维表格：

```bash
python scripts/sync_bitable.py
```

发送指定日期日报；周四会额外生成前一周周五至本周周四的周报：

```bash
python scripts/send_annotation_team_daily.py --date 2026-08-03
```

如果指标表已由其他任务刷新，可跳过数据库同步：

```bash
python scripts/send_annotation_team_daily.py --skip-sync
```

生产环境可用 cron、launchd、systemd timer 或 CI 定时任务调度。请把秘密放在调度器的 secret store 中。

## 测试

```bash
pytest
```

测试使用模拟命令、内存 SQLite 和示例记录，不连接生产数据库或真实飞书资源。

## 安全说明

公开前需确认仓库中没有 `.env`、日志、数据库导出、真实 webhook、飞书资源 ID、内网地址、姓名、邮箱或报告内容。详见 [SECURITY.md](SECURITY.md)。

## License

[MIT](LICENSE)

