"use strict";
// markdown 渲染器（含 LaTeX 数学保护）的纯逻辑测试。run: node --test tests/test_markdown.cjs
// 只测 mdToHtml 的 HTML 结构（数学 span 抽取、行内格式），不依赖浏览器/KaTeX。
const test = require("node:test");
const assert = require("node:assert");
const { mdToHtml, escapeHtml } = require("../web/markdown.js");

// ---- 基本行内格式 ----------------------------------------------------------
test("inline code / bold / italic / link survive", () => {
  const html = mdToHtml("`c` **b** *i* [t](http://x)");
  assert.match(html, /<code>c<\/code>/);
  assert.match(html, /<b>b<\/b>/);
  assert.match(html, /<i>i<\/i>/);
  assert.match(html, /<a href="http:\/\/x" target="_blank" rel="noopener">t<\/a>/);
});

test("strikethrough ~~x~~ -> <del>", () => {
  assert.match(mdToHtml("~~done~~"), /<del>done<\/del>/);
});

// ---- 内联数学 $...$ ---------------------------------------------------------
test("inline math becomes a .math span with raw tex", () => {
  const html = mdToHtml("求 $a^2 + b^2 = c^2$ 的解");
  assert.match(html, /<span class="math"[^>]*data-tex="a\^2 \+ b\^2 = c\^2"[^>]*>.*?<\/span>/);
  assert.match(html, /data-display="0"/);
  // 不应残留裸 $
  assert.doesNotMatch(html, /\$a\^2/);
});

test("inline math with star is NOT mangled into <i> (regression)", () => {
  // 这是根因之一：旧渲染器的斜体正则会把 $a * b$ 破坏成 $a <i>b</i>$
  const html = mdToHtml("$a * b * c$");
  assert.doesNotMatch(html, /<i>/);
  assert.match(html, /data-tex="a \* b \* c"/);
});

test("inline math with < and > keeps relation in tex (HTML-escaped in attr)", () => {
  const html = mdToHtml("$x < y$");
  assert.match(html, /data-tex="x &lt; y"/);
});

// ---- display 数学 $$...$$ --------------------------------------------------
test("display math is data-display=1 and not split across <p>", () => {
  const html = mdToHtml("行前\n$$\nE = mc^2\n$$\n行后");
  assert.match(html, /data-display="1"/);
  assert.match(html, /data-tex="E = mc\^2"/);
  // display 块独立成元素，不应被 <br> 拆进同一个 <p>
  assert.doesNotMatch(html, /<p>.*\x01.*<\/p>/s);
});

// ---- \(...\) 与 \[...\] ----------------------------------------------------
test("\\(...\\) inline and \\[...\\] display delimiters", () => {
  assert.match(mdToHtml("\\(x+1\\)"), /data-display="0"[^>]*data-tex="x\+1"/);
  assert.match(mdToHtml("\\[x+1\\]"), /data-display="1"[^>]*data-tex="x\+1"/);
});

// ---- 代码块里的 $ 不被当数学 ----------------------------------------------
test("dollar inside fenced code is not treated as math", () => {
  const html = mdToHtml("```\ncost $5\n```");
  assert.match(html, /<pre class="code"><code>cost \$5<\/code><\/pre>/);
  assert.doesNotMatch(html, /class="math"/);
});

// ---- 行内反引号代码里的 $ 不被当数学（回归：`$$ ... $$` 字面量） ----------
test("dollar inside inline backtick code is not treated as math", () => {
  const html = mdToHtml("可用 `$$ ... $$` 表示块级公式");
  // 反引号内应是字面代码，原样保留 $$，不得抽成 .math
  assert.doesNotMatch(html, /class="math"/);
  assert.match(html, /<code>\$\$ \.\.\. \$\$<\/code>/);
});

test("inline math outside backticks still works alongside backtick code", () => {
  const html = mdToHtml("行内 $x^2$ 和字面 `$y$`");
  // $x^2$ 抽成数学，`$y$` 保持字面代码
  assert.match(html, /data-tex="x\^2"/);
  assert.match(html, /<code>\$y\$<\/code>/);
});

// ---- 货币误判防护 ----------------------------------------------------------
test("lone currency $5 and $6 not misread as math", () => {
  const html = mdToHtml("costs $5 and $6 today");
  assert.doesNotMatch(html, /class="math"/);
  // 文本应原样保留（$ 符号仍在）
  assert.match(html, /\$5/);
});

// ---- escapeHtml 直接校验 ---------------------------------------------------
test("escapeHtml escapes the five chars", () => {
  assert.equal(escapeHtml(`<a>&"'`), "&lt;a&gt;&amp;&quot;&#39;");
});
