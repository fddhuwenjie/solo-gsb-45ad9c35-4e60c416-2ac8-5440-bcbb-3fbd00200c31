# AIS NMEA 0183 报文解码 API

基于 **Python 3.11+ / FastAPI / Pydantic v2** 的 AIS（ITU-R M.1371）解码服务。
`POST /decode` 接收同一批次、带 `source_id` 的原始 `!AIVDM` / `!AIVDO` 语句，
完成校验、乱序分片重组、歧义枚举与六位 payload 解码。
`POST /ingest` 在此基础上携带本批 UTC 观测时间 `observed_at`，把成功解码的消息
按 `(source_id, MMSI)` 持久化为可查询的船舶快照（SQLite），并提供 `GET /vessels` 查询；
每条成功解码的船位（消息 1/2/3）还会追加到**可按时间回看的船位轨迹**，由 `GET /tracks`
按 `observed_at` 升序回放。
对无 `sequence_id` 的歧义批，`POST /reviews` 可建立**待核定批**（保存原批与各歧义池全部候选，
不写快照），由人工核定后经 `POST /reviews/{batch_id}/resolve` 把选定候选连同本批明确解码结果
原子写入快照；`GET /reviews` / `GET /reviews/{batch_id}` 随时查询待核定批、候选与核定结果。
接收站发现已入库船位为误报时，`POST /tracks/invalidations` 可按 `source_id`、MMSI、
`observed_at` 把该轨迹点标记为无效：`/tracks` 随即只回放有效点，`/vessels` 船位快照退至
同源同船最新有效点（已无有效点则移除船位侧，静态航次保留），且后续同时间观测不会复活该点。

## 功能

- **逐句校验**（校验失败不影响同批其他语句，逐条返回错误）：
  - 帧结构（`!` 前缀、`*HH` 校验和字段）
  - **XOR 校验和**（`!` 与 `*` 之间所有字符异或）
  - **分片总数 / 分片序号**（1–9，序号 ≤ 总数）
  - **fill bits**（0–5；非末分片必须为 0，保证六位边界对齐）
  - 频道（`A`/`B`，兼容 `1`/`2` 自动归一化）、六位字符集合法性
- **乱序归组重组**：
  - 带非空 `sequence_id` 的多片语句按 `(source_id, channel, sequence_id, 分片总数)` 归组，按分片序号重组；
  - `sequence_id` 为空的孤儿分片按 `(source_id, channel, 分片总数)` 入池，**枚举不复用语句的完整组合**：
    唯一 → 解码；多解 → **HTTP 409** 并在 `candidates` 中并列全部候选；缺片 → 返回 `missing_fragments` 缺失序号；
  - 同批完全相同（同 `source_id` 同原文）的重发语句自动去重。
- **六位 payload 解码**：
  - 消息 **1/2/3**（A 类船位报告）：MMSI、经纬度、航速（SOG）、航向（COG/船首向）、转向率、航行状态、时间戳等；
  - 消息 **5**（静态与航次数据）：船名、呼号、IMO、船型、尺度、ETA、吃水、目的地；
  - 标准“不可用”哨兵值一律输出为 `null`（见下表）；
  - **位长校验**：消息 1/2/3 必须恰好 168 bit、消息 5 必须恰好 424 bit，不符即拒绝；
    不支持的消息类型返回 `unsupported message type` 错误。
- **船舶快照（`/ingest` + `/vessels`）**：
  - 按 `(source_id, MMSI)` 分别保存**最新船位消息 1/2/3** 与**最新静态航次消息 5** 两条独立时间线；
  - 每类消息按批次 `observed_at` 比较：**旧批不得回滚新状态**；**同时间先保存者优先**；
    **同批多条同类消息按原语句下标取首条**；
  - 整条消息原样保存：新船位坐标为 `null` 时保留 `null`，**不借用历史坐标**；
  - 错误、缺片与歧义候选**不写入**快照；歧义批仍返回 409，但其中明确解码的消息**仍更新**快照；
  - 本地 **SQLite** 持久化，Compose 重启后仍可查询。
- **船位轨迹（`GET /tracks`）**：
  - `/ingest` 成功解码的船位（消息 1/2/3）按原语句下标逐点写入，**迟到的旧观测仍保留在轨迹中**
    （只是不回滚 `/vessels` 最新快照），可按 `observed_at` 升序完整回看；
  - 同源同船同一 `observed_at` **只保留最先保存的一条**（`INSERT OR IGNORE`），重复提交、
    同批多条同时间船位都不会增加轨迹点；坐标为 `null` 的点原样保留，不借用相邻位置；
  - 建待核定批、无效报文、缺片与**未选中候选**均不生成轨迹点；普通入库时轨迹点与快照在
    **同一事务**提交，核定时轨迹点、快照与核定状态一同成功或回滚；
  - 打开旧版数据库时，现存的每船一条船位快照自动作为该船轨迹的**唯一起点**，不推断更早航迹。
