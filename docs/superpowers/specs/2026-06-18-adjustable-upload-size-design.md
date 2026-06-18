# 可调整的上传文件大小限制（admin 权限）— 设计

日期：2026-06-18
状态：已批准设计，待实现

## 背景与目标

当前单文件大小上限 `max_upload_bytes`（默认 10 MB）是启动时从环境变量
`SOPHCLAW_MAX_UPLOAD_BYTES` 读入、缓存在 `Config` 单例里的**静态值**。它在
`sophclaw/api/file_routes.py` 中被两处使用：

- 上传 `/upload`（`file_routes.py:126`）：解码后超过上限返回 413。
- 读取/下载 `/read`（`file_routes.py:107`）：文件大于上限返回 413。

目标：让这个上限在**运行时可由 admin 调整**，改动立即生效、重启后保留，普通用户
无修改权。

> 范围说明：`sophclaw/tools/files.py` 的 `MAX_READ_BYTES`（256 KB）是 agent 读文件
> 工具的文本/二进制解码阈值，与本需求无关，**不在范围内**。

## 决策摘要

| 维度 | 决策 |
|------|------|
| 限制范围 | **统一一个上限**，同时作用于上传与读取/下载（保持现状语义） |
| 持久化 | **存 DB，运行时生效**；环境变量/默认值作为初始回退 |
| 前端 | **后端 API + admin 面板界面**，界面显示上下限提示 |

## 第 1 节：数据层

新增通用 settings 键值表（为后续运行时配置预留，当前只用一个键
`max_upload_bytes`）：

```sql
CREATE TABLE IF NOT EXISTS settings (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at REAL,
  updated_by INTEGER
);
```

DB 层新增方法：

- `async def get_setting(key: str) -> str | None`
- `async def set_setting(key: str, value: str, user_id: int) -> None` —— upsert，
  同时写入 `updated_at`（当前时间戳）与 `updated_by`。

生效解析（新增模块级 helper，放在 `config.py` 或新建 `settings.py`）：

```python
async def effective_max_upload_bytes(db) -> int:
    raw = await db.get_setting("max_upload_bytes")
    if raw is not None:
        try:
            return int(raw)
        except ValueError:
            pass
    return get_config().max_upload_bytes
```

`file_routes.py` 的上传与读取改为在请求时调用 `effective_max_upload_bytes(db)`，
不再直接读 `cfg.max_upload_bytes`。SQLite 为共享文件，多 worker 天然一致，无需额外
缓存刷新机制。

## 第 2 节：API 层

新增 `sophclaw/api/settings_routes.py`，挂载在 `/api/settings`，并在 `main.py` 注册。

常量：`MIN_UPLOAD_BYTES = 1024`（1 KB），`MAX_UPLOAD_CEILING = 1024 * 1024 * 1024`
（1 GB，硬顶，不可调）。

### `GET /api/settings`（`require_user`，任意登录用户）

```json
{
  "max_upload_bytes": 10485760,
  "default_max_upload_bytes": 10485760,
  "min_bytes": 1024,
  "max_bytes": 1073741824
}
```

- `max_upload_bytes`：当前生效值（`effective_max_upload_bytes`）。
- `default_max_upload_bytes`：环境变量/内置默认（`cfg.max_upload_bytes`）。
- `min_bytes` / `max_bytes`：校验边界，供前端显示与预校验。

放开给所有登录用户的理由：上限影响每个上传者，前端可据此提前提示；修改权仍只在
admin。

### `PUT /api/settings/max_upload_bytes`（`require_admin`，仅 admin）

请求体：`{ "max_upload_bytes": <int> }`

校验：

- 必须是正整数；
- `MIN_UPLOAD_BYTES <= value <= MAX_UPLOAD_CEILING`；
- 不满足 → `400`。

通过后 `set_setting("max_upload_bytes", str(value), user_id)`，返回 `{ "ok": true }`。

> 硬顶理由：上传走 base64，整文件会被解码进内存，无上限存在 OOM 风险。1 GB 是安全
> 天花板，admin 可在此范围内自由调整。

## 第 3 节：前端

`web/index.html` 的 `renderAdmin()` 中新增一张卡片，置于 Providers 卡片附近：

- 标题：**系统设置（System settings — admin）**
- 一个数字输入框，**以 MB 为单位**（存取时与字节互转）：
  - 进入时 `GET /api/settings`，将 `max_upload_bytes` 换算成 MB 填入；
  - 输入框旁显示**上下限提示**，例如：
    `允许范围：0.001 – 1024 MB（当前默认 10 MB）`
    （由 `min_bytes` / `max_bytes` / `default_max_upload_bytes` 换算得到）。
- 「保存」按钮 → `PUT /api/settings/max_upload_bytes`（MB→字节，四舍五入为整数）。
  - 前端先做范围校验，越界直接提示、不发请求；后端兜底再校验。
  - 成功/失败沿用现有 `alert` / `alertErr` 风格反馈。

## 测试（TDD：先写测试再实现）

1. **DB**：`set_setting` / `get_setting` upsert 往返（含覆盖更新）。
2. **生效解析** `effective_max_upload_bytes`：
   - 无 DB 值 → 回退 `cfg.max_upload_bytes`；
   - 有 DB 值 → 用 DB 值；
   - DB 值非法（非整数）→ 回退默认。
3. **API**：
   - `GET /api/settings` 普通用户可读，四个字段齐全；
   - `PUT` admin 成功；非 admin → 403；
   - 越界（< 1 KB、> 1 GB、非正整数）→ 400；
   - 改完上限后，`/upload` 与 `/read` 按**新**上限触发 413（端到端验证生效）。
4. **改造** `tests/test_files_api.py:40`：原先直接改 `get_config().max_upload_bytes`，
   改为通过 `set_setting` 注入低上限，验证新解析路径。

## 受影响文件

- `sophclaw/db.py` — settings 表 + `get_setting`/`set_setting`
- `sophclaw/config.py`（或新建 `sophclaw/settings.py`）— `effective_max_upload_bytes`
- `sophclaw/api/file_routes.py` — 上传/读取改用生效解析
- `sophclaw/api/settings_routes.py` — 新增
- `sophclaw/main.py` — 注册 router
- `web/index.html` — admin 面板系统设置卡片
- `tests/` — 新增 settings 相关测试 + 改造 `test_files_api.py`
