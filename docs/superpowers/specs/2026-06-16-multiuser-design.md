# sophagent 多用户 + 用户组设计

状态：已批准设计，待写实现计划
日期：2026-06-16

## 1. 背景与目标

当前 sophagent 的多用户模型很薄：

- `users` 表有 `role`（admin/user），首个引导用户是 admin。
- `agents` 全局共享，仅 admin 可增删改，所有登录用户可见。
- `sessions` 按 `user_id` 严格私有；`memory`、`workspace` 同样按用户隔离。

本设计引入完整的「用户组 + 所有权 + 权限委派 + 加入审批/邀请」模型，满足 REQ1.1–REQ1.8。

### 核心不变量

- 每个 user 在创建时自动获得一个**自己拥有的 group**（REQ1.7）。
- admin 的那个 group 就是 **admin group**（REQ1.8），全库唯一。
- **admin 身份 = 是 admin group 的成员**（派生，非独立 role 字段）。
- **root admin = admin group 的 owner**（REQ1.1 引导创建的首个用户），全库唯一，拥有最高权限。
- 一个 user 可同时属于多个 group：自己的个人组（owner）+ 加入的其他组（member）。

## 2. 数据模型

### 新增表

```sql
CREATE TABLE groups (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  owner_id INTEGER NOT NULL REFERENCES users(id),
  is_admin_group INTEGER NOT NULL DEFAULT 0,   -- 全库仅一行为 1
  created_at TEXT NOT NULL
);

CREATE TABLE group_members (
  group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  user_id  INTEGER NOT NULL REFERENCES users(id)  ON DELETE CASCADE,
  can_manage INTEGER NOT NULL DEFAULT 0,         -- REQ1.5 单一 manage 标志
  joined_at TEXT NOT NULL,
  PRIMARY KEY (group_id, user_id)
);
-- owner 也作为一行存在，can_manage 恒为 1

CREATE TABLE group_join (                          -- 申请与邀请合一（REQ1.6）
  id INTEGER PRIMARY KEY,
  group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  user_id  INTEGER NOT NULL REFERENCES users(id)  ON DELETE CASCADE,
  kind TEXT NOT NULL,            -- 'request'(用户申请) | 'invite'(owner邀请)
  created_at TEXT NOT NULL,
  UNIQUE(group_id, user_id, kind)  -- 仅保留 pending 行，处理后删除
);
```

### 修改现有表

- `agents`：新增 `group_id INTEGER NOT NULL REFERENCES groups(id)`；唯一约束从 `name` 改为 `UNIQUE(group_id, name)`（agent 名仅组内唯一）。
- `sessions`：新增 `group_id INTEGER REFERENCES groups(id)`（建会话时从 agent 继承）；保留 `user_id` 作为**创建者**。
- `users.role`：**保留**作缓存/兼容；登录时按 admin group 成员资格同步写入；所有鉴权以组成员关系为准（live 查询）。

> SQLite 不支持给已有表加 `UNIQUE` 约束或简单删旧约束。`agents` 的唯一约束变更通过迁移：建新表 `agents_new` → 拷贝数据（带 group_id）→ 删旧表 → 改名。详见第 5 节。

## 3. 权限矩阵

身份判定（按优先级，live 查询）：

- **root admin** = `groups.owner_id WHERE is_admin_group=1` 的用户。
- **admin** = admin group 的任一成员。
- **group owner** = `groups.owner_id`。
- **group manager** = 该组 `group_members.can_manage=1` 的成员。
- **group member** = 该组任一成员。

| 身份 | 能力 |
|---|---|
| **root admin** | 最高权限；可修改 admin group 内任意其他成员（收回 can_manage、移出 admin group、删除账户）；唯一可修改自身账户者 |
| **admin**（admin group 成员） | 跨所有 group/user/agent/session 全部操作；增删改 user 与 group；管理任意组成员与权限。**例外**：不可对 root admin 执行任何写操作 |
| **group owner** | 本组 agent/session 全 CRUD；审批申请、发邀请、增删成员、授予/收回成员 `can_manage`；自身不可被移出/降权 |
| **成员 + can_manage** | 本组 agent 增删改；列出/删除本组**任意**成员的 session；不能管理成员与权限 |
| **普通成员**（默认，REQ1.4） | 列出/使用本组 agent；对其建**自己的** session 并对话；只能看/改/删自己的 session |

### root admin 保护规则（用户补充要求）

任何针对 `target_user == root_admin` 且 `actor != root_admin` 的写操作（patch/delete user、移出 admin group、改 can_manage、降权）一律 **403**。root admin 自身不受此限制，可修改自己的密码等。

### 已裁定的歧义

REQ1.4 文面把「创建 session」列在 owner 名下，但普通成员若不能建会话就无法使用 agent。裁定：**任何成员都能创建并对话自己的 session**（基础使用权）；`can_manage`/owner 额外获得的是「agent 定义的增删改」+「管理他人 session」。`memory` 与 `workspace` 维持按用户（创建者）隔离，不变。

## 4. API 设计

### 用户 `/api/users`（admin 专属）

沿用 list/create/patch/delete，调整：

- `create_user`：同时自动建其个人 group（`name = username + "'s group"`）并设为 owner + 成员（REQ1.7）。
- `UserCreate.role` 字段**移除**——「设为管理员」改为「把用户加入 admin group」。
- 删除 user：级联删除其拥有的 group（及组内 agent/session）。
- 所有写操作叠加 root admin 保护规则。
- 保留「不能删除/降权最后一个 admin」语义（root admin 本就不可删）。

