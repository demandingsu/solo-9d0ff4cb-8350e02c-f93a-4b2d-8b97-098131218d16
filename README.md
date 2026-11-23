# 培训供数与更正 API

培训报名表和测评表按课题分批交付。源数据由培训方提供，每行带**表名**、**源行号**和内部判断的**归并学员号**；交付给课题组时改用**课题专用匿名行号**和**学员号**。同一学员在不同表中拿到同一个学员号；同一行在课题内的行号一旦分配就不变。发现两人被误并为一人时，把要拆出的行单独指定，拆出的行统一换新学员号，其余行保留原号。按成功登记的交付记录给课题组出更正单，凭原匿名行号标明新号，更正单可原样重新获取。

培训方重新排版报名表时，用**换版**登记新版号与旧行号→新行号对照。换版后交付可在明细上带版号，由服务按已发布对照回溯到原始报名行；同一源行始终沿用原来的课题匿名行号和当前学员号，行号在不同版本之间可以复用。

## 运行

```bash
docker compose up --build
```

- 交互文档：<http://localhost:8000/docs>
- OpenAPI: <http://localhost:8000/openapi.json>
- 数据卷 `app-data` 挂到容器 `/data`，SQLite 文件为 `/data/app.db`。

本地不装 Docker 时：

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
APP_DB_PATH=./data/app.db .venv/bin/python -m uvicorn app.main:app --port 8000
```

## 在 /docs 里演示

1. `POST /demo/seed` — 生成一套演示数据（可重复调用，不会重复写入）。
2. `GET /projects/{project_id}/rows` — 看课题行号、学员号、拆分标记。
3. `GET /projects/{project_id}/deliveries` — 看每次交付的匿名行号与学员号。
4. `GET /projects/{project_id}/corrections` — 看全课题更正单；
   `?split_id=1` 只看某次拆分，`?delivery_id=3` 只看某次交付涉及的更正行。
5. `GET /registration/versions` — 看报名版号链、当前生效版号与每行的旧/新行号对照。

### 行号互换演练（演示数据已内置）

演示剧本里报名表先从 `v2026-1` 换到 `v2026-2`，第 1、2 行**互换行号**（1→2、2→1），
随后又换到 `v2026-3` 重新编号（31–34）。换版后的交付 `demo-delivery-v2` 用新版号提交：

```json
POST /projects/{id}/deliveries
{
  "request_id": "manual-v2",
  "items": [
    {"table_name": "registration", "row_no": 2, "version": "v2026-2"},
    {"table_name": "registration", "row_no": 1, "version": "v2026-2"}
  ]
}
```

`v2026-2` 的第 2 行回溯到源行 1，第 1 行回溯到源行 2，所以返回的仍是源行各自原来的
课题匿名行号与学员号（不会新占编号）。即使已经换到 `v2026-3`，`v2026-2` 的已发布对照
照样供数。换版后才补登的报名行（演示中的第 5 行）没有任何对照，交付时省略 `version`，
按原登记行号交付。

### 并发换版演练

开两个终端同时向同一个当前版号发起换版（或用 `tests/test_concurrency.py` 里的
`test_parallel_version_switches_only_one_succeeds`）：两个事务都以 `BEGIN IMMEDIATE`
抢写锁，只有一个换版成功并成为当前版，另一个收到 409 `version_not_active`，且不会留下
半成品对照；库里始终只有一个 `is_active = 1` 的版号（部分唯一索引兜底）。

演示数据覆盖：跨表共享学员号；S002 在拆分前、拆分后都有交付；S002 中未拆出的行保留原号；S003 的拆分行从未交付，因此更正单为空；报名行号互换、链式换版、跨版交付与补登行交付。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/source-rows` | 登记/更正源明细（表名、行号、归并学员号） |
| GET | `/source-rows` | 查看源明细 |
| POST | `/projects` | 创建课题组 |
| POST | `/projects/{id}/deliveries` | 按课题交付指定行，返回匿名行号与学员号 |
| GET | `/projects/{id}/deliveries[/{delivery_id}]` | 重取交付记录 |
| GET | `/projects/{id}/rows` | 课题行号、学员号、拆分/交付状态 |
| POST | `/projects/{id}/splits` | 拆分误并学员 |
| GET | `/projects/{id}/splits[/{split_id}]` | 重取拆分记录 |
| GET | `/projects/{id}/corrections` | 更正单（可按 `split_id` / `delivery_id` 过滤） |
| POST | `/registration/versions/switch` | 报名表换版：当前版号、新版号、旧→新行号对照 |
| GET | `/registration/versions` | 列出已发布版号对照与当前生效版号 |
| POST | `/demo/seed` | 演示数据 |
| GET | `/health` | 健康检查 |