- **误报标记（`POST /tracks/invalidations`）**：
  - 接收站按 `(source_id, MMSI, observed_at)` 把**一条已入库船位**标为无效并记录原因，响应
    返回该原始点；找不到该点返回 **404**，**重复标记返回首次记录（首次原因与标记时间）且不改原因**；
  - `/tracks` 只返回有效点（无效点行不删除、仍留在库中）；若该点正是 `/vessels` 的船位快照，
    快照**退至同源同船最新有效点**；已无有效点则**移除船位部分**，静态航次（消息 5）独立保留；
    标记较早的点**不回滚**有效的较新快照；
  - 标记之后，`/ingest` 与歧义批核定在该 `(source_id, MMSI, observed_at)` 再次收到船位时，
    **不会重新激活无效点、也不会写回船位快照**；其他时间的观测仍按原有规则写入；
  - 标记、轨迹可见性与快照回退在**同一事务**一次生效，SQLite 持久化，**重启后仍保持**；
    待核定批与已核定记录本身不受影响，既有接口的响应合同不变。
- **歧义批人工核定（`/reviews`）**：
  - `POST /reviews` 提交原语句与 `observed_at` 建立**待核定批**：复用同一套校验/重组/解码，
    持久保存原批与各歧义池的全部候选并返回批次 ID；**仅接受存在歧义的批次**，**建批时不写快照**；
  - `POST /reviews/{batch_id}/resolve` 为每个歧义池选定一个**已列出且成功解码**的候选，
    连同本批明确解码结果**按原语句下标**写入快照；缺选、伪造或解码失败的候选**整批拒绝**，
    不留部分写入；**核定状态与快照写入同事务**，已核定批不可改选或重复写入；
  - 核定前已有更晚观测写入时**较新快照不回滚**，批次仍记为已核定并返回**实际保存的快照**；
  - `GET /reviews` / `GET /reviews/{batch_id}` 查询待核定批、候选与核定结果，**重启后仍可查询**。
- 交互式 API 文档：`/docs`（Swagger UI）、`/redoc`。

## 快速开始

### Docker（一条命令）

```bash
docker compose up --build
```

服务监听 **http://localhost:8000**（`docker-compose.yml` 将宿主机 8000 映射到容器 8000）。
健康检查：`GET /health`。

快照数据库通过环境变量 `AIS_DB_PATH` 定位；Compose 将其设为 `/data/ais.db` 并挂载
命名卷 `ais-data`，因此 `docker compose restart`（乃至 `down` + `up`）后快照仍可查询。
本地直接运行时默认写入当前目录的 `ais_snapshots.db`。

### 本地运行

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 运行测试

```bash
pip install -r requirements-dev.txt
pytest -q          # 103 个单元/集成测试
```

## API

### `POST /decode`

请求体：

```json
{
  "sentences": [
    { "source_id": "ais-receiver-1", "sentence": "!AIVDM,1,1,,A,15Mwqgh01DreE9HGD@44gkg40000,0*1C" }
  ]
}
```

- `source_id`（必填）：接收机/数据源标识，是分片归组键的一部分；
- `sentence`（必填）：完整原始语句，含 `*HH` 校验和。

HTTP 状态码：

| 状态码 | 含义 |
| ------ | ---- |
| `200`  | 批次处理完成，无歧义分组（各结果状态见 `results[].status`） |
| `409`  | 至少一个无 `sequence_id` 分片池存在多个完整组合，候选见 `results[].candidates` |
| `422`  | 请求体不符合 schema（如空批次、缺字段） |

`results[].status` 取值：

| status | 含义 |
| ------ | ---- |
| `decoded`    | 成功解码，`decoded` 为消息体 |
| `incomplete` | 缺片，`missing_fragments` 列出缺失的分片序号 |
| `ambiguous`  | 多解，`candidates` 并列全部完整组合（此时整体响应为 409） |
| `error`      | 语句校验失败或解码失败，`error` 为机器可读错误码，`detail` 为说明 |

### `POST /ingest`

与 `/decode` 相同的语句列表，外加本批 UTC 观测时间 `observed_at`；完成同样的
校验/去重/重组/解码，并**把成功解码的消息写入持久快照**。请求体：

```json
{
  "observed_at": "2026-10-05T08:30:00Z",
  "sentences": [
    { "source_id": "ais-receiver-1", "sentence": "!AIVDM,1,1,,A,15Mwqgh01DreE9HGD@44gkg40000,0*1C" }
  ]
}
```

- `observed_at`（必填）：本批观测时间，ISO 8601；无时区按 UTC 处理，带偏移会换算为 UTC；
- `sentences`（必填）：同 `/decode`。

