# 即时查询提速 + 并行工具执行 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 让天气等即时查询走 curl 快路径并把 web_search 从 30s 提速到 10s；让 agent loop 支持并行工具执行并兼容 `multi_tool_use.parallel` 伪工具。

**架构：** 三处独立改动 + 一处核心循环改造。① `web.py` 搜索超时独立为 10s；② `prompt.py` 在启用 terminal 时注入"即时查询优先 curl"引导；③ `Dockerfile` 装 curl；④ `loop.py` 新增纯函数 `expand_parallel_calls` 拆解伪工具，分发段改为 `asyncio.gather` 并发且按原序回写。

**技术栈：** Python 3.12、httpx、asyncio、pytest（`uv run --extra dev pytest`，asyncio_mode=auto）。

规格：`docs/superpowers/specs/2026-06-26-fast-lookup-and-parallel-tools-design.md`

## 文件结构

- 修改：`sophagent/tools/web.py` — 搜索路径用独立 `SEARCH_TIMEOUT=10.0`，快失败。
- 修改：`sophagent/agent/prompt.py` — 新增 `FAST_LOOKUP_GUIDE`，启用 terminal 时注入。
- 修改：`Dockerfile` — apt 装 `curl`、`ca-certificates`。
- 修改：`sophagent/agent/loop.py` — 新增模块级 `expand_parallel_calls()` 并导出；改造 `run()` 工具分发段为并行。
- 测试：`tests/test_tools.py`（web_search 超时）、`tests/test_loop.py`（prompt 引导、拆解、并行顺序）。

---

### 任务 1：web_search 搜索超时独立为 10s

**文件：**
- 修改：`sophagent/tools/web.py:17-18`（新增常量）、`:102-122`（两个搜索函数）
- 测试：`tests/test_tools.py`

- [ ] **步骤 1：编写失败的测试**

在 `tests/test_tools.py` 末尾追加：

```python
async def test_web_search_uses_short_timeout(ctx, monkeypatch):
    """无 Tavily key 时 web_search 走 DDG，且用 10s 而非 30s 超时（快失败）。"""
    import sophagent.tools.web as web

    captured = {}

    class FakeResp:
        text = '<a class="result__a" href="http://x.com">Title</a>'

    class FakeClient:
        def __init__(self, *a, **k):
            captured["timeout"] = k.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return FakeResp()

    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr(web.httpx, "AsyncClient", FakeClient)
    out = await web.web_search(ctx, "shenzhen weather")
    assert captured["timeout"] == web.SEARCH_TIMEOUT
    assert web.SEARCH_TIMEOUT == 10.0
    assert "Title" in out
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run --extra dev pytest tests/test_tools.py::test_web_search_uses_short_timeout -v`
预期：FAIL，`AttributeError: module 'sophagent.tools.web' has no attribute 'SEARCH_TIMEOUT'`

- [ ] **步骤 3：编写最少实现代码**

在 `sophagent/tools/web.py` 的 `FETCH_TIMEOUT = 30.0` 下一行新增：

```python
SEARCH_TIMEOUT = 10.0
```

把 `_ddg_search` 的 client 构造改为带显式超时：

```python
async def _ddg_search(query: str, count: int) -> str:
    async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT, headers={"User-Agent": UA}) as client:
        resp = await client.post("https://html.duckduckgo.com/html/", data={"q": query})
```

把 `_tavily_search` 的 client 构造改为：

```python
async def _tavily_search(query: str, count: int, api_key: str) -> str:
    async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT) as client:
```

（`web_fetch` 的 `FETCH_TIMEOUT=30s` 保持不变——抓正文需要更久。）

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run --extra dev pytest tests/test_tools.py::test_web_search_uses_short_timeout -v`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add sophagent/tools/web.py tests/test_tools.py
git commit -m "perf(web): web_search 搜索超时独立为 10s 快失败"
```

---

### 任务 2：prompt 注入即时查询快路径引导

**文件：**
- 修改：`sophagent/agent/prompt.py`（新增常量 + 注入条件）
- 测试：`tests/test_loop.py`

- [ ] **步骤 1：编写失败的测试**

在 `tests/test_loop.py` 末尾追加：

```python
def test_fast_lookup_guide_injected_with_terminal(ctx):
    """启用 terminal 时注入快路径引导；未启用则不注入。"""
    from sophagent.agent.prompt import build_system_prompt

    ctx.agent.tools = ["terminal"]
    assert "Fast lookups" in build_system_prompt(ctx.agent)

    ctx.agent.tools = ["read_file"]
    assert "Fast lookups" not in build_system_prompt(ctx.agent)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run --extra dev pytest tests/test_loop.py::test_fast_lookup_guide_injected_with_terminal -v`
