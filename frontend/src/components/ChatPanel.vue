<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from "vue";
import { sessionsApi, uploadFile } from "../api/client";
import type { Agent, Message } from "../types";

const props = defineProps<{
  sessionId: string;
  agents: Agent[];
  selectedAgentId: number | null;
}>();

const emit = defineEmits<{ sessionUpdated: [] }>();

interface ToolRow {
  id?: string;
  name: string;
  args: string;
  result?: string;
  done: boolean;
  error: boolean;
}

interface WorklogItem {
  kind: "worklog";
  reasoning: string;
  tools: ToolRow[];
  live: boolean;
  open: boolean;
}

interface TextItem {
  kind: "user" | "assistant" | "error" | "turnmeta";
  content: string;
}

type DisplayItem = WorklogItem | TextItem;

const items = ref<DisplayItem[]>([]);
const input = ref("");
const streaming = ref(false);
const usage = ref({ inTok: 0, outTok: 0, ctx: "0/0", cache: "—" });
const attached = ref<Array<{ name: string; path: string }>>([]);
const messagesEl = ref<HTMLElement | null>(null);

const currentAgent = computed(
  () => props.agents.find(a => a.id === props.selectedAgentId) ?? null
);

const mdToHtml = (text: string) => window.mdToHtml?.(text) ?? text;
const worklogSummary = (state: {
  hasThinking: boolean;
  live: boolean;
  tools: Array<{ kind: string; done: boolean; error: boolean }>;
}) => window.worklogSummary?.(state) ?? "处理中…";
const toolKind = (name: string) => window.toolKind?.(name) ?? "unknown";

function scrollBottom() {
  if (messagesEl.value) messagesEl.value.scrollTop = messagesEl.value.scrollHeight;
}

function wlSummary(wl: WorklogItem) {
  return worklogSummary({
    hasThinking: !!wl.reasoning,
    live: wl.live,
    tools: wl.tools.map(t => ({
      kind: toolKind(t.name),
      done: t.done,
      error: t.error,
    })),
  });
}

function buildHistory(messages: Message[]) {
  const out: DisplayItem[] = [];
  for (const m of messages) {
    if (m.role === "user") {
      out.push({ kind: "user", content: m.content });
    } else if (m.role === "assistant") {
      const tcs = m.tool_calls || [];
      if (m.reasoning || tcs.length) {
        out.push({
          kind: "worklog",
          reasoning: m.reasoning || "",
          tools: tcs.map(tc => ({
            id: tc.id,
            name: tc.name,
            args: JSON.stringify(tc.arguments, null, 2),
            done: true,
            error: false,
          })),
          live: false,
          open: false,
        });
      }
      if (m.content) out.push({ kind: "assistant", content: m.content });
    }
  }
  return out;
}

async function loadSession() {
  const detail = await sessionsApi.get(props.sessionId);
  items.value = buildHistory(
    detail.messages.filter(
      m => !(m.role === "user" && m.content.startsWith("[Earlier conversation summary]"))
    )
  );
  await nextTick();
  scrollBottom();
}

async function onFileChange(ev: Event) {
  const inputEl = ev.target as HTMLInputElement;
  const file = inputEl.files?.[0];
  inputEl.value = "";
  if (!file) return;
  try {
    const r = await uploadFile(file);
    attached.value.push({ name: r.name, path: r.path });
  } catch (e) {
    alert("上传失败: " + (e instanceof Error ? e.message : String(e)));
  }
}

function removeChip(i: number) {
  attached.value.splice(i, 1);
}

function ensureLiveWorklog(): WorklogItem {
  const last = items.value[items.value.length - 1];
  if (last?.kind === "worklog" && last.live) return last;
  const wl: WorklogItem = {
    kind: "worklog",
    reasoning: "",
    tools: [],
    live: true,
    open: true,
  };
  items.value.push(wl);
  return wl;
}

function closeLiveWorklog() {
  const last = items.value[items.value.length - 1];
  if (last?.kind === "worklog" && last.live) {
    last.live = false;
    last.open = false;
  }
}

function hasAssistantBubble() {
  const last = items.value[items.value.length - 1];
  return last?.kind === "assistant";
}

