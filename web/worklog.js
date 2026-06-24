"use strict";
// 活动折叠（worklog）的纯逻辑：工具名 -> kind 映射 + 摘要文案生成。
// 移植自 hermes-webui 的 worklog 摘要算法，文案中文化、按 sophagent 工具适配。
// 既可被浏览器 <script> 加载（挂到 window），也可被 node require（测试）。
(function (root) {
  // 工具名 -> kind。显式名表优先，子串兜底（顺序：先 search 再 web，避免 web_search 误判）。
  var NAME_KIND = {
    terminal: "shell", python_exec: "shell",
    read_file: "read", skill_view: "read",
    write_file: "write", edit_file: "write", skill_manage: "write",
    list_dir: "list", skills_list: "list",
    web_search: "search", web_fetch: "web",
    delegate_task: "delegate", memory: "memory",
  };
  function toolKind(name) {
    var n = String(name || "").toLowerCase();
    if (NAME_KIND[n]) return NAME_KIND[n];
    if (!n) return "unknown";
    if (n.indexOf("search") >= 0 || n.indexOf("grep") >= 0 || n.indexOf("find") >= 0) return "search";
    if (n.indexOf("terminal") >= 0 || n.indexOf("shell") >= 0 || n.indexOf("command") >= 0 ||
        n.indexOf("exec") >= 0 || n.indexOf("python") >= 0 || n.indexOf("process") >= 0) return "shell";
    if (n.indexOf("read") >= 0 || n.indexOf("view") >= 0 || n.indexOf("open") >= 0) return "read";
    if (n.indexOf("list") >= 0 || n.indexOf("todo") >= 0) return "list";
    if (n.indexOf("web") >= 0 || n.indexOf("fetch") >= 0 || n.indexOf("curl") >= 0 ||
        n.indexOf("browse") >= 0 || n.indexOf("navigate") >= 0) return "web";
    if (n.indexOf("write") >= 0 || n.indexOf("patch") >= 0 || n.indexOf("edit") >= 0) return "write";
    if (n.indexOf("memory") >= 0) return "memory";
    if (n.indexOf("delegate") >= 0 || n.indexOf("subagent") >= 0) return "delegate";
    return "unknown";
  }

  // kind -> 文案模板。{n} 占位多个时的数量。
  var SUMMARIES = {
    shell:    { done: "运行了 1 条命令", doneMany: "运行了 {n} 条命令", running: "运行命令中", runningMany: "运行 {n} 条命令中" },
    read:     { done: "读取了 1 个文件", doneMany: "读取了 {n} 个文件", running: "读取文件中", runningMany: "读取 {n} 个文件中" },
    write:    { done: "修改了 1 个文件", doneMany: "修改了 {n} 个文件", running: "修改文件中", runningMany: "修改 {n} 个文件中" },
    list:     { done: "列出了目录",     doneMany: "列出了 {n} 次",     running: "列出目录中", runningMany: "列出 {n} 次中" },
    search:   { done: "搜索了 1 次",     doneMany: "搜索了 {n} 次",     running: "搜索中",     runningMany: "搜索 {n} 次中" },
    web:      { done: "访问了 1 个网页", doneMany: "访问了 {n} 个网页", running: "访问网页中", runningMany: "访问 {n} 个网页中" },
    delegate: { done: "委派了 1 个任务", doneMany: "委派了 {n} 个任务", running: "委派任务中", runningMany: "委派 {n} 个任务中" },
    memory:   { done: "更新了记忆",     doneMany: "更新了 {n} 次记忆", running: "更新记忆中", runningMany: "更新记忆中" },
    unknown:  { done: "调用了 1 个工具", doneMany: "调用了 {n} 个工具", running: "调用工具中", runningMany: "调用工具中" },
  };
  var ORDER = ["shell", "read", "write", "search", "web", "list", "delegate", "memory", "unknown"];

  function fmt(tpl, n) { return n === 1 ? tpl.done : tpl.doneMany.replace("{n}", String(n)); }
  function fmtRunning(tpl, n) { return n === 1 ? tpl.running : tpl.runningMany.replace("{n}", String(n)); }

  // state: { hasThinking: bool, live: bool, tools: [{kind, done, error}] }
  function worklogSummary(state) {
    state = state || {};
    var tools = state.tools || [];
    var hasThinking = !!state.hasThinking;
    var live = !!state.live;

    if (!tools.length) {
      if (hasThinking) return live ? "思考中…" : "已思考";
      return live ? "处理中…" : "";
    }

    var runningCounts = {}, doneCounts = {}, failed = 0, anyRunning = false;
    for (var i = 0; i < tools.length; i++) {
      var t = tools[i];
      if (t.done === false) { runningCounts[t.kind] = (runningCounts[t.kind] || 0) + 1; anyRunning = true; }
      else doneCounts[t.kind] = (doneCounts[t.kind] || 0) + 1;
      if (t.error) failed += 1;
    }
    var segs = [];
    var k, n;
    for (var r = 0; r < ORDER.length; r++) {
      k = ORDER[r]; n = runningCounts[k] || 0;
      if (n) segs.push(fmtRunning(SUMMARIES[k] || SUMMARIES.unknown, n));
    }
    for (var d = 0; d < ORDER.length; d++) {
      k = ORDER[d]; n = doneCounts[k] || 0;
      if (n) segs.push(fmt(SUMMARIES[k] || SUMMARIES.unknown, n));
    }
    if (failed) segs.push(failed + " 个失败");
    var body = segs.join("、");
    if (hasThinking) body = "思考并" + body;
    if (live && anyRunning) body += "…";
    return body;
  }

  var api = { toolKind: toolKind, worklogSummary: worklogSummary };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root) { root.toolKind = toolKind; root.worklogSummary = worklogSummary; }
})(typeof window !== "undefined" ? window : null);