预期：FAIL（断言找不到 "Fast lookups"）

- [ ] **步骤 3：编写最少实现代码**

在 `sophagent/agent/prompt.py` 的 `CRON_GUIDE = """..."""` 定义之后、`build_system_prompt` 之前新增：

```python
FAST_LOOKUP_GUIDE = """\
## Fast lookups
For instant factual queries — weather, current time/date math, exchange rates,
your public IP, a quick DNS/HTTP check — prefer running ONE lightweight shell
command via the `terminal` tool instead of `web_search`. Examples:
- Weather: `curl -s 'wttr.in/深圳?format=3'` (use ?format=4 for wind/humidity)
- Public IP: `curl -s ifconfig.me`
Pass `--max-time 10` so the command fails fast. Reserve `web_search` for genuine
multi-source web research that a single command can't answer."""
```

在 `build_system_prompt` 内、`CRON_GUIDE` 注入块之后新增（紧跟 `if "cron" in agent.tools:` 块）：

```python
    if "terminal" in agent.tools:
        parts.append(FAST_LOOKUP_GUIDE)
```

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run --extra dev pytest tests/test_loop.py::test_fast_lookup_guide_injected_with_terminal -v`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add sophagent/agent/prompt.py tests/test_loop.py
git commit -m "feat(prompt): 即时查询优先 curl 快路径引导(启用 terminal 时)"
```

---

### 任务 3：Dockerfile 安装 curl

**文件：**
- 修改：`Dockerfile`（在 `RUN useradd` 之后、`WORKDIR` 之前加 apt 层）

> 说明：本任务无单测，验收靠镜像构建。容器是 `python:3.12-slim`，默认无 curl。

- [ ] **步骤 1：修改 Dockerfile**

在 `Dockerfile` 的 `RUN useradd -m -u 1000 app` 之后新增一层：

```dockerfile
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
```

- [ ] **步骤 2：构建镜像验证 curl 可用**

运行：
```bash
docker build -t sophagent:curltest . \
  && docker run --rm --entrypoint curl sophagent:curltest --version
```
预期：打印 `curl x.y.z ...` 版本信息，退出码 0。

> 若本机无 docker，记录为待人工验证，跳过本步骤但仍提交 Dockerfile 改动。

- [ ] **步骤 3：Commit**

```bash
git add Dockerfile
git commit -m "build(docker): 安装 curl 与 ca-certificates 以支持即时查询快路径"
```

---

### 任务 4：拆解 multi_tool_use.parallel 伪工具（纯函数）

**文件：**
- 修改：`sophagent/agent/loop.py`（新增模块级 `PARALLEL_TOOL` 常量、`expand_parallel_calls` 函数、加入 `__all__`）
- 测试：`tests/test_loop.py`

- [ ] **步骤 1：编写失败的测试**

在 `tests/test_loop.py` 末尾追加：

```python
def test_expand_parallel_passthrough():
    """普通工具调用原样返回。"""
    from sophagent.agent.loop import expand_parallel_calls

    calls = [ToolCall(id="a", name="web_search", arguments={"query": "x"})]
    assert expand_parallel_calls(calls) == calls


def test_expand_parallel_unwraps_both_key_forms():
    """multi_tool_use.parallel 展开为真实调用，兼容 recipient_name/name 两种键，
    剥离命名空间前缀，生成稳定派生 id。"""
    from sophagent.agent.loop import expand_parallel_calls

    parent = ToolCall(
        id="p",
        name="multi_tool_use.parallel",
        arguments={"tool_uses": [
            {"recipient_name": "functions.web_search", "parameters": {"query": "a"}},
            {"name": "web_fetch", "arguments": {"url": "u"}},
        ]},
    )
    out = expand_parallel_calls([parent])
    assert [c.name for c in out] == ["web_search", "web_fetch"]
    assert [c.id for c in out] == ["p.0", "p.1"]
    assert out[0].arguments == {"query": "a"}
    assert out[1].arguments == {"url": "u"}


def test_expand_parallel_malformed_passthrough():
    """tool_uses 缺失/非数组时保留原调用，交给 dispatch 报未知工具。"""
    from sophagent.agent.loop import expand_parallel_calls

    bad = ToolCall(id="p", name="multi_tool_use.parallel", arguments={})
    assert expand_parallel_calls([bad]) == [bad]
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run --extra dev pytest tests/test_loop.py -k expand_parallel -v`
预期：FAIL，`ImportError: cannot import name 'expand_parallel_calls'`

