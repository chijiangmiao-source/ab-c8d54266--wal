# wal-recover — 星载归档镜像 WAL 恢复服务

断电后的星载归档只保留主数据库与可能截断的 WAL。本服务从原始字节解析
SQLite 3 主库与标准 WAL，校验 WAL 头与全部帧（魔数、格式版本、盐值、跨帧
累计校验和），以最后一个校验完整且提交库大小非零的帧确定恢复前缀，重建
最后一次可恢复提交的数据库镜像，避免把未提交的遥测页带入下行副本。

## 限制

- 仅支持小端校验和的标准 WAL（魔数 `0x377F0682`；大端校验和的
  `0x377F0683` 一律拒绝）与标准 SQLite 3 主库（`SQLite format 3\0`）。
- 页大小限 512–4096 字节（2 的幂），主库与 WAL 页大小必须一致。
- WAL 解码后不超过 2 MiB；请求体不超过 32 MiB；重建镜像不超过 64 MiB。
- 主库允许为 0 字节（全新数据库，全部页都在 WAL 中），此时以 WAL 页大小为准。

## 严格失败策略

出现以下任一情况时，整个恢复失败并定位首个失效偏移，**不返回部分镜像**：

| 条件 | error.code | offset |
| --- | --- | --- |
| WAL 超过 2 MiB | `wal_too_large` | `null` |
| WAL 不足 32 字节头 | `invalid_wal_header` | 0 |
| 魔数非法或为不支持的 `0x377F0683` | `unsupported_magic` | 0 |
| WAL 格式版本非 3007000 | `unsupported_version` | 4 |
| WAL 页大小越界/非 2 的幂 | `invalid_page_size` | 8 |
| WAL 头校验和不符 | `header_checksum_mismatch` | 24 |
| 帧截断 | `truncated_frame` | 该帧起始偏移 |
| 帧盐值与 WAL 头不符（盐值变化） | `salt_mismatch` | 该帧起始偏移 |
| 页号为 0 或大于 `0xFFFFFFFE` | `invalid_page_number` | 该帧起始偏移 |
| 跨帧累计校验和不符 | `checksum_mismatch` | 该帧起始偏移 |
| 主库/WAL 页大小不一致 | `page_size_mismatch` | `null` |
| 主库头非法 | `invalid_database` | `null` |
| 主库页大小越界 | `unsupported_page_size` | `null` |
| 全部帧有效但无完整提交 | `no_commit` | `null` |
| 提交声明的镜像超过 64 MiB | `image_too_large` | `null` |

请求本身 malformed 时返回 400：`invalid_json` / `invalid_base64` /
`invalid_request`；另有 404 `not_found`、411、413 `request_too_large`。

## API

### `GET /health`

```json
{"status": "ok"}
```

### `POST /recover`

请求：

```json
{
  "database": "<base64 主库>",
  "wal": "<base64 WAL>",
  "stable_page_order": true
}
```

- `stable_page_order`（可选，默认 `false`）：为 `true` 时 `pages` 按页号
  升序稳定输出；为 `false` 时按恢复应用顺序（来源帧号、页号）输出。

成功（200）：

```json
{
  "ok": true,
  "commit_frame": 7,
  "commit_size_pages": 12,
  "recovered_pages": 9,
  "pages": [{"page": 1, "source_frame": 3}],
  "image_size": 12288,
  "image_sha256": "<重建镜像 SHA-256>",
  "image": "<base64 重建镜像>"
}
```

- `commit_frame`：恢复前缀的末帧（最后一个有效提交帧，1 起始帧号）。
- `commit_size_pages`：该提交帧声明的数据库页数，即镜像页数。
- `recovered_pages`：写入最终镜像的 WAL 去重页数（页号 ≤ 提交库大小）。
- `pages[].source_frame`：该页最终页像来源的帧号（前缀内最后一次出现）。
- 镜像 = 主库截断/零填充到提交大小后，以前缀内各页最后一次出现覆盖；
  末次提交之后的有效但未提交帧一律排除。

失败（422）：

```json
{"ok": false, "error": {"code": "checksum_mismatch", "message": "...", "offset": 3352}}
```

失败响应绝不含 `image` 字段。

## 运行与验收

```sh
# 构建镜像、启动 app，待健康检查后由 verify 服务执行一次
# 单元测试（有效多事务 WAL / 末帧损坏 / 无提交 WAL 等）与 HTTP 冒烟，
# 以其退出码给出验收结果：
docker compose up --build --exit-code-from verify
echo $?        # 0 = 验收通过
docker compose down

# 复核脚本访问宿主机端口（默认 8080，可用 HOST_PORT 覆盖）：
HOST_PORT=9090 docker compose up -d app
curl -s http://127.0.0.1:9090/health
```

本地无 Docker 时可直接运行同一套检查：

```sh
python3 -m unittest tests.test_walrec -v
python3 -m app.server &          # PORT=8080 可覆盖
APP_URL=http://127.0.0.1:8080 python3 tests/smoke.py
```

## 结构

```
app/walrec.py    恢复引擎：WAL 头/帧头原始字节解析、校验链、恢复前缀与镜像重建
app/server.py    HTTP 前端：/health 与 /recover（仅标准库，无第三方依赖）
tests/           单元测试、WAL 构造夹具（含真实 sqlite3 生成的多事务 WAL）、HTTP 冒烟
verify.sh        verify 服务的一次性验收入口（退出码即验收结果）
docker-compose.yml  app（健康检查 + 可配置宿主机端口）与 verify 服务
```