响应在 `/decode` 的 `summary` / `results` 之外增加 `observed_at` 与 `snapshots`
（本批涉及船舶的**合并快照**，即写入完成后的最新持久状态）。HTTP 状态码语义与
`/decode` 一致：`200` 正常，`409` 存在歧义分组（**明确解码的消息仍会更新快照**），
`422` 请求体不合法。

快照更新规则（按 `(source_id, MMSI)` 与消息类分别比较）：

| 规则 | 行为 |
| ---- | ---- |
| 新旧比较 | 仅当新消息的 `observed_at` **严格更新**才覆盖；旧批不得回滚新状态 |
| 同时间 | **先保存者优先**（后到的同时间批次不覆盖） |
| 同批多条同类 | 按原语句下标取**首条** |
| null 坐标 | 新船位坐标为 `null` 时保留 `null`，不借用历史坐标（快照与轨迹点均如此） |
| 不写入 | 错误、缺片、歧义候选不写入快照（也不生成轨迹点） |
| 轨迹 | 每条船位另按 `(source_id, MMSI, observed_at)` 追加到 `vessel_track`，迟到旧点保留、同时间去重，详见 `GET /tracks` |

### `GET /vessels`

按来源和 MMSI 查询已保存的合并快照；两个查询参数均可选（省略则列出全部）：

```
GET /vessels?source_id=ais-receiver-1&mmsi=366999999
```

返回 `VesselSnapshot` 数组，按 `(source_id, mmsi)` 排序。每个快照结构：

```json
{
  "source_id": "ais-receiver-1",
  "mmsi": 366999999,
  "position": {
    "observed_at": "2026-10-05T09:00:00Z",
    "data": { "message_type": 1, "mmsi": 366999999, "longitude": null, "latitude": 40.7484, "...": "..." }
  },
  "static": {
    "observed_at": "2026-10-05T08:30:00Z",
    "data": { "message_type": 5, "mmsi": 366999999, "name": "MV CLAUDE", "destination": "NEW YORK", "...": "..." }
  }
}
```

`position`（消息 1/2/3）与 `static`（消息 5）是两条独立时间线，各自携带写入时的
批次 `observed_at`；对应消息类尚未解码到时该侧为 `null`。

### `GET /tracks`

按来源和 MMSI 查询一船的**完整船位轨迹**。两个查询参数均为**必填**
（缺参返回 `422`；该来源/MMSI 无任何数据返回 `404`；只解到过消息 5 的船
返回 `200` 且 `points` 为空数组）：

```
GET /tracks?source_id=ais-receiver-1&mmsi=366999999
```

```bash
curl -s 'http://localhost:8000/tracks?source_id=ais-receiver-1&mmsi=366999999'
```

轨迹按 `observed_at` **升序**返回每个成功保存且仍有效的消息 1/2/3 船位；迟到的旧观测
照样在列（即便它没有回滚 `/vessels` 最新快照），同一 `observed_at` 的重复提交
不产生重复点，坐标 `null` 原样保留；被标记为误报的点不再出现（见
`POST /tracks/invalidations`）：

```json
{
  "source_id": "ais-receiver-1",
  "mmsi": 366999999,
  "points": [
    { "observed_at": "2026-10-05T11:00:00Z",
      "data": { "message_type": 1, "mmsi": 366999999, "longitude": -73.98009, "latitude": 40.7484, "...": "..." } },
    { "observed_at": "2026-10-05T12:00:00Z",
      "data": { "message_type": 1, "mmsi": 366999999, "longitude": -73.9835, "latitude": 40.7484, "...": "..." } },
    { "observed_at": "2026-10-05T13:00:00Z",
      "data": { "message_type": 1, "mmsi": 366999999, "longitude": null, "latitude": 40.7484, "...": "..." } }
  ]
}
```

| 状态码 | 含义 |
| ------ | ---- |
| `200` | 返回轨迹（无有效船位点时 `points` 为 `[]`） |
| `404` | 该 `(source_id, MMSI)` 没有任何已保存数据（船位点全部被标记无效时若也无快照则为 404） |
| `422` | 缺少必填查询参数 `source_id` 或 `mmsi` |

> `/tracks` 只返回**有效**船位点；被 `POST /tracks/invalidations` 标为误报的点仍保留在库中，
> 但不再出现在回放结果里。

### `POST /tracks/invalidations`（标记误报船位）

接收站发现某条已入库船位是误报时，按 `source_id`、`mmsi`、`observed_at` 定位该轨迹点，
并在 `reason` 中写明原因（非空）：

```bash
curl -s -X POST http://localhost:8000/tracks/invalidations \
  -H 'Content-Type: application/json' \
  -d '{
    "source_id": "ais-receiver-1",
    "mmsi": 366999999,
    "observed_at": "2026-10-05T12:00:00Z",
    "reason": "receiving station confirmed a false position report"
  }'
```

