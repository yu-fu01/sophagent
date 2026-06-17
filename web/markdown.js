"use strict";
// 极简 markdown 渲染器（XSS 安全：先转义再处理）+ LaTeX 数学保护。
// 移植自 index.html 的内联渲染器，并增加：
//   1. 数学 span（$$…$$ / $…$ / \[…\] / \(…\)）先抽取保护，避免被 * / _ / 行格式破坏，
//      且多行 display 数学整块保护、不被按行拆段。
//   2. renderMathInElement：用全局 KaTeX 把 .math span 渲染成公式。
//   3. ~~删除线~~。
// 既可被浏览器 <script> 加载（挂到 window），也可被 node require（测试）。
(function (root) {
  const escapeHtml = s => s.replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

  function mdInline(t) {
    t = escapeHtml(t);
    // 行内代码 `…` 已在 mdToHtml 里预先 stash（见 \x02 占位符），此处不再处理，
    // 以免代码里的 $ / * / _ 被后续行内规则误伤。
    t = t.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
    t = t.replace(/~~([^~]+)~~/g, "<del>$1</del>");
    t = t.replace(/(^|[^*\w])\*([^*\s][^*]*)\*/g, "$1<i>$2</i>");
    t = t.replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
    return t;
  }

  // 数学 span 的 HTML：tex 经转义放入 data-tex，浏览器解码后交给 KaTeX；
  // 标签内同时放转义后的原文作为「KaTeX 未加载」时的兜底显示。
  const mathSpan = ({tex, display}) =>
    `<span class="math" data-display="${display ? 1 : 0}" data-tex="${escapeHtml(tex)}">${escapeHtml(tex)}</span>`;

  function mdToHtml(src) {
    // 1) 保护围栏代码块（在任何其它转换之前）
    const blocks = [];
    src = src.replace(/```[\w-]*\n?([\s\S]*?)(```|$)/g, (m, code) => {
      blocks.push(`<pre class="code"><code>${escapeHtml(code.replace(/\n$/, ""))}</code></pre>`);
      return `\x00${blocks.length - 1}\x00`;
    });

    // 1.5) 保护行内代码 `…`（单行）。必须在数学抽取之前，否则代码里的 $$…$$ / $…$
    //      会被误当数学。围栏代码块已在上一步占位，剩下的反引号即行内代码。
    const inlineCode = [];
    src = src.replace(/`([^`\n]+)`/g, (m, code) => {
      inlineCode.push(code);
      return `\x02${inlineCode.length - 1}\x02`;
    });

    // 2) 保护数学 span。顺序：先 display（$$ / \[），再 inline（\(...\) / $...$）。
    //    代码块（围栏 + 行内）已被占位，故代码里的 $ 不会被当作数学。
    const math = [];
    const stash = (tex, display) => { math.push({tex, display}); return `\x01${math.length - 1}\x01`; };
    src = src.replace(/\$\$([\s\S]+?)\$\$/g, (m, t) => stash(t.trim(), true));
    src = src.replace(/\\\[([\s\S]+?)\\\]/g, (m, t) => stash(t.trim(), true));
    src = src.replace(/\\\(([\s\S]+?)\\\)/g, (m, t) => stash(t.trim(), false));
    // 行内 $...$：内容首尾不得为空白（规避「costs $5 and $6」这类货币误判）；
    // 不满足则原样返回 $…，不当作数学。
    src = src.replace(/\$([^\$\n]+?)\$/g, (m, t) =>
      /^\s|\s$/.test(t) ? m : stash(t, false));

    // 3) 行级块状结构
    const lines = src.split("\n");
    const out = [];
    let para = [], list = null, table = null;
    const flushPara = () => { if (para.length) { out.push("<p>" + para.map(mdInline).join("<br>") + "</p>"); para = []; } };
    const flushList = () => { if (list) { out.push(`<${list.tag}>` + list.items.map(i => `<li>${mdInline(i)}</li>`).join("") + `</${list.tag}>`); list = null; } };
    const flushTable = () => {
      if (!table) return;
      const tr = (cells, tag) => "<tr>" + cells.map(c => `<${tag}>${mdInline(c)}</${tag}>`).join("") + "</tr>";
      out.push("<table>" + tr(table.head, "th") + table.rows.map(r => tr(r, "td")).join("") + "</table>");
      table = null;
    };
    const flushAll = () => { flushPara(); flushList(); flushTable(); };
    for (const raw of lines) {
      const line = raw.replace(/\s+$/, "");
      let m;
      if (/^\x00\d+\x00$/.test(line.trim())) { flushAll(); out.push(blocks[+line.trim().slice(1, -1)]); continue; }
      // 独占一行的数学占位符 -> display 块独立成元素，不并入 <p>
      if (/^\x01\d+\x01$/.test(line.trim())) { flushAll(); out.push("\x01" + line.trim().slice(1, -1) + "\x01"); continue; }
      if (!line.trim()) { flushAll(); continue; }
      if ((m = line.match(/^(#{1,4})\s+(.*)/))) { flushAll(); out.push(`<h${m[1].length}>${mdInline(m[2])}</h${m[1].length}>`); continue; }
      if (/^(-{3,}|\*{3,})$/.test(line.trim())) { flushAll(); out.push("<hr>"); continue; }
      if ((m = line.match(/^>\s?(.*)/))) { flushAll(); out.push(`<blockquote>${mdInline(m[1])}</blockquote>`); continue; }
      if ((m = line.match(/^\s*[-*+]\s+(.*)/))) {
        flushPara(); flushTable();
        if (!list || list.tag !== "ul") { flushList(); list = {tag: "ul", items: []}; }
        list.items.push(m[1]); continue;
      }
      if ((m = line.match(/^\s*\d+[.)]\s+(.*)/))) {
        flushPara(); flushTable();
        if (!list || list.tag !== "ol") { flushList(); list = {tag: "ol", items: []}; }
        list.items.push(m[1]); continue;
      }
      if (/^\s*\|.*\|\s*$/.test(line)) {
        flushPara(); flushList();
        const cells = line.trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim());
        if (!table) table = {head: cells, rows: []};
        else if (!cells.every(c => /^:?-{2,}:?$/.test(c))) table.rows.push(cells);
        continue;
      }
      flushList(); flushTable();
      para.push(line);
    }
    flushAll();

    // 4) 还原占位符：数学（行内 / 独占块）与行内代码一并替换。
    //    行内代码内容此时才转义，保证 `<` `&` 等原样显示且不破坏 HTML。
    return out.join("\n")
      .replace(/\x01(\d+)\x01/g, (m, i) => mathSpan(math[+i]))
      .replace(/\x02(\d+)\x02/g, (m, i) => `<code>${escapeHtml(inlineCode[+i])}</code>`);
  }

  // 用全局 KaTeX 渲染容器内所有 .math span。KaTeX 未加载则保留兜底原文。
  function renderMathInElement(el) {
    if (typeof katex === "undefined" || !el || !el.querySelectorAll) return;
    for (const n of el.querySelectorAll(".math")) {
      const tex = n.dataset.tex;
      if (tex == null) continue;
      try {
        katex.render(tex, n, {
          displayMode: n.dataset.display === "1",
          throwOnError: false,
          output: "html",
        });
      } catch (e) { /* 保留兜底文本 */ }
    }
  }

  const api = { escapeHtml, mdInline, mdToHtml, renderMathInElement };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root) Object.assign(root, api);
})(typeof window !== "undefined" ? window : null);