- [ ] **步骤 3：编写最少实现代码**

在 `sophagent/agent/loop.py` 的 `log = logging.getLogger(__name__)` 之后新增：

```python
PARALLEL_TOOL = "multi_tool_use.parallel"


def expand_parallel_calls(tool_calls: list[ToolCall]) -> list[ToolCall]:
    """Flatten any multi_tool_use.parallel pseudo-call into real ToolCalls.

    GPT-family models sometimes bundle parallel tool calls into a single call
    named ``multi_tool_use.parallel`` whose arguments carry a ``tool_uses`` list.
    Each item names a tool (``recipient_name`` or ``name``) and its parameters
    (``parameters`` or ``arguments``). We expand those into individual ToolCalls
    so the normal dispatch path runs them. Malformed wrappers are left as-is so
    dispatch returns an error the model can self-correct.
    """
    out: list[ToolCall] = []
    for tc in tool_calls:
        if tc.name != PARALLEL_TOOL:
            out.append(tc)
            continue
        uses = tc.arguments.get("tool_uses")
        if not isinstance(uses, list):
            out.append(tc)  # malformed → dispatch reports unknown tool
            continue
        for i, use in enumerate(uses):
            if not isinstance(use, dict):
                continue
            name = (use.get("recipient_name") or use.get("name") or "").split(".")[-1]
            args = use.get("parameters")
            if args is None:
                args = use.get("arguments")
            out.append(ToolCall(id=f"{tc.id}.{i}", name=name, arguments=args or {}))
    return out
```

把 `expand_parallel_calls` 加入文件顶部的 `__all__` 列表：

```python
__all__ = [
    "AgentRunner",
    "expand_parallel_calls",
    "estimate_tokens",
    "history_tokens",
    "truncate_old_tool_messages",
    "KEEP_RECENT_TOOL_MSGS",
    "TOOL_TRUNCATE_NOTE",
]
```

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run --extra dev pytest tests/test_loop.py -k expand_parallel -v`
预期：3 个测试全 PASS

- [ ] **步骤 5：Commit**

```bash
git add sophagent/agent/loop.py tests/test_loop.py
git commit -m "feat(loop): 拆解 multi_tool_use.parallel 伪工具为真实调用"
```

---

### 任务 5：loop 工具分发改为并行执行

**文件：**
- 修改：`sophagent/agent/loop.py:199-206`（`run()` 工具分发段）
- 测试：`tests/test_loop.py`

- [ ] **步骤 1：编写失败的测试**

在 `tests/test_loop.py` 末尾追加：

```python
async def test_parallel_dispatch_preserves_order(ctx, fake_provider, monkeypatch):
    """并发执行多个工具，但 history 中 tool 消息按原调用顺序排列（非完成顺序）。"""
    import asyncio

    from sophagent.agent import loop as loop_mod

    async def fake_dispatch(name, args, c):
        await asyncio.sleep(args.get("delay", 0.0))
        return f"done:{name}"

    monkeypatch.setattr(loop_mod.registry, "dispatch", fake_dispatch)
    turn = AssistantTurn(content="", stop_reason="tool_calls", tool_calls=[
        ToolCall(id="a", name="slow", arguments={"delay": 0.05}),
        ToolCall(id="b", name="fast", arguments={"delay": 0.0}),
    ])
    fake_provider([turn, AssistantTurn(content="ok", stop_reason="stop")])
    runner = AgentRunner(ctx.agent, ctx, history=[])
    await collect(runner, "go")
    tool_msgs = [m for m in runner.history if m.role == "tool"]
    assert [m.tool_call_id for m in tool_msgs] == ["a", "b"]
    assert tool_msgs[0].content == "done:slow"