`observed_at` 与 `/ingest` 相同按 ISO 8601 归一化为 UTC（无时区按 UTC，带偏移换算）。
响应 `200` 返回**原始点**以及首次标记信息：

```json
{
  "source_id": "ais-receiver-1",
  "mmsi": 366999999,
  "observed_at": "2026-10-05T12:00:00Z",
  "reason": "receiving station confirmed a false position report",
  "invalidated_at": "2026-10-06T05:34:59.851499Z",
  "point": {
    "observed_at": "2026-10-05T12:00:00Z",
    "data": { "message_type": 1, "mmsi": 366999999, "longitude": -73.98009, "latitude": 40.7484, "...": "..." }
  }
}
```

| 状态码 | 含义 |
| ------ | ---- |
| `200` | 标记成功（或该点此前已标记——重复标记返回**首次**记录，原因不变） |
| `404` | 该 `(source_id, MMSI, observed_at)` 没有已保存的船位点 |
| `422` | 请求体不合法（缺字段、`reason` 为空等） |

标记的连带效果（标记、轨迹可见性与快照回退在同一事务内一次生效，重启后仍保持）：

- **轨迹**：该点立即从 `GET /tracks` 消失（其余点与顺序不变）；
- **快照**：若该点正是 `GET /vessels` 当前船位快照，船位侧退至**同源同船最新有效点**；
  已无有效点则该快照的 `position` 变为 `null`，**静态航次 `static` 原样保留**；标记较早的点
  不会回滚有效的较新快照；
- **不复活**：此后 `/ingest`（或歧义批的迟到核定）若再收到同一 `(source_id, MMSI, observed_at)`
  的船位，无效点不会被重新激活、也不会写回快照；其他观测时间的消息仍按原规则正常入库；
- **核定**：待核定批与已核定记录本身不受影响（核定状态、原候选与已记录的核定结果不变）。

```bash
# 1) 先写入 11:00 与 12:00 两个船位（快照停在最新的 12:00）
curl -s -X POST http://localhost:8000/ingest -H 'Content-Type: application/json' \
  -d '{"observed_at":"2026-10-05T11:00:00Z",
       "sentences":[{"source_id":"ais-receiver-1",
                     "sentence":"!AIVDM,1,1,,A,15Mwqgh01DreE9HGD@44gkg40000,0*1C"}]}' >/dev/null
curl -s -X POST http://localhost:8000/ingest -H 'Content-Type: application/json' \
  -d '{"observed_at":"2026-10-05T12:00:00Z",
       "sentences":[{"source_id":"ais-receiver-1",
                     "sentence":"!AIVDM,1,1,,A,15Mwqgh01DreF9HGD@44gkg40000,0*1F"}]}' >/dev/null

# 2) 12:00 被接收站确认为误报
curl -s -X POST http://localhost:8000/tracks/invalidations \
  -H 'Content-Type: application/json' \
  -d '{"source_id":"ais-receiver-1","mmsi":366999999,
       "observed_at":"2026-10-05T12:00:00Z","reason":"false position report"}'

# 3) /tracks 只剩 11:00；/vessels 快照也退到 11:00
curl -s 'http://localhost:8000/tracks?source_id=ais-receiver-1&mmsi=366999999'
curl -s 'http://localhost:8000/vessels?source_id=ais-receiver-1&mmsi=366999999'

# 4) 重复标记：返回首次记录，reason 仍是第一次的内容
curl -s -X POST http://localhost:8000/tracks/invalidations \
  -H 'Content-Type: application/json' \
  -d '{"source_id":"ais-receiver-1","mmsi":366999999,
       "observed_at":"2026-10-05T12:00:00Z","reason":"a later different reason"}'
```

### `POST /reviews`（建立待核定批）

请求体与 `/ingest` 相同（`observed_at` + `sentences`）。服务复用同一套校验、去重、
重组与解码，把**原批**和**各歧义池的全部候选**持久化并返回批次 ID；**建批时不写任何
船舶快照**。仅接受存在歧义的批次：

| 状态码 | 含义 |
| ------ | ---- |
| `201`  | 待核定批已建立，响应含 `batch_id`、`results`（含各池 `candidates`） |
| `422`  | 请求体不合法，或批中**不存在歧义分组**（无需核定） |

```bash
curl -s -X POST http://localhost:8000/reviews \
  -H 'Content-Type: application/json' \
  -d '{
    "observed_at": "2026-10-05T09:30:00Z",
    "sentences": [
      {"source_id": "ais-receiver-2",
       "sentence": "!AIVDM,2,1,,B,55M2oP@2:N0l4447774pu9@PE8r1=@5800000016<PD886P`,0*40"},
      {"source_id": "ais-receiver-2",
       "sentence": "!AIVDM,2,1,,B,55M2oPP2:N0p888;;;9<uE@PE8r0=8u=<000001@;@I796i9,0*43"},
      {"source_id": "ais-receiver-2",
       "sentence": "!AIVDM,2,2,,B,0B0Slm3kP00000000000000,2*3F"}
    ]
  }'
