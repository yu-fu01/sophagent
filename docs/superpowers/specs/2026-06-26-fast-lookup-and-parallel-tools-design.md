# 即时查询提速 + 并行工具执行 设计

日期：2026-06-26
分支：fix-web-search

## 背景与问题

用户提问"明天深圳天气如何"时，sophagent 选择 `web_search`，又慢又绕；而参考项目 hermes 让模型直接 `curl wttr.in` 一次秒回。

根因分析：

1. **web_search 默认后端慢**：无 Tavily key 时走 DuckDuckGo HTML 端点（`html.duckduckgo.com/html/`），出了名的慢且常被限流，`FETCH_TIMEOUT = 30s`。
2. **天气类需要两跳**：web_search 只返回 标题+URL+摘要，模型往往还要再 `web_fetch` 一个页面（又一个 30s）才能拿到答案。
3. **没有快路径**：容器是 `python:3.12-slim`，**不含 `curl`/`wget`**，模型即便想学 hermes 用 curl 也跑不了；prompt 里也没有任何"即时事实优先走快路径"的引导。

此外用户确认要补一个独立能力：**并行工具执行**，并兼容模型自发产生的 `multi_tool_use.parallel` 伪工具调用。当前 `loop.py` 的工具分发是串行 `for tc in turn.tool_calls: await dispatch(...)`。

> 说明：`multi_tool_use.parallel` 并非 hermes 的工具（全仓零命中），而是 OpenAI 系模型有时把多个工具调用打包成的伪调用，参数含 `tool_uses` 数组。本设计的任务是**识别并拆解**它，而非"移植"。

## 目标

1. 让"天气/时间/汇率/IP"等即时事实类查询变快（对标 hermes 的 terminal 快路径）。
2. 让 web_search 在确实需要时也不再 30s 级慢。
3. agent loop 支持并行执行同一 turn 内的多个工具调用，并兼容 `multi_tool_use.parallel`。

## 非目标（YAGNI）

- 不移植 hermes 的 2746 行多后端 `terminal_tool.py`（docker/modal/ssh/singularity/daytona、VM 生命周期、process_registry）。sophagent 保持"容器即硬边界"的轻量哲学。
- 不引入有状态/后台任务的 terminal 会话。
- 不做工具级的细粒度并发调度框架，只在 loop 层做一批并发。

---

## 第一块：即时查询提速（方案 C = 快路径 + web_search 提速）

### 1.1 容器装 curl

`Dockerfile` 在 `pip install` 之前加一层 apt 安装 `curl`、`ca-certificates`：

```dockerfile
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
```

理由：启用 hermes 同款 `curl wttr.in` 等即时查询；`ca-certificates` 保证 https 出网。

### 1.2 prompt 引导（新增一段 fast-lookup guide）

在 `sophagent/agent/prompt.py` 新增一段引导，**仅当 agent 启用了 `terminal` 工具时注入**（与现有 `SESSION_SEARCH_GUIDE`/`CRON_GUIDE` 同样的条件注入风格）：

- 即时事实类查询（天气、当前时间/日期换算、汇率、公网 IP、简单的 DNS/HTTP 探测等）**优先用 `terminal` 跑一条轻量命令**，例如：
  - 天气：`curl -s 'wttr.in/深圳?format=3'`（或 `?format=4` 带风/湿度）
  - 公网 IP：`curl -s ifconfig.me`
- `web_search` 留给**真正需要检索多来源网页**的场景，不要用它查能一条命令拿到的即时事实。
- 命令要快失败（自带 `-s --max-time`），不要长时间挂起。

引导文案需中英兼顾（项目 prompt 现状以英文为主，给出中文示例 query 以贴合用户实际用法）。

### 1.3 web_search 提速

改 `sophagent/tools/web.py`：

