"use strict";
// 活动折叠（worklog）摘要算法的纯逻辑测试。run: node --test tests/test_worklog.cjs
const test = require("node:test");
const assert = require("node:assert");
const { toolKind, worklogSummary } = require("../web/worklog.js");

// ---- toolKind：工具名 -> kind 映射 ------------------------------------------
test("toolKind maps sophclaw tools to kinds", () => {
  assert.equal(toolKind("terminal"), "shell");
  assert.equal(toolKind("python_exec"), "shell");
  assert.equal(toolKind("read_file"), "read");
  assert.equal(toolKind("skill_view"), "read");
  assert.equal(toolKind("write_file"), "write");
  assert.equal(toolKind("edit_file"), "write");
  assert.equal(toolKind("skill_manage"), "write");
  assert.equal(toolKind("list_dir"), "list");
  assert.equal(toolKind("skills_list"), "list");
  assert.equal(toolKind("web_search"), "search");
  assert.equal(toolKind("web_fetch"), "web");
  assert.equal(toolKind("delegate_task"), "delegate");
  assert.equal(toolKind("memory"), "memory");
  assert.equal(toolKind("totally_unknown_tool"), "unknown");
});

// ---- worklogSummary：单工具完成 ---------------------------------------------
test("single done shell command", () => {
  assert.equal(
    worklogSummary({ hasThinking: false, live: false, tools: [{ kind: "shell", done: true }] }),
    "运行了 1 条命令");
});

test("single done read", () => {
  assert.equal(
    worklogSummary({ hasThinking: false, live: false, tools: [{ kind: "read", done: true }] }),
    "读取了 1 个文件");
});

// ---- 多工具按 order 合并，用「、」连接 --------------------------------------
test("two commands + one read, joined with Chinese comma", () => {
  assert.equal(
    worklogSummary({ hasThinking: false, live: false, tools: [
      { kind: "shell", done: true }, { kind: "shell", done: true }, { kind: "read", done: true },
    ] }),
    "运行了 2 条命令、读取了 1 个文件");
});

// ---- 思考前缀 ---------------------------------------------------------------
test("thinking prefix prepends 思考并", () => {
  assert.equal(
    worklogSummary({ hasThinking: true, live: false, tools: [
      { kind: "shell", done: true }, { kind: "shell", done: true }, { kind: "read", done: true },
    ] }),
    "思考并运行了 2 条命令、读取了 1 个文件");
});

// ---- 进行中（live + 有未完成工具）-> running 文案 + 省略号 ------------------
test("running command while live shows running text with ellipsis", () => {
  assert.equal(
    worklogSummary({ hasThinking: false, live: true, tools: [{ kind: "shell", done: false }] }),
    "运行命令中…");
});

test("running many commands while live", () => {
  assert.equal(
    worklogSummary({ hasThinking: false, live: true, tools: [
      { kind: "shell", done: false }, { kind: "shell", done: false },
    ] }),
    "运行 2 条命令中…");
});

// ---- 失败计数 ---------------------------------------------------------------
test("failed tool appends 个失败", () => {
  assert.equal(
    worklogSummary({ hasThinking: false, live: false, tools: [{ kind: "shell", done: true, error: true }] }),
    "运行了 1 条命令、1 个失败");
});

// ---- 纯思考（无工具）-------------------------------------------------------
test("thinking only, live shows 思考中…", () => {
  assert.equal(
    worklogSummary({ hasThinking: true, live: true, tools: [] }),
    "思考中…");
});

test("thinking only, done shows 已思考", () => {
  assert.equal(
    worklogSummary({ hasThinking: true, live: false, tools: [] }),
    "已思考");
});