```

响应 `201`（节选）——`batch_id` 用于后续查询与核定：

```json
{
  "batch_id": "e9b441effb2d442f8dff0052f9ff3eee",
  "status": "pending",
  "observed_at": "2026-10-05T09:30:00Z",
  "resolved_at": null,
  "ambiguous_pools": 1,
  "results": [
    {
      "status": "ambiguous",
      "candidates": [
        { "sentence_indexes": [0, 2], "decoded": { "mmsi": 366000001, "name": "NORTHERN STAR", "...": "..." } },
        { "sentence_indexes": [1, 2], "decoded": { "mmsi": 366000002, "name": "SOUTHERN CROSS", "...": "..." } }
      ]
    }
  ]
}
```

### `GET /reviews` / `GET /reviews/{batch_id}`（查询）

```bash
curl -s 'http://localhost:8000/reviews'                      # 全部批次（摘要）
curl -s 'http://localhost:8000/reviews?status=pending'       # 仅待核定（或 resolved）
curl -s 'http://localhost:8000/reviews/e9b441effb2d442f8dff0052f9ff3eee'   # 单批详情
```

- 列表视图返回摘要（`batch_id`、`status`、`observed_at`、`created_at`、`resolved_at`、
  `total_sentences`、`ambiguous_pools`）；详情视图另含原批 `sentences`、完整 `results`
  （含各歧义池候选）；已核定批还含 `selections` 与核定实际写入的 `snapshots`；
- 未知批次 ID 返回 `404`；批次与候选存于 SQLite，**重启后仍可查询**。

### `POST /reviews/{batch_id}/resolve`（核定）

为**每个歧义池**选定一个候选：`selections` 的每一项是所选候选在 `results[].candidates`
中列出的 `sentence_indexes`（原样照抄）。核定时把选定候选与本批**明确解码**的结果一起，
按**原语句下标**顺序、以建批时的 `observed_at` 写入快照（同船同类同观测时间仍按下标取首条；
错误与缺片结果不入库）。

```bash
curl -s -X POST http://localhost:8000/reviews/e9b441effb2d442f8dff0052f9ff3eee/resolve \
  -H 'Content-Type: application/json' \
  -d '{"selections": [[0, 2]]}'
```

响应 `200`（节选）——批次记为已核定，`snapshots` 为**实际保存**的快照：

```json
{
  "batch_id": "e9b441effb2d442f8dff0052f9ff3eee",
  "status": "resolved",
  "resolved_at": "2026-10-05T10:05:00Z",
  "selections": [[0, 2]],
  "snapshots": [
    {
      "source_id": "ais-receiver-2",
      "mmsi": 366000001,
      "static": {
        "observed_at": "2026-10-05T09:30:00Z",
        "data": { "message_type": 5, "name": "NORTHERN STAR", "destination": "BOSTON", "...": "..." }
      }
    }
  ]
}
```

| 状态码 | 含义 |
| ------ | ---- |
| `200`  | 核定完成；快照写入与状态翻转**原子提交**，响应含实际保存的快照 |
| `404`  | 未知批次 ID |
| `409`  | 批次**已核定**（不可改选或重复写入） |
| `422`  | 缺选（数量与歧义池不符）、候选未列出（伪造）或所选候选解码失败——**整批拒绝，不留部分写入** |

核定前若已有更晚 `observed_at` 的观测写入同一 `(source_id, MMSI, 消息类)`，较新快照
**不回滚**；批次仍记为已核定，响应与后续 `GET /reviews/{batch_id}` 返回实际保存的快照。

#### 迟到观测与迟到核定示例

船位轨迹与快照的时序差异可用下面三步复现。先写入 `12:00` 船位，再迟到提交
`11:00` 船位：轨迹保留两点，快照仍是 `12:00`。

```bash
# 1) 12:00 的船位
curl -s -X POST http://localhost:8000/ingest \
  -H 'Content-Type: application/json' \
  -d '{"observed_at":"2026-10-05T12:00:00Z",
       "sentences":[{"source_id":"ais-receiver-1",
                     "sentence":"!AIVDM,1,1,,A,15Mwqgh01DreE9HGD@44gkg40000,0*1C"}]}'

# 2) 迟到的 11:00 船位（更旧）：进入轨迹，但快照不回滚
curl -s -X POST http://localhost:8000/ingest \
  -H 'Content-Type: application/json' \
  -d '{"observed_at":"2026-10-05T11:00:00Z",
       "sentences":[{"source_id":"ais-receiver-1",
                     "sentence":"!AIVDM,1,1,,A,15Mwqgh01DreF9HGD@44gkg40000,0*1F"}]}'