async function send() {
  const text = input.value.trim();
  if ((!text && !attached.value.length) || streaming.value) return;
  const refs = attached.value.map(a => `[附加文件: ${a.path}]`).join("\n");
  const content = refs ? (refs + (text ? "\n\n" + text : "")) : text;
  attached.value = [];
  input.value = "";
  streaming.value = true;
  items.value.push({ kind: "user", content });

  let assistantText = "";

  try {
    const resp = await sessionsApi.chat(props.sessionId, content);
    const reader = resp.body!.getReader();
    const dec = new TextDecoder();
    let buf = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const line = buf.slice(0, idx).trim();
        buf = buf.slice(idx + 2);
        if (!line.startsWith("data: ")) continue;
        const ev = JSON.parse(line.slice(6));

        if (ev.type === "reasoning_delta") {
          if (hasAssistantBubble()) continue;
          const wl = ensureLiveWorklog();
          wl.reasoning += ev.text;
        } else if (ev.type === "text_delta") {
          if (!hasAssistantBubble()) closeLiveWorklog();
          if (!hasAssistantBubble()) {
            items.value.push({ kind: "assistant", content: "" });
          }
          assistantText += ev.text;
          const last = items.value[items.value.length - 1];
          if (last?.kind === "assistant") last.content = assistantText;
        } else if (ev.type === "tool_call") {
          if (hasAssistantBubble()) {
            items.value.push({ kind: "assistant", content: assistantText });
            assistantText = "";
          }
          const wl = ensureLiveWorklog();
          wl.tools.push({
            id: ev.id,
            name: ev.name,
            args: JSON.stringify(ev.arguments, null, 2),
            done: false,
            error: false,
          });
        } else if (ev.type === "tool_result") {
          const wl = [...items.value].reverse().find(i => i.kind === "worklog") as WorklogItem | undefined;
          if (wl) {
            const row = wl.tools.find(t => t.id === ev.id) || wl.tools[wl.tools.length - 1];
            if (row) {
              row.result = ev.preview;
              row.done = true;
            }
          }
        } else if (ev.type === "error" && ev.message !== "stopped by user") {
          closeLiveWorklog();
          items.value.push({ kind: "error", content: ev.message });
          assistantText = "";
        } else if (ev.type === "turn_usage") {
          closeLiveWorklog();
          items.value.push({
            kind: "turnmeta",
            content: `本轮 ↑${ev.input_tokens} ↓${ev.output_tokens}` +
              (ev.cache_hit != null ? ` · 缓存 ${ev.cache_hit}%` : ""),
          });
          assistantText = "";
        } else if (ev.type === "done") {
          const u = ev.usage || {};
          usage.value = {
            inTok: u.input_tokens || 0,
            outTok: u.output_tokens || 0,
            ctx: `${ev.context_length || 0}/${ev.context_limit || 0}`,
            cache: ev.cache_hit != null ? `${ev.cache_hit}%` : "—",
          };
        }
        await nextTick();
        scrollBottom();
      }
    }
  } catch (e) {
    items.value.push({
      kind: "error",
      content: e instanceof Error ? e.message : String(e),
    });
  } finally {
    closeLiveWorklog();
    streaming.value = false;
    await loadSession();
    emit("sessionUpdated");
  }
}

async function stopTurn() {
  await sessionsApi.stop(props.sessionId);
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    send();
  }
}

watch(() => props.sessionId, loadSession, { immediate: true });
onMounted(loadSession);
</script>

<template>
  <main class="main">
    <header class="chat-header">
      <div>
        <h2>{{ currentAgent?.name || "对话" }}</h2>
        <div class="meta">{{ currentAgent?.provider }} / {{ currentAgent?.model }}</div>
      </div>
      <div class="usage-bar">
        <span>↑{{ usage.inTok }} ↓{{ usage.outTok }}</span>
        <span>ctx {{ usage.ctx }}</span>
        <span>cache {{ usage.cache }}</span>
      </div>
    </header>

    <div class="chat-body" :class="{ streaming }">
      <div ref="messagesEl" class="messages">
        <template v-for="(item, i) in items" :key="i">
          <div v-if="item.kind === 'user'" class="msg user">{{ item.content }}</div>

          <details
            v-else-if="item.kind === 'worklog'"
            class="worklog"
            :open="item.open"
          >
            <summary>
              <span class="wl-caret">▸</span>
              <span class="wl-sum">{{ wlSummary(item) }}</span>
            </summary>
            <div class="wl-body">
              <div v-if="item.reasoning" class="thinking-card">
                <div class="tc-label">💡 思考</div>
                <div class="think-body" v-html="mdToHtml(item.reasoning)" />
              </div>
              <details
                v-for="(tool, ti) in item.tools"
                :key="ti"
                class="tool-card-row"
                :class="{ err: tool.error }"
              >
                <summary>
                  🔧 {{ tool.name }}
                  <template v-if="tool.args"> · {{ tool.args.split('\n')[0].slice(0, 80) }}</template>
                </summary>
                <pre>{{ tool.args }}{{ tool.result ? '\n→ ' + tool.result : '' }}</pre>
              </details>
            </div>
          </details>

          <div
            v-else-if="item.kind === 'assistant'"
            class="msg assistant"
            v-html="mdToHtml(item.content)"
          />
          <div v-else-if="item.kind === 'error'" class="msg err">{{ item.content }}</div>
          <div v-else-if="item.kind === 'turnmeta'" class="turnmeta">{{ item.content }}</div>
        </template>
      </div>

      <div class="composer">
        <div v-if="attached.length" class="chips">
          <span v-for="(a, idx) in attached" :key="a.path" class="chip">
            📎 {{ a.name }}
            <button type="button" @click="removeChip(idx)">✕</button>
          </span>
        </div>
        <textarea
          v-model="input"
          placeholder="输入消息，Enter 发送，Shift+Enter 换行"
          :disabled="streaming"
          @keydown="onKeydown"
        />
        <div class="composer-actions">
          <label>
            <input type="file" hidden @change="onFileChange" />
            <el-button size="small">📎 附件</el-button>
          </label>
          <el-button v-if="streaming" type="danger" size="small" @click="stopTurn">停止</el-button>
          <el-button
            v-else
            type="primary"
            size="small"
            :disabled="!input.trim() && !attached.length"
            @click="send"
          >
            发送
          </el-button>
        </div>
      </div>
    </div>
  </main>
</template>