### 组 `/api/groups`

- `GET /api/groups` — 我所属的组（admin 见全部）
- `GET /api/groups/{gid}` — 详情含成员（成员或 admin）
- `POST /api/groups` — admin 建组（body 可指定 `owner_id`，默认调用者；honors REQ1.2）
- `PATCH /api/groups/{gid}` — 改名（owner/admin）
- `DELETE /api/groups/{gid}` — admin 删除非个人组（个人组随用户删除而删除；admin group 不可删）

### 成员与权限

- `GET /api/groups/{gid}/members` — 成员/admin
- `POST /api/groups/{gid}/members {user_id}` — owner/admin 直接添加（admin group 加人走此路，REQ1.8）
- `PATCH /api/groups/{gid}/members/{uid} {can_manage}` — owner/admin 授予/收回（REQ1.5）
- `DELETE /api/groups/{gid}/members/{uid}` — 移除（不可移除 owner；root admin 保护）

### 申请与邀请（REQ1.6）

- `POST /api/groups/{gid}/join` — 当前用户申请加入（kind=request）
- `POST /api/groups/{gid}/invite {user_id}` — owner/admin 邀请（kind=invite）
- `GET /api/groups/{gid}/pending` — owner/admin 看本组待处理申请
- `GET /api/me/invitations` — 我收到的邀请
- `POST /api/joinreq/{id}/approve` — owner/admin 批准 request → 写入成员
- `POST /api/joinreq/{id}/accept` — 用户接受 invite → 写入成员
- `DELETE /api/joinreq/{id}` — 拒绝/撤销/婉拒（按 kind 校验调用者）

### agent `/api/agents`

- `GET /api/agents` — 我可见的组的 agent（admin 见全部）
- `AgentCreate` 增加 `group_id`；create/update/delete 要求该组 owner/can_manage/admin
- 普通成员仅可 list + 建 session
- `meta/options` 仍返回 tools/providers（任意已登录用户可见即可，admin 不再强制）

### session `/api/sessions`

- 建会话：校验「调用者是 agent 所属组的成员」，`group_id` 继承自 agent
- `GET /api/sessions` — 我创建的 session（不变）
- 改/删自己的 session — 不变
- 新增 `GET /api/groups/{gid}/sessions` — owner/can_manage/admin 看全组会话
- 删除他人会话 — 需 owner/can_manage/admin（经组会话端点或带组校验）

### openai 兼容层 `/v1`

- `/v1/models` — 仅列出调用者可见的 agent
- `/v1/chat/completions` — 在调用者可见的 agent 中按 `model` 名解析；跨组重名时取首个匹配（文档说明）

## 5. 迁移、鉴权实现与前端

### 数据迁移（`db.connect` 内幂等执行）

1. 建新表（groups / group_members / group_join）。
2. 若不存在 admin group：取现有 `role='admin'` 且 id 最小者为 root admin，为其建 admin group（`is_admin_group=1`，owner=该用户），并写 admin group 成员行。
3. 为其余每个现有用户建个人 group（owner=自身）+ 成员行。
4. `agents` 表结构迁移（建 `agents_new` 带 `group_id` + `UNIQUE(group_id,name)`）：现有全局 agents 全部归入 admin group。
5. `sessions` 加 `group_id`：按各 session 的 `user_id` 归入该用户的个人 group。

迁移以「检测表/列是否存在」做幂等守卫，可在已迁移库上重复运行无副作用。

### 鉴权实现（`auth.py`）

- `is_admin(db, user_id) -> bool`：查 admin group 成员资格。
- `get_root_admin_id(db) -> int`：admin group 的 owner_id。
- `group_access(db, gid, user_id) -> 'none'|'member'|'manager'|'owner'|'admin'`：综合判定，admin 直接返回 'admin'。
- `require_admin` 改为基于 `is_admin`。
- 提供可复用的依赖/辅助函数供路由做组级校验。
- 登录时把派生的 admin 状态同步写回 `users.role`（缓存）。

### 前端 `web/index.html`（完整界面，Q4）

- 「我的组」视图：组列表、成员管理（加人/授权 can_manage/移除）、待处理申请审批、收到的邀请、申请加入其他组。
- agent/session 视图按当前所选组过滤；建 agent 时选择目标组。
- admin 额外可见全局用户管理 + 全局组管理面板；UI 对 root admin 保护项做禁用态。

### 测试 `tests/test_api.py`

扩展覆盖：

- 组隔离：A 组成员看不到 B 组的 agent/session。
- 权限授予/收回：can_manage 开关对 agent CRUD 的影响。
- 申请/邀请全流程：request→approve、invite→accept、拒绝/撤销。
- admin group 派生权限：加入 admin group 即获 admin 能力。
- root admin 保护：其他 admin 改不动 root admin（403），root admin 可改他人。
- 迁移幂等：现有 fixture 数据迁移后行为正确。
- 普通成员可建自己 session 并对话；owner 可见全组 session。

## 6. 非目标（YAGNI）

- 不做按动作（create/update/delete）细分的权限，仅单一 can_manage（Q2）。
- 不做 session 组内共享可见（采用「创建者私有 + owner 可管理」，Q1）。
- 不做组级独立 workspace/memory（维持按用户隔离）。
- 不做角色层级以外的 RBAC/ABAC 通用框架。