# 3) 回看轨迹：两点按 observed_at 升序；/vessels 仍停在 12:00
curl -s 'http://localhost:8000/tracks?source_id=ais-receiver-1&mmsi=366999999'
```

歧义批**迟到核定**（建批时间早于已入库的更新观测）时，核定写入的旧船位同样进入轨迹，
而 `/vessels` 最新快照保持不回滚：

```bash
# 1) 09:30 建立待核定批（含一条明确船位 + 一个歧义静态消息池）
BATCH=$(curl -s -X POST http://localhost:8000/reviews \
  -H 'Content-Type: application/json' \
  -d '{"observed_at":"2026-10-05T09:30:00Z","sentences":[
        {"source_id":"ais-receiver-2",
         "sentence":"!AIVDM,1,1,,A,15Mwqgh01DreE9HGD@44gkg40000,0*1C"},
        {"source_id":"ais-receiver-2",
         "sentence":"!AIVDM,2,1,,B,55M2oP@2:N0l4447774pu9@PE8r1=@5800000016<PD886P`,0*40"},
        {"source_id":"ais-receiver-2",
         "sentence":"!AIVDM,2,1,,B,55M2oPP2:N0p888;;;9<uE@PE8r0=8u=<000001@;@I796i9,0*43"},
        {"source_id":"ais-receiver-2",
         "sentence":"!AIVDM,2,2,,B,0B0Slm3kP00000000000000,2*3F"}]}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["batch_id"])')

# 2) 核定前，同船 12:00 的更新船位已经正常入库
curl -s -X POST http://localhost:8000/ingest \
  -H 'Content-Type: application/json' \
  -d '{"observed_at":"2026-10-05T12:00:00Z",
       "sentences":[{"source_id":"ais-receiver-2",
                     "sentence":"!AIVDM,1,1,,A,15Mwqgh01DreF9HGD@44gkg40000,0*1F"}]}'

# 3) 人工迟到核定（批内观测时间仍是 09:30）：旧船位进轨迹，快照保持 12:00
curl -s -X POST "http://localhost:8000/reviews/$BATCH/resolve" \
  -H 'Content-Type: application/json' -d '{"selections":[[1,3]]}'

# 轨迹含 09:30（核定写入）与 12:00（先前入库）两点，升序返回
curl -s 'http://localhost:8000/tracks?source_id=ais-receiver-2&mmsi=366999999'
```

若核定请求缺选、伪造候选或选中解码失败的候选，整批拒绝——轨迹点、快照与
`review_batch` 状态一起回滚，不留部分写入。

### 哨兵值 → `null`

| 字段 | 原始哨兵值 |
| ---- | ---------- |
| `rate_of_turn` | -128（0x80） |
| `speed_over_ground` | 1023 |
| `longitude` | 181°（0x6791AC0） |
| `latitude` | 91°（0x3412140） |
| `course_over_ground` | 3600 |
| `true_heading` | 511 |
| `timestamp` | 60 |
| `imo_number` / `ship_type` / `draught` | 0 |
| `eta_month` / `eta_day` | 0 |
| `eta_hour` / `eta_minute` | 24 / 60 |

## 示例

以下示例报文由 `scripts/generate_examples.py` 生成（可重复执行验证），响应为实际服务输出。

### 1. 单片解码（消息 1）

```bash
curl -s -X POST http://localhost:8000/decode \
  -H 'Content-Type: application/json' \
  -d '{
    "sentences": [
      {"source_id": "ais-receiver-1",
       "sentence": "!AIVDM,1,1,,A,15Mwqgh01DreE9HGD@44gkg40000,0*1C"}
    ]
  }'
```

响应 `200`（节选）：

```json
{
  "results": [
    {
      "source_id": "ais-receiver-1",
      "status": "decoded",
      "message_type": 1,
      "decoded": {
        "message_type": 1,
        "mmsi": 366999999,
        "navigation_status": 0,
        "speed_over_ground": 8.4,
        "longitude": -73.9835,
        "latitude": 40.7484,
        "course_over_ground": 121.5,
        "true_heading": 119,
        "timestamp": 34
      }
    }
  ]
}
```

### 2. 多片乱序重组（消息 5，分片 2 先于分片 1 到达）

```bash
curl -s -X POST http://localhost:8000/decode \
  -H 'Content-Type: application/json' \
  -d '{
    "sentences": [
      {"source_id": "ais-receiver-1",
       "sentence": "!AIVDM,2,2,4,A,NECQEp6ClRh000000000000,2*1F"},
      {"source_id": "ais-receiver-1",
       "sentence": "!AIVDM,2,1,4,A,55Mwqgh2:N2UL@?7;?@mJ0<h5D@D000000000016?0N:<6Lf,0*5E"}
    ]
  }'