- 把搜索路径的超时从复用 `FETCH_TIMEOUT(30s)` 改为独立的 `SEARCH_TIMEOUT = 10.0`，快失败。
- DDG HTML 请求显式带 `timeout=SEARCH_TIMEOUT`（当前 `_ddg_search` 用的是 client 默认 30s）。
- `web_fetch` 的 30s 维持不变（抓正文需要更长），但搜索不该被它拖。
- 不改变"有 Tavily key 用 Tavily、否则 DDG"的回退逻辑（Tavily 本就快）。

验收：无 key 情况下 web_search 失败/返回应在 ~10s 内，而非 30s 挂起。

---

## 第二块：并行工具执行 + 兼容 multi_tool_use.parallel

改造点集中在 `sophagent/agent/loop.py` 的 `run()` 工具分发段（当前约 199-206 行）。

### 2.1 拆解伪工具

分发前对 `turn.tool_calls` 做一次展开：

- 若某个 `ToolCall.name == "multi_tool_use.parallel"`，读取其 `arguments["tool_uses"]`（数组，每项形如 `{"recipient_name": "<tool>", "parameters": {...}}` 或 `{"name": ..., "arguments": ...}` —— 解析时两种键都兼容）。
- 把每个子项展开成独立的 `ToolCall`：name 取真实工具名（去掉可能的 `functions.` 前缀），arguments 取其参数，id 用 `f"{parent_id}.{i}"` 这类稳定派生 id。
- 展开后得到一个"扁平化的 tool_calls 列表"，后续按它分发。
- 容错：`tool_uses` 缺失/非数组时，按普通未知工具处理，返回错误文本让模型自纠（沿用现有 dispatch 的容错风格）。

放在 loop 层（而非 provider）：provider 无关，openai/anthropic 都走同一条路。

### 2.2 并行分发

- 用 `asyncio.gather` 并发执行扁平列表里每个调用的 `registry.dispatch(name, args, ctx)`。
- **顺序保持**：gather 保留入参顺序 → 按原顺序把 `tool` 结果消息追加进 history、按原顺序 yield `tool_call` 与 `tool_result` 事件。保证：① 可复现；② 每个真实 tool_call_id（含展开出来的子 id）都有对应 tool 结果消息，满足 OpenAI 协议"每个 tool_call 必须有 tool 回复"。
- `tool_call` 事件仍在执行前 yield（让 UI 先看到调用），`tool_result` 在该调用完成后 yield。实现上：先按顺序 yield 所有 `tool_call` 事件，再 `gather` 执行，最后按顺序 yield 结果并写 history。

### 2.3 并发边界

- 并行最利好只读类（多个 web_search / web_fetch / 文件读 同时跑）。
- 对会写共享状态的工具（memory / files 写 / skills 变更），本期**不做强制串行**，但结果回写严格有序，避免 history 错乱；竞态风险记录在案，后续如出现问题再加"写类工具串行化"白名单。
- 可选并发上限：本期不设硬上限（单 turn 工具数本就有限）；如需要再加 `asyncio.Semaphore`。

---

## 测试计划

- **web_search 提速**：单测 mock httpx，断言搜索路径使用 `SEARCH_TIMEOUT=10`；断言无 key 时走 DDG 分支。
- **伪工具拆解**：构造一个 `ToolCall(name="multi_tool_use.parallel", arguments={"tool_uses":[...]})`，断言展开成正确数量的真实 ToolCall、id 稳定、两种键格式都能解析。
- **并行分发顺序**：mock 两个 dispatch（一个慢一个快），断言 history 中 tool 消息顺序 = 原 tool_calls 顺序（而非完成顺序），且每个 tool_call_id 都有回复。
- **回归**：现有 loop / web 相关测试全绿（`uv run --extra dev pytest`）。
- **Dockerfile**：构建镜像验证 `curl` 可用（手动/CI）。

## 影响面 / 风险

- `loop.py` 是核心循环，改分发段需保证 `done`/`turn_usage`/`error` 等事件路径不受影响；务必保留"无 tool_calls 即 done 返回"的早退分支。
- prompt 引导依赖模型遵循；不保证 100%，但配合 curl 可用 + web_search 提速，整体延迟显著下降。
- Dockerfile 增加 apt 层会略增镜像体积与构建时间（可接受）。