### 交付

```json
POST /projects/{id}/deliveries
{
  "request_id": "batch-2026-05-01-1",
  "items": [
    {"table_name": "registration", "row_no": 2},
    {"table_name": "assessment",   "row_no": 11}
  ]
}
```

`merged_student_no` 可省略；若填写则必须与源登记一致，否则返回 409。行不存在返回 404，同批重复行返回 422，整批不会写入。`request_id` 用于幂等：同 id 同内容返回原记录，同 id 不同内容返回 409。

报名明细可额外带 `version`：

```json
{"table_name": "registration", "row_no": 21, "version": "v2026-2"}
```

省略 `version` 时按原始登记行号解释（换版后才补登的行只能这样交付）；带 `version` 时
按该版已发布对照把行号回溯到源行，再走与普通交付完全相同的编号逻辑。因此同一源行无论
用哪个版本的行号交付，拿到的都是同一个课题匿名行号和当前学员号，拆分更正单也仍按实际
交付记录出具。版号只能用于 `registration` 表，否则 422；未知版号 404 `unknown_version`，
版号内行号不存在 404 `unknown_version_row`。同批折算后若指向同一源行（含跨版本、带不带
版号混用），按重复行 422 整批拒绝。

### 报名换版

```json
POST /registration/versions/switch
{
  "current_version": "v2026-1",
  "new_version": "v2026-2",
  "mappings": [
    {"old_row_no": 1, "new_row_no": 2},
    {"old_row_no": 2, "new_row_no": 1},
    {"old_row_no": 3, "new_row_no": 3},
    {"old_row_no": 4, "new_row_no": 4}
  ]
}
```

- 第一次换版以**当时库中登记的报名行**为准：对照的旧行号集合必须与库中报名行完全一致。
- 后续换版的对照必须覆盖**当前版全部行**；新旧两列行号在一次对照内各自唯一。
- 换版是追加式记录：已发布对照永久保留、继续供数；版号一旦发布不得复用（409
  `version_no_reused`）。
- `current_version` 不是当前生效版号时拒绝换版（409 `version_not_active`）；对照不完整
  或行号重复返回 422。任何校验或保存失败都整体回滚，当前版与已发布对照不变。
- 行号在不同版本间可以复用；服务沿版号链把每个新行号一直追溯到最初的源行号
  （`GET /registration/versions` 返回的 `source_row_no`）。

### 原单重发

换版前后的交付都可以用原 `request_id` 把**原请求体原样重发**，幂等指纹包含 `version`，
所以取回的是当时的回执（含匿名行号、学员号）。同一 `request_id` 改用别的版号或别的行号
属于内容冲突，返回 409 `request_id_conflict`。

### 拆分

```json
POST /projects/{id}/splits
{
  "merged_student_no": "S002",
  "rows": [
    {"table_name": "assessment", "row_no": 11},
    {"original_row_no": 4}
  ]
}
```

行可以用源坐标 `table_name + row_no` 指定，也可以用更正单上的原匿名行号 `original_row_no` 指定（两种不能混用在同一项上）。拆分行的归并学员号可由服务推断；跨多个归并号时必须显式指定。所有被拆行必须当前属于同一学员号，否则返回 409 `row_not_in_student` / `rows_span_multiple_students`。

### 更正单

每行包含：`table_name`、`original_row_no`（原匿名行号）、`old_student_no`、`new_student_no`、`delivery_id`、`split_id`。这些行是拆分成功时按已登记交付记录写下的，不在读取时现算，因此同一 `split_id`/`delivery_id` 的更正单多次获取结果完全一致（含 `digest_sha256`）。

