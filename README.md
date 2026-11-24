# 装车秤单同期名次复核

Flask + SQLite 的港口装车秤单复核页。支持按日期上传/整日替换 CSV、按“日期 + 单号”暂扣或解除、在同一读事务中查询全部秤单与未暂扣秤单的总数和降序名次区间，并在页面显示小计键访问数。

地磅导出的 CSV 不含日期，选错日期会把同一批车计两遍。因此上传（或粘贴整日报）时会先做**同期重合预检**：核对所选日前后各 7 天的在用日报（跨年照查），命中则暂不入库，等质控员确认。

## 启动

### Docker Compose

```bash
docker compose up --build
```

浏览器打开：

- <http://127.0.0.1:8000/>
- 也映射了 <http://127.0.0.1:5000/> 到同一服务

SQLite 数据库保存在 Compose 命名卷 `scale-data` 的 `/data/scale.db`。

### 本地 Python

```bash
python3 -m pip install -r requirements.txt
DB_PATH=./data/scale.db python3 app.py
# 或
gunicorn --bind 0.0.0.0:8000 app:app
```

## 同期重合预检（写入前）

上传日期 `D` 时：

1. 窗口为 `D-7 … D+7`（含首尾，跨年自然跨年）。
2. 窗口内每个**已在库**的日报（含旧库历史数据）都与来稿比较：
   - 来稿与对方的票数各自都 **≥ 100**；
   - “单号 + 净重”都相同的票数占**来稿票数**的比例 ≥ 95%；
   - 同一票数占**对方票数**的比例也 ≥ 95%。
3. 任一日报满足条件即算命中。页面上列出**所有**命中日、两边票数、重合票数和两边比例，来稿**不入库**。
4. 无命中则直接整日替换入库；写入在一个 `BEGIN IMMEDIATE` 事务里完成，正是“写入前最后提交的那一份”生效。

相关规则：

- **暂扣不影响比对**：比对只看原单票号和净重，暂扣表不参与，也不改变命中结果。
- **旧库参与**：启动时用一条 `INSERT … SELECT` 为已有 `tickets` 日期补齐 `day_reports`（代数、票数），旧日报照常参与核对。
- **比对工作量不随无关票数增长**：候选日只看 `day_reports` 的票数元数据（小表），每个候选日再用按主键索引的 `EXISTS` 针对来稿票号计数。邻日中与来稿不重合的票不会被扫描，语句条数和 SQLite 虚拟机步数与这些票数无关（有测试用 `set_progress_handler` 验证）。
- **比对期间可继续上报**：比对在 WAL 读事务中执行；预检只先取一个很短的写事务登记提交序号。窗口外（甚至窗口内）的重报都能正常提交，不会被较慢的比对阻塞。

### 质控员确认

- 命中页 `GET /precheck/<id>` 显示命中明细，`POST /confirm` 提交确认。
- 确认时若**目标日或窗口内任一日报**在预检后有新报或换报（按每日期独立 generation 快照判断），返回 `409`/页面提示，**必须重新核对**才能确认；旧原稿和小计保持预检前状态。
- 确认写入的是质控员当时看到的那份票单（预检时已存下）。
- 预检记录在确认后保留为审计依据（`state=confirmed`），不删除。
- 窗口外的重报不会让预检过期。

### 提交顺序

每次上传在比对前取一个单调递增的 `submission_seq`。写入时在同一写锁内比较该日期已写入的最大序号：

- 后提交的上传/确认不会覆盖更晚提交且已写入的日报；
- 过期的上传或过期确认直接拒绝（`PrecheckStale` / `409`），不会写入；
- 并发同日重报最终只留后提交的那份，先提交但后到的被拒。

## CSV 格式

在页面选择日期后上传；该日期的旧秤单会被整个交易日的新文件替换，但暂扣记录不会删除。

支持表头：

```csv
单号,净重
A-1001,32000
A-1002,31500
A-1003,31500
```