async def test_parallel_pseudo_tool_end_to_end(ctx, fake_provider, monkeypatch):
    """模型发来的 multi_tool_use.parallel 被拆解并各产出一条 tool 结果消息。"""
    from sophagent.agent import loop as loop_mod

    async def fake_dispatch(name, args, c):
        return f"ran:{name}"

    monkeypatch.setattr(loop_mod.registry, "dispatch", fake_dispatch)
    turn = AssistantTurn(content="", stop_reason="tool_calls", tool_calls=[
        ToolCall(id="p", name="multi_tool_use.parallel", arguments={"tool_uses": [
            {"recipient_name": "web_search", "parameters": {"query": "a"}},
            {"recipient_name": "web_fetch", "parameters": {"url": "u"}},
        ]}),
    ])
    fake_provider([turn, AssistantTurn(content="ok", stop_reason="stop")])
    runner = AgentRunner(ctx.agent, ctx, history=[])
    await collect(runner, "go")
    tool_msgs = [m for m in runner.history if m.role == "tool"]
    assert [m.tool_call_id for m in tool_msgs] == ["p.0", "p.1"]
    assert tool_msgs[0].content == "ran:web_search"
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run --extra dev pytest tests/test_loop.py -k "parallel_dispatch or pseudo_tool" -v`
预期：FAIL —— `test_parallel_pseudo_tool_end_to_end` 当前串行路径会把伪工具整体丢给 dispatch（fake_dispatch 返回 `ran:multi_tool_use.parallel`，只有 1 条 tool 消息且 id 为 `p`），断言不符。

- [ ] **步骤 3：编写实现代码**

把 `sophagent/agent/loop.py` `run()` 中的工具分发段（当前）：

```python
            for tc in turn.tool_calls:
                yield {"type": "tool_call", "id": tc.id, "name": tc.name, "arguments": tc.arguments}
                result = await registry.dispatch(tc.name, tc.arguments, self.ctx)
                tool_msg = Message(role="tool", content=result, tool_call_id=tc.id)
                self.history.append(tool_msg)
                await self._persist(tool_msg)
                yield {"type": "tool_result", "id": tc.id, "name": tc.name,
                       "preview": result[:500] + ("..." if len(result) > 500 else "")}
```

替换为：

```python
            calls = expand_parallel_calls(turn.tool_calls)
            # 先按原序广播调用事件，让 UI 立刻看到全部调用
            for tc in calls:
                yield {"type": "tool_call", "id": tc.id, "name": tc.name, "arguments": tc.arguments}
            # 并发执行（dispatch 自身吞异常返回文本，gather 不会抛）
            results = await asyncio.gather(
                *(registry.dispatch(tc.name, tc.arguments, self.ctx) for tc in calls)
            )
            # 严格按原序回写 history 并广播结果，保证可复现且每个 tool_call_id 都有回复
            for tc, result in zip(calls, results):
                tool_msg = Message(role="tool", content=result, tool_call_id=tc.id)
                self.history.append(tool_msg)
                await self._persist(tool_msg)
                yield {"type": "tool_result", "id": tc.id, "name": tc.name,
                       "preview": result[:500] + ("..." if len(result) > 500 else "")}
```

（`asyncio` 已在文件顶部导入，无需新增 import。）

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run --extra dev pytest tests/test_loop.py -k "parallel_dispatch or pseudo_tool" -v`
预期：2 个测试全 PASS

- [ ] **步骤 5：Commit**

```bash
git add sophagent/agent/loop.py tests/test_loop.py
git commit -m "feat(loop): 工具调用并行分发(asyncio.gather)且按原序回写"
```

---

### 任务 6：全量回归

- [ ] **步骤 1：跑全量测试**

运行：`uv run --extra dev pytest`
预期：全绿（含既有 loop / tools / web 相关测试）。

- [ ] **步骤 2：如有红，定位修复**

若 `loop.py` 改动影响了既有 tool_call 顺序/事件断言，回到任务 5 检查事件 yield 顺序与 history 写入顺序是否与原行为一致（单工具场景必须与改前完全等价）。

---

## 自检

**1. 规格覆盖度：**
- 第一块 1.1 容器装 curl → 任务 3 ✅
- 第一块 1.2 prompt 引导 → 任务 2 ✅
- 第一块 1.3 web_search 提速 → 任务 1 ✅
- 第二块 2.1 拆伪工具 → 任务 4 ✅
- 第二块 2.2 并行分发 + 顺序保持 → 任务 5 ✅
- 第二块 2.3 并发边界（写类不强制串行、不设硬上限）→ 当前实现不引入串行化白名单，符合"本期不做"；无需任务 ✅
- 测试计划各项 → 任务 1/4/5 测试 + 任务 6 回归 ✅

**2. 占位符扫描：** 无 TODO/待定；每个代码步骤均含完整代码。✅

**3. 类型一致性：** `expand_parallel_calls(list[ToolCall]) -> list[ToolCall]` 在任务 4 定义、任务 5 调用，签名一致；`ToolCall(id,name,arguments)`、`AssistantTurn(content,stop_reason,tool_calls)`、`Message(role,content,tool_call_id)` 与 `sophagent/models.py` 一致。✅
