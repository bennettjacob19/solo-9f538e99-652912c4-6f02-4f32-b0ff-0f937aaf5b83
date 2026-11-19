# 本地服务发现目录（svcdir）

一个运行在 Flask + SQLite 上的小型服务发现目录：运维人员可以在网页上修改
「服务名 → 目标地址」的映射、把互相调用的 2～10 个服务**成组原子切换**到新地址、
创建命名备份、并从一个备份恢复。另有 `/resolve` 查询 API 供客户端使用。

## 启动

### Docker Compose（推荐）

```bash
docker compose up --build
```

- 网页：<http://localhost:8000/>
- API：`GET http://localhost:8000/resolve?name=orders-api`
- 健康检查：`GET /healthz`
- SQLite 数据与备份都位于命名卷 `svcdir-data` 的 `/data/directory.sqlite3`
  （备份是同一数据库里的版本快照，所以备份与恢复天然原子）。

### 本地运行

```bash
python3 -m pip install -r requirements.txt
SVCDIR_DB=./data/directory.sqlite3 python3 -m svcdir   # 开发用内置服务器
# 或： gunicorn --bind 0.0.0.0:8000 --workers 1 --threads 8 svcdir.__main__:app
```

环境变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SVCDIR_DB` | `data/directory.sqlite3` | SQLite 文件路径 |
| `SVCDIR_RESTORE_LEASE_SECONDS` | `30` | 恢复租约时长（进程异常死亡后的兜底回收上限） |
| `PORT` | `8000` | 仅用于 `python -m svcdir` |

## 页面与 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 网页：目录、修改表单、成组切换、备份列表、恢复按钮 |
| GET | `/resolve?name=<服务名>` | 返回单个服务的地址 |
| GET | `/resolve` | 返回整个当前目录 |
| GET | `/api/state` | 当前版本、完整目录、恢复状态、备份列表 |
| GET | `/api/backups` | 备份列表 |
| POST | `/api/services` | `{"service": ..., "address": ..., "note": ...}` 新增/修改 |
| DELETE | `/api/services/<名>` | 删除 |
| POST | `/api/switch` | 成组原子切换（见下） |
| POST | `/api/backups` | `{"name": ...}` 创建命名备份 |
| GET | `/api/backups/<名>/validate` | 校验备份（不修改目录） |
| POST | `/api/backups/<名>/restore` | 从备份恢复 |

浏览器表单走 `POST /services`、`/switch`、`/backups`、`/backups/<名>/restore`，
成功后 303 跳回页面并显示结果或错误。

### 成组切换（原子发布）

互相调用的服务要一起换地址时，在网页勾选 2～10 个现有服务、填写各自的新地址、
所见目录版本和请求号（页面已按当前版本预填后两者），一次提交整组发布；
也可以用 API：

```json
POST /api/switch
{
  "request_id": "sw-2026-001",
  "seen_version": 6,
  "changes": [
    {"service": "orders-api", "address": "10.1.0.11:8080"},
    {"service": "billing-api", "address": "10.1.0.12:8080"}
  ]
}
```

成功响应（`replayed` 区分首次发布与重放）：

```json
{"ok": true, "request_id": "sw-2026-001", "version": 7, "previous_version": 6,
 "services": {"orders-api": "10.1.0.11:8080", "billing-api": "10.1.0.12:8080"},
 "replayed": false}