也支持无表头两列：

```csv
A-1001,32000
A-1002,31500
```

规则：

- 单号在所选日期内唯一；同一 CSV 重复会整文件拒绝。
- 净重必须是整数，范围 `0` 到 `65535` 千克（含边界）。
- 支持 `utf-8-sig`、UTF-8 和中文 Excel 常见的 GB18030。
- 页面也可直接粘贴 CSV 文本。

示例文件：[`sample_tickets.csv`](sample_tickets.csv)。

## 暂扣与重报

- 暂扣表以 `(slip_date, ticket_no)` 为主键，不设指向秤单的外键。
- 整日替换秤单时不动暂扣表，因此：
  - 重报后暂扣仍有效；
  - 单号在一次或多次 CSV 中缺失，暂扣记录仍保留；
  - 该单以后重新出现时，会自动按暂扣状态排除出 `active` 口径。
- 目标单被暂扣时，“全部秤单”仍正常排名；“未暂扣秤单”显示区间总数和“暂扣”标记，名次单元格留空。

## 名次规则

给定区间 `[start, end]`（同年且包含单据日期，首尾均含）和目标净重 `w`：

- `higher = count(net_weight > w)`
- `equal = count(net_weight = w)`
- 名次区间为 `higher + 1` 到 `higher + equal`

例如两单同为 90 kg，且只有一单 100 kg，则 90 kg 的单共用第 `2-3` 名。

## 小计键与 576 上限

每个 `subtotals` 行只表示：

```text
一个日期规范段 × 一个二进重量档 × 一种口径(all/active) → ticket_count
```

实现使用两棵确定性的平衡/二分区间树：

- 日期：按当年实际天数（365/366）递归二分。任意年内区间最多覆盖 **15** 个互不相交的日期规范段（已用穷举测试验证所有起止日组合）。
- 重量：`0..65535` 是完整的 `2^16` 域。`> w` 的后缀最多拆成 **16** 个对齐二次幂重量档；另加目标重量叶子键 1 个、全重量根键（取总数）1 个，因此每个日期段、每种口径最多访问 `18` 个重量键。

总上限为：

```text
2 种口径 × 15 个日期段 × 18 个重量键 = 540 ≤ 576
```

若目标已暂扣，`active` 口径只取每段的总数根键，实际访问数通常更低。每次查询都用 `KeyMeter` 对每个请求的小计主键计数；数据库中不存在的空键也计一次，并在页面和 JSON 的 `meter.empty` 中显示。

目标原单通过 `tickets` 的 `(slip_date, ticket_no)` 主键索引读取；暂扣标记通过 `holds` 主键读取。所有名次计数均来自小计键，不扫描原单表。

## 一致性和失败处理

- SQLite 连接使用显式事务；嵌套写入用 savepoint。
- 上传替换、质控确认都在一个 `BEGIN IMMEDIATE` 事务内完成：回读当天旧原单、写回新原单、应用新旧小计差异、更新日期代数和写入顺序，然后提交。
- 暂扣/解除在一个事务内写暂扣记录并同步修改 `active` 小计。
- 任一步失败即回滚，旧秤单、暂扣记录、小计、预检状态仍保持上一已提交状态，查询可照常取数。
- 名次查询在一个读事务中读取目标原单、暂扣状态和全部小计键，因此看到的是同一个已提交快照。

## 主要路由

页面：

- `GET /`：上传、暂扣/解除、查询入口，以及待质控员核对的命中预检列表。
- `GET /precheck/<id>`：命中明细与确认按钮。
- `GET /day?date=YYYY-MM-DD`：回读当天原单和“原单缺失但仍有效”的暂扣。
- `GET /ticket?date=...&ticket_no=...`：只回读对应原单和暂扣状态。
- `GET|POST /query`：复核结果页。

表单/API：