## 实现要点

- **编号**：`project_counters` 为每个课题维护行号、学员号两个自增序列。`project_rows` 以 `(project_id, table_name, row_no)` 为主键并唯一约束 `project_row_no`；`project_student_numbers` 对每个归并学员号唯一，拆分组以 `kind='split'` 另占一个号。
- **交付即登记**：每次交付把每行写入 `delivery_items`（含当时的学员号）。这是更正单的唯一事实来源。
- **拆分与更正同事务**：拆分先完成全部校验，再在同一 `BEGIN IMMEDIATE` 事务内分配新号、改行归属、给该行在旧号下的每条交付记录写一条更正行。任一步失败整体回滚，原归属和原更正单都不变。
- **交错交付**：更正行只针对"用旧号成功交付过"的记录，所以拆分前的交付一定在单上；拆分后的交付读到当前学员号，用新号，不产生更正行。
- **报名换版**：`registration_versions` 是版号链，部分唯一索引保证全库只有一个 `is_active=1` 的当前版，`version_no` 唯一所以版号无法复用；`registration_version_rows` 保存每次换版不可变的旧→新行号对照，并直接记下每行追溯到的 `source_row_no`，旧/新行号在同一版内各有唯一约束。换版与交付同在 `BEGIN IMMEDIATE` 事务内完成全部校验后才写入，失败整体回滚。
- **版号交付**：带版号的报名明细先按已发布对照解析为源行号再走交付，去重也按解析后的源坐标进行，因此跨版本指向同一源行的明细会被整批判重；源行与课题匿名行号/学员号的绑定不变，行号跨版本复用不影响编号。幂等指纹纳入 `version`，原单原样重发取回原回执，改版号即冲突。
- **并发**：所有写操作 `BEGIN IMMEDIATE`，SQLite WAL 模式，连接 30s busy timeout。并发交付不会重号，并发拆分同一组只有一次成功，并发换版同一当前版也只有一次成功。

## 实测

```bash
python -m pytest -q     # 37 passed
```

覆盖内容：

- 跨表同号、行号稳定、不同课题编号隔离；
- 未知行、重复行返回错误且无副作用；
- 拆分后未拆出的行保留原号；
- 交错交付（交付→交付→拆分→交付）的更正单内容与顺序；
- 未交付即拆分的更正单为空；
- 拆分失败（行已不属于该组）后归属与更正单不变；
- 用匿名行号寻址拆分；
- `request_id` 幂等与冲突；
- 更正单按 `split_id`、`delivery_id` 过滤，且后续交付/拆分不改变既有更正单；
- 同一行多次交付各生成一条更正行；
- 8 线程并发交付编号唯一；6 线程并发拆分同一组仅一次成功；失败事务回滚计数器；
- 首次换版锚定当时登记行、对照全覆盖与两列行号各自唯一；
- 版号复用、旧版号重提被拒，失败换版不留半成品；
- 链式换版的源行追溯、行号跨版本复用、已发布版换版后照常供数；
- 行号互换后版号交付沿用源行的匿名行号与当前学员号；
- 跨版/混用版号的同源行判重、未知版号与未知行整批拒绝；
- 换版后补登行省略版号按原行号交付；
- 换版前后原请求号原样重发取回原回执，改版号为内容冲突；
- 6 线程并发换版仅一次成功，换版与交付并发后编号仍一致。

## 余项

- 未做身份认证与按课题的数据权限；生产部署需要加一层鉴权。
- 不支持删除/撤销拆分、更正行或已发布的换版对照，只有追加式记录。
- 拆分只允许从"归并组"拆出；已拆出的组若要再拆，需要新的接口语义和更正链设计，当前返回 409。
- 换版只覆盖 `registration` 表；测评表若将来也需要版本化，可复用同一套版号链模型另建表。
- 分页为 offset 方式，超大课题可改用键集分页。
- 演示数据是固定剧本，仅用于 `/docs` 试用。