```

响应 `200`（节选）——两片按 `sequence_id=4` 归组、按序号重组后解码：

```json
{
  "results": [
    {
      "status": "decoded",
      "sequence_id": "4",
      "total_fragments": 2,
      "received_fragments": [1, 2],
      "message_type": 5,
      "decoded": {
        "mmsi": 366999999,
        "imo_number": 9074729,
        "call_sign": "WDC1234",
        "name": "MV CLAUDE",
        "ship_type": 70,
        "draught": 8.5,
        "destination": "NEW YORK"
      }
    }
  ]
}
```

### 3. 歧义（无 sequence_id，两个分片 1 共享一个分片 2）→ 409

```bash
curl -s -X POST http://localhost:8000/decode \
  -H 'Content-Type: application/json' \
  -d '{
    "sentences": [
      {"source_id": "ais-receiver-2",
       "sentence": "!AIVDM,2,1,,B,55M2oP@2:N0l4447774pu9@PE8r1=@5800000016<PD886P`,0*40"},
      {"source_id": "ais-receiver-2",
       "sentence": "!AIVDM,2,1,,B,55M2oPP2:N0p888;;;9<uE@PE8r0=8u=<000001@;@I796i9,0*43"},
      {"source_id": "ais-receiver-2",
       "sentence": "!AIVDM,2,2,,B,0B0Slm3kP00000000000000,2*3F"}
    ]
  }'
```

响应 **`409`**（节选）——两个完整组合都成立，并列候选，不做猜测：

```json
{
  "results": [
    {
      "status": "ambiguous",
      "detail": "2 complete combinations are possible for fragment pool ...",
      "candidates": [
        {
          "sentence_indexes": [0, 2],
          "message_type": 5,
          "decoded": { "mmsi": 366000001, "name": "NORTHERN STAR", "destination": "BOSTON" }
        },
        {
          "sentence_indexes": [1, 2],
          "message_type": 5,
          "decoded": { "mmsi": 366000002, "name": "SOUTHERN CROSS", "destination": "MIAMI" }
        }
      ]
    }
  ]
}
```

### 4. 缺片（sequence_id=9 的分片 2 未到达）

```bash
curl -s -X POST http://localhost:8000/decode \
  -H 'Content-Type: application/json' \
  -d '{
    "sentences": [
      {"source_id": "ais-receiver-3",
       "sentence": "!AIVDM,2,1,9,A,55Mwqgh2:N2UL@?7;?@mJ0<h5D@D000000000016?0N:<6Lf,0*53"}
    ]
  }'
```

响应 `200`：

```json
{
  "results": [
    {
      "status": "incomplete",
      "sequence_id": "9",
      "total_fragments": 2,
      "received_fragments": [1],
      "missing_fragments": [2],
      "detail": "missing fragment(s) 2 of 2"
    }
  ]
}
```

### 5. 写入快照并查询（`/ingest` + `/vessels`）

```bash
curl -s -X POST http://localhost:8000/ingest \
  -H 'Content-Type: application/json' \
  -d '{
    "observed_at": "2026-10-05T08:30:00Z",
    "sentences": [
      {"source_id": "ais-receiver-1",
       "sentence": "!AIVDM,1,1,,A,15Mwqgh01DreE9HGD@44gkg40000,0*1C"},
      {"source_id": "ais-receiver-1",
       "sentence": "!AIVDM,2,2,4,A,NECQEp6ClRh000000000000,2*1F"},
      {"source_id": "ais-receiver-1",
       "sentence": "!AIVDM,2,1,4,A,55Mwqgh2:N2UL@?7;?@mJ0<h5D@D000000000016?0N:<6Lf,0*5E"}
    ]
  }'