- `POST /upload`，或 `POST /api/upload`（命中返回 `202` + `stored=false`）
- `POST /confirm`，或 `POST /api/confirm`（字段 `precheck_id`；过期返回 `409`）
- `GET /api/precheck`、`GET /api/prechecks`（可选 `?date=`）
- `POST /hold`、`POST /release`
- `GET|POST /api/rank`、`GET|POST /api/query`
- `GET /healthz`

JSON 示例：

```bash
curl 'http://127.0.0.1:8000/api/rank?date=2026-05-01&ticket_no=B&start=2026-01-01&end=2026-12-31'
```

返回中的 `result.all`、`result.active` 含 `total/higher/equal/rank_start/rank_end/rank_text`，`result.meter` 含 `accessed/empty/limit/within_limit/details`。

## 已做验证

在本环境中实际运行了：

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
python3 scripts/concurrency_check.py
```

结果：**47 个单元/集成测试全部通过**，覆盖：

- 原有：同重名次区间、暂扣、跨整日重报、CSV 校验、日期段 ≤15、重量档 ≤16、最坏 540 键、事务回滚、Flask 页面；
- 窗口：前后各 7 天、第 8 天不命中、跨年（2025-12-28 对 2026-01-02）；
- 门槛：双方各 ≥100 单、双方各自 ≥95%（95% 命中，94% 与“单侧 96%/另一侧 80%”不命中）、同号不同净重不命中；
- 命中列表：多个命中日都列出并带两边票数和重合数；
- 暂扣不影响比对；
- 旧库 `tickets` 在启动补齐后参与核对；
- 无关票数增长时比对工作量不变（`set_progress_handler` 计量 + `EXPLAIN QUERY PLAN` 走主键）；
- 命中不入库、无命中直接入库、确认后入库、重复确认拒绝；
- 目标日换报 / 窗口内换报使确认过期（`409`）、窗口外重报不影响、过期后重新预检可确认；
- 注入小计失败时确认整体回滚，预检记录仍为 pending，旧原单与小计不变；
- 提交顺序：后提交先写、先提交后到均正确处理；
- 真实 HTTP（Gunicorn + `entrypoint.sh`）验证了 `202/200/409` 流程。

`scripts/concurrency_check.py` 实测输出：

```text
outside_window_during_comparison: {'elapsed_seconds': ~0.005, 'outside_report_committed': True}
window_during_comparison: {'elapsed_seconds': ~0.005, 'window_report_committed': True,
                           'confirm_refused': True, 'stale_dates': ['2026-05-05']}
concurrent_same_day_reuploads: {'winner': 'T0', 'outcomes': [('T0', 3, 'committed'), ('T1', 2, 'refused')]}
expired_confirmation_is_refused: {'report': 'ok', 'confirm': 'stale', 'dates': ['2026-05-04']}
confirmation_before_window_report: {'report': 'ok', 'confirm': 'stale', 'dates': ['2026-05-04']}
```

说明：并发检查用注入的“暂停比对”钩子把竞争窗口固定下来，因此结果可重复；实际 Gunicorn 双 worker 下也用真实 HTTP 请求跑过窗口外上报与确认并发。

## 未完成 / 未在本环境验证的事项

- 当前环境没有 `docker` / `docker compose` 可执行文件，因此未能真正执行 `docker compose up --build`；已用 PyYAML 校验 `compose.yaml` 与 `docker-compose.yml` 等价，并在本机用 `entrypoint.sh` 的等价 Gunicorn 命令启动、跑通 HTTP 流程。镜像构建需在有 Docker 的环境最终确认。
- “提交顺序”使用数据库内单调计数器，不依赖客户端时钟，因此可比较同一服务内任意并发提交；不跨多个独立数据库实例（本应用为单库部署）。
- 未加入登录鉴权；需求未要求，当前定位为港口内网复核工具。
- 未做浏览器视觉回归；页面行为通过 Flask 测试客户端和真实 HTTP 请求验证。