```

语义：

- **整组原子**：所有成员的新地址写进一个完整的新目录版本，一次提交发布；
  `/resolve` 单名查询与全目录查询只会看到切换前或切换后的完整组合，
  永远不会看到新旧混搭。失败（校验、冲突、进程崩溃）不会留下半组。
- **冲突检测**：提交后若组内任一服务在 `seen_version` 之后被修改、删除或
  经备份恢复——即使地址又改回相同值——整组返回 `409 switch_conflict`，
  目录不变；组外服务的任何改动不阻止发布。重新读取目录后用新的
  `seen_version` 重试即可。
- **幂等重试**：`request_id` 是幂等键。同号同内容重试（网络重试、双击、
  重启后重发）返回首次结果，即使目录后来又变过也不会重复发布；
  同号异内容返回 `409 request_id_conflict`。幂等记录与版本切换在同一个
  事务里落盘，重启后不丢。
- **与恢复交错**：恢复进行中新的切换返回 `409 restore_in_progress`；
  已完成的切换在恢复期间重放仍返回首次结果。恢复本身会把全目录标记为
  「已变更」，因此恢复之后基于旧 `seen_version` 的切换必然冲突。

查询响应示例：

```json
{"ok": true, "version": 6, "restore_in_progress": false,
 "name": "orders-api", "address": "10.0.0.12:8080"}
```

## 一致性与恢复语义

实现要点（对应需求逐条）：

- **备份是同一提交点的完整目录。** 目录从不原地修改：每次修改都写入一个
  *完整的新版本*（先复制当前版本的全部行，再改一行），最后一条
  `UPDATE meta SET value=<新版本>` 才是发布。备份行只固定「版本号」，因此
  创建备份期间持续写入不会让备份变成半个目录。
- **恢复前校验。** 恢复分为三步：①在一个写事务里登记「恢复中」租约；②只读
  事务里校验备份（条目数、每行服务名/地址合法性、重复服务名、SHA-256 摘要与
  备份记录比对）；③一个原子写事务创建完整的新版本并切换 `current_version`。
  校验失败 → 直接返回 422，目录一个字节都不动。
- **恢复期间查询仍返回当前完整目录。** `current_version` 只在最后一步切换，
  查询永远读它；整个查询在读事务快照里完成，因此不会读到混排的数据。
- **新修改明确拒绝，已受理的修改先结算。** 登记恢复意图需要写锁，所有已受理
  的修改必然先提交完成；之后到达的修改看到有效租约，返回 `409
  restore_in_progress`（网页也会明确提示）。
- **恢复后新查询只见备份地址；先前受理的查询不混用两版。** 切换后新查询用新
  版本；一个在切换前开始的查询持有自己的读快照（`store.read_view()`），
  即使内部重复读取也仍是同一版本。
- **切换中断后重启只呈现原版或完整备份版。** 新版本与版本号切换在同一事务内
  提交；崩在提交前 → 仍是原版，崩在提交后 → 是完整备份版，旧版本的行永不删除。
  恢复者用租约 + 操作系统文件锁（`<db>.restore.lock`）标记存活：进程死亡时
  内核释放锁，重启后立即回收被遗弃的「恢复中」标记，不必等租约到期。
- **并发恢复。** 文件锁 + 数据库内的 `restore_state` 双重互斥，第二个恢复请求
  得到 409。
- **成组切换的冲突检测基于「触碰版本」而非地址值。** `service_touches` 表记录
  每个服务最后一次被修改、删除、恢复或成组切换时的目录版本——即使新地址与旧
  地址相同也照记。切换请求携带 `seen_version`，组内任一服务的触碰版本大于它
  即整组冲突；恢复会触碰恢复前后的全部服务，所以恢复之后旧视角的切换必然
  冲突。组外服务的触碰不参与判断。
- **成组切换的幂等记录与发布同事务。** `group_requests` 表保存
  「请求号 → 内容指纹（seen_version + 全部成员地址的 SHA-256）+ 首次结果」，
  与新版本、`current_version` 切换在同一事务提交：崩溃要么都落盘要么都不，
  重启后同号重试只重放首次结果，不会重复发布；同号异内容直接 409。
  只有成功的发布才占用请求号，失败的尝试可修正后用同号重试。

## 测试

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest -q
```

关键交错场景覆盖在 `tests/test_svcdir.py`：