```

响应 `200`（节选）——`results` 与 `/decode` 相同，`snapshots` 给出本批涉及船舶的合并快照：

```json
{
  "observed_at": "2026-10-05T08:30:00Z",
  "summary": { "total_sentences": 3, "decoded": 2 },
  "snapshots": [
    {
      "source_id": "ais-receiver-1",
      "mmsi": 366999999,
      "position": {
        "observed_at": "2026-10-05T08:30:00Z",
        "data": { "message_type": 1, "longitude": -73.9835, "latitude": 40.7484, "speed_over_ground": 8.4 }
      },
      "static": {
        "observed_at": "2026-10-05T08:30:00Z",
        "data": { "message_type": 5, "name": "MV CLAUDE", "destination": "NEW YORK" }
      }
    }
  ]
}
```

随后任意时刻按来源和 MMSI 查询（重启后仍有效）：

```bash
curl -s 'http://localhost:8000/vessels?source_id=ais-receiver-1&mmsi=366999999'
```

```json
[
  {
    "source_id": "ais-receiver-1",
    "mmsi": 366999999,
    "position": { "observed_at": "2026-10-05T08:30:00Z", "data": { "longitude": -73.9835, "...": "..." } },
    "static":   { "observed_at": "2026-10-05T08:30:00Z", "data": { "name": "MV CLAUDE", "...": "..." } }
  }
]
```

时序行为示例：先以 `observed_at=2026-10-05T08:30:00Z` 写入船位后，再以
`observed_at=2026-10-05T08:00:00Z`（更早）提交同一船舶的另一船位——快照保持
`08:30` 的状态不变，但 `GET /tracks` 轨迹同时保留 `08:00` 与 `08:30` 两点；
而以 `observed_at=2026-10-05T09:00:00Z` 提交坐标为 `null`
的 `!AIVDO` 船位后，快照坐标即为 `null`（不回填历史坐标），轨迹中该点坐标也是 `null`。
重复提交同一 `observed_at` 的船位不会增加轨迹点。

## 设计说明

- **归组键**：多片归组严格限定在同一 `source_id`、同一频道内；不同接收机/频道的同名
  `sequence_id` 不会混淆。
- **歧义语义**：无 `sequence_id` 时，服务对分片池做笛卡尔枚举，每个候选组合中同一物理
  语句至多使用一次；组合唯一才解码，否则整体返回 409 并列出全部候选（候选数上限 100，
  超出时 `detail` 会注明截断）。带 `sequence_id` 的组内若同序号分片内容冲突，返回
  `conflicting_fragments` 错误而非猜测。
- **位长严格性**：六位 payload 去掉 fill bits 后，消息 1/2/3 必须 168 bit、消息 5 必须
  424 bit；任何更长/更短的报文都以 `decode_error` 拒绝，避免输出错位字段。
- **解码正确性**：解码器已与参考实现 `pyais` 交叉验证（800 条随机消息 1/5 逐字段一致）。
- **快照存储**：每 `(source_id, MMSI, 消息类)` 一行 SQLite 记录，`observed_at` 归一化为
  定宽 UTC ISO 8601 文本，覆盖条件为 SQL 级 `excluded.observed_at > 已存值` 的原子
  upsert——严格更新才覆盖，天然实现“旧批不回滚、同时间先保存者优先”；`/ingest` 按
  语句下标顺序应用本批解码结果，因此同批同类首条生效。快照保存整条解码消息，
  不做字段级合并（null 坐标原样保留）。
- **轨迹存储**：`vessel_track` 以 `(source_id, mmsi, observed_at)` 为主键、`INSERT OR
  IGNORE` 写入，因此迟到旧观测成为轨迹中间的一点、重复提交不增点；普通入库的快照
  upsert 与轨迹 insert 在**单个事务**内完成。首次打开旧库时把 `vessel_state` 中既有的
  船位快照幂等地回填为每船**唯一起点**（`sentence_index = -1`），不推断更早航迹。
- **误报标记**：`vessel_track` 另设 `invalid` / `invalid_reason` / `invalidated_at` 三列
  （打开旧库时自动 `ALTER TABLE` 补齐，不改动任何已有点）；标记操作在**单个事务**内把该点
  置无效，并在其正是当前船位快照时把 `vessel_state` 船位行回退到 `vessel_track` 中最新有效点，
  没有有效点则删除该行（静态行独立保留）。重复标记不改任何字段，只回读首次记录。入库与核定
  共用的写入逻辑对已无效时间槽跳过快照 upsert，因此同时间的新观测既不复活轨迹点也不写回快照。
- **人工核定**：待核定批的原语句、解码结果（含各歧义池全部候选）与核定结果
  （`selections` + 实际写入的快照）以 JSON 持久化在同一 SQLite 库中，重启后仍可查询；
  核定时“应用全部快照与轨迹写入 + 翻转批次状态”在**单个事务**内完成——任一失败整体回滚，
  批次保持待核定且不留任何轨迹点；状态翻转带 `status='pending'` 条件，并发/重复核定只会成功一次。

## 项目结构

```
app/
  main.py        # FastAPI 入口，POST /decode、/ingest、/reviews、/tracks/invalidations，GET /vessels、/tracks、/reviews、/health
  models.py      # Pydantic 请求/响应模型（含快照、船位轨迹、误报标记与核定批模型）
  nmea.py        # NMEA 0183 语句解析与校验（XOR 校验和、分片、fill bits）
  ais.py         # 六位 armour、位提取、消息 1/2/3 与 5 解码
  assembler.py   # 归组、乱序重组、孤儿分片池歧义枚举
  storage.py     # SQLite 快照、船位轨迹（含旧库起点回填/无效列迁移）、误报标记与核定批持久化（核定事务）
scripts/
  generate_examples.py  # 重新生成 README/测试中的示例报文
tests/           # pytest 单元与 API 集成测试（含快照、轨迹时序、误报标记语义与人工核定流程）
Dockerfile
docker-compose.yml      # 命名卷 ais-data 持久化 /data/ais.db
requirements.txt / requirements-dev.txt
```
