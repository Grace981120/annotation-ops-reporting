# Annotation Ops Reporting

把“今天标了多少、库存还剩多少、质量是否达标、什么时候能完成”从人工追问，变成每天自动送达的飞书经营简报。

这是一个面向数据标注团队的轻量运营系统：MySQL 负责事实数据，飞书多维表格承载目标、人员和日报，脚本负责汇总计算，飞书群机器人负责把结论送到团队和管理者面前。

## 为什么需要它

很多标注团队已经有标注平台、数据库和日报，但管理信息仍然是割裂的：库存要问研发，个人产出要翻表格，质量与目标要手算，周报还要重复整理。

本项目解决四个持续发生的问题：

| 管理问题 | 系统给出的答案 |
| --- | --- |
| 今天团队有没有正常运转？ | 已提交人数、未提交人员、个人产出与投入 |
| 当前任务是否健康？ | 待标注、已标注、正样本、正样本率、本周增量 |
| 谁或哪个任务需要关注？ | 效率排行、低于基线提醒、任务投入分布 |
| 目标什么时候能完成？ | 预计正样本、达到目标与清空库存所需人天 |

它不是新的标注工具，而是位于标注平台之上的“运营驾驶舱”。

## 你会得到什么

### 一套数据底座

配套多维表格包含四张表：

- **项目任务**：任务、产能基线、单位与分类
- **标注日报**：提交人、当日产出、投入时间和基线达成率
- **团队人员**：在岗成员与日报提交范围
- **标注指标**：库存、累计产出、正样本、覆盖人数和目标预测

完整字段见 [多维表格模板结构](docs/base-schema.md)。

### 三类自动报告

- **团队详细日报**：提交情况、个人排行、低基线提醒、投入分布和任务全景
- **管理精简日报**：保留核心进展与风险，适合管理群快速阅读
- **周报**：汇总周五至次周周四的投入、产出、效率、目标差距和每日回顾

### 一条自动化链路

```text
只读 MySQL
  -> 指标汇总与口径对账
  -> 飞书多维表格
  -> 日报 / 管理摘要 / 周报卡片
  -> 飞书群机器人
```

## 适合谁

- 5～100 人的数据标注或数据运营团队
- 已经有 MySQL 数据源，并用飞书协作的团队
- 希望保留自己的数据口径，不想引入重型 BI 平台的团队
- 需要同时关注数量、正样本质量、人员投入和交付目标的项目

如果你的业务表结构不同，可以保留报表和多维表格层，只替换 `sync_bitable.py` 中的 SQL 适配器。

## 5 分钟了解如何使用

### 1. 准备运行环境

需要 Python 3.11+、一个 MySQL 只读账号、已登录的 `lark-cli`，以及飞书群机器人 webhook。

```bash
git clone https://github.com/Grace981120/annotation-ops-reporting.git
cd annotation-ops-reporting
python -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'
cp .env.example .env
```

填写 `.env` 后，将配置加载到当前 shell：

```bash
set -a
source .env
set +a
```

### 2. 准备多维表格

在自己的飞书空间创建一个 Base，按 [多维表格模板结构](docs/base-schema.md) 建立四张表。公开演示请使用“商品图片分类、道路目标框选”等通用 mock 任务，以及“标注员甲/乙/丙”等模拟姓名，不要导入真实人员、客户名称或历史日报。

然后把 Base token、四张表的 table ID，以及“标注指标”各行的 record ID 写入本地 `.env`。这些值属于部署配置，不能提交到 GitHub。

### 3. 配置数据源和消息出口

在 `.env` 中填写：

- `ANNOTATION_MYSQL_*`：只读数据库连接
- `ANNOTATION_BASE_TOKEN` 与四个 `*_TABLE_ID`
- `ANNOTATION_TASK_NAMES_JSON`：报表中展示的任务及顺序
- `ANNOTATION_RECORD_MAP_JSON`：指标 code 到记录 ID 的映射
- `ANNOTATION_*_WEBHOOK_URL`：详细日报、管理摘要、周报的机器人地址

macOS 也可以把数据库密码和 webhook 放进 Keychain。SSH 隧道默认关闭，启用时推荐密钥认证。

### 4. 先试跑，再发送

只查询数据库，不写多维表格：

```bash
python scripts/sync_bitable.py --dry-run
```

确认统计口径后同步指标：

```bash
python scripts/sync_bitable.py
```

发送指定日期日报；周四会额外发送周报：

```bash
python scripts/send_annotation_team_daily.py --date 2026-08-03
```

如果指标已由其他任务刷新：

```bash
python scripts/send_annotation_team_daily.py --skip-sync
```

### 5. 放到定时任务中

建议每小时同步一次指标，在下班后发送日报。可使用 cron、launchd、systemd timer 或 CI scheduler；秘密应放在调度器的 secret store 中。

```cron
0 * * * * cd /path/to/repo && set -a && . ./.env && set +a && .venv/bin/python scripts/sync_bitable.py
30 18 * * 1-5 cd /path/to/repo && set -a && . ./.env && set +a && .venv/bin/python scripts/send_annotation_team_daily.py --skip-sync
```

## 数据口径

参考实现展示待标注、已标注、正样本、本周正样本和覆盖数量等运营指标。底层 SQL 仅作为查询适配器，公开报表中的任务名称通过环境变量配置，不需要暴露内部项目名称。

SQL 依赖示例业务表结构，接入自己的平台时需要修改查询，但报表数据集、卡片构建、重试与校验逻辑可以复用。

## 运维与安全

- 数据库账号必须只读
- 真实 `.env`、日志、数据库导出和报告载荷不得提交
- Base token、table ID、record ID 和 webhook 仅存于环境变量或本地秘密存储
- SSH 隧道使用密钥认证并校验服务器主机密钥
- 先运行 `--dry-run` 核对 SQL 口径，再允许写入飞书
- 卡片发送包含结构校验和失败重试，但仍应监控定时任务退出码

详见 [Security Policy](SECURITY.md)。

## 测试

```bash
pytest
```

测试使用内存 SQLite、模拟命令和虚构人员，不连接真实数据库或飞书资源。

## 当前边界

- 这是可自托管的参考实现，不是托管 SaaS
- 多维表格 schema 已文档化，但每个部署者必须在自己的飞书空间创建副本
- 数据库 SQL 是示例适配器，不承诺兼容其他标注平台的表结构
- 目前通过飞书自定义机器人发送卡片，不包含独立 Web 管理后台

## License

[MIT](LICENSE)