- `test_writes_rejected_while_restore_in_progress_and_settled_before`
- `test_query_accepted_before_switch_keeps_one_version`
- `test_concurrent_writers_during_restore_never_mix`
- `test_cross_process_readers_and_writer_during_restore`（独立进程 + WAL 快照）
- `test_corrupt_backup_is_rejected_and_directory_unchanged`
- `test_backup_with_missing_rows_is_rejected`
- `test_interrupted_switch_presents_original_or_complete_backup`
- `test_killed_restorer_leaves_original_and_restart_can_retry`（SIGKILL 恢复进程）
- `test_expired_restore_lease_is_reclaimed_after_restart`
- `test_dead_restorer_is_reclaimed_immediately_on_restart`
- `test_restore_can_run_after_prior_abandoned_marker`
- `test_live_restore_lease_survives_sibling_process_initialization`
- `test_backup_row_pins_one_version_during_continuous_writes`
- `test_http_rejects_writes_and_serves_reads_during_restore`
- 成组切换：`test_group_switch_publishes_one_complete_version`、
  `test_modified_member_conflicts_but_unrelated_changes_do_not`、
  `test_member_changed_back_to_same_address_still_conflicts`、
  `test_deleted_member_conflicts_even_when_recreated_identically`、
  `test_restore_after_seen_conflicts_even_with_identical_addresses`、
  `test_failed_switch_leaves_no_partial_group`、
  `test_same_request_replays_first_result_even_after_directory_changes`、
  `test_replay_returns_first_result_even_after_restore`、
  `test_same_request_id_with_different_content_rejected`、
  `test_request_record_survives_restart`、
  `test_concurrent_identical_requests_publish_exactly_once`、
  `test_crash_mid_switch_leaves_no_partial_group_and_retry_publishes_once`、
  `test_readers_see_only_complete_combinations_around_switch`、
  `test_read_view_opened_before_switch_keeps_old_combination`、
  `test_switch_rejected_during_restore_but_completed_request_replays`、
  `test_switch_interleaved_with_writes_and_restore`、
  `test_schema_v1_database_is_migrated`
- `test_api_flow` / `test_api_switch_flow` / `test_html_page_and_forms` /
  `test_html_group_switch_form` / `test_html_escapes_hostile_names`
- `test_api_validation_errors_are_json` / `test_edge_case_inputs_are_rejected_cleanly`

## 布局

```
svcdir/errors.py     错误类型 → HTTP 状态码
svcdir/store.py      目录版本、成组切换、命名备份、校验、恢复（核心逻辑）
svcdir/web.py        Flask 路由：网页 + API
svcdir/templates/    页面模板
tests/test_svcdir.py 自测（含并发交错）
Dockerfile / docker-compose.yml
```

数据库 schema 为 v2；打开 v1 的旧数据库文件时会自动迁移（新增
`service_touches` 与 `group_requests` 两表，并重放不可变版本历史回填各服务
的最后变更版本），目录数据与备份不受影响。

## 已知限制与未完成项

- **未在 Docker 中实际构建/运行。** 本环境没有 Docker（`docker` 命令不存在），
  因此 Dockerfile 与 compose 文件只做了静态检查。应用本身已用
  `python -m svcdir` 与 gunicorn 在真实 HTTP 上跑通（含成组切换、幂等重放、
  进程重启后不重复发布）。
- 单容器部署，恢复互斥依赖同一文件系统上的 `flock`；若把数据目录放到不支持
  `flock` 的网络文件系统，会退化为「租约到期才算失效」。
- 备份保存在同一 SQLite 数据库中，可防止误改目录，但不能防磁盘/文件损坏；
  需要异地容灾应另行导出备份文件。
- 未做用户认证与 HTTPS——按需求这是本地运维工具，请只在内网使用。
- 恢复的「已受理查询」保证依赖单个查询请求只在一个读事务里读完（当前实现如此）；
  没有为客户端提供跨多个独立 HTTP 请求的版本固定参数。
