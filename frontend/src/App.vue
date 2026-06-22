<script setup lang="ts">
import { onMounted, ref } from "vue";
import { bootstrapAuth, agentsApi, sessionsApi } from "./api/client";
import type { Agent, Session } from "./types";
import Sidebar from "./components/Sidebar.vue";
import ChatPanel from "./components/ChatPanel.vue";
import AgentPanel from "./components/AgentPanel.vue";

const loading = ref(true);
const error = ref("");
const agents = ref<Agent[]>([]);
const sessions = ref<Session[]>([]);
const selectedAgentId = ref<number | null>(null);
const currentSessionId = ref<string | null>(null);
const showAgentPanel = ref(false);
const panelAgent = ref<Agent | null>(null);
const panelStartForm = ref(false);

async function refreshAgents() {
  agents.value = await agentsApi.list();
  if (selectedAgentId.value && !agents.value.find(a => a.id === selectedAgentId.value)) {
    selectedAgentId.value = agents.value[0]?.id ?? null;
  }
}

async function refreshSessions() {
  sessions.value = await sessionsApi.list();
}

async function boot() {
  loading.value = true;
  error.value = "";
  try {
    await bootstrapAuth();
    await refreshAgents();
    await refreshSessions();
    if (!selectedAgentId.value && agents.value.length) {
      selectedAgentId.value = agents.value[0].id;
    }
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    loading.value = false;
  }
}

async function selectAgent(agentId: number) {
  selectedAgentId.value = agentId;
  const existing = sessions.value.find(s => s.agent_id === agentId);
  if (existing) {
    currentSessionId.value = existing.id;
    return;
  }
  const s = await sessionsApi.create(agentId);
  await refreshSessions();
  currentSessionId.value = s.id;
}

async function selectSession(sessionId: string) {
  currentSessionId.value = sessionId;
  const s = sessions.value.find(x => x.id === sessionId);
  if (s) selectedAgentId.value = s.agent_id;
}

async function newSessionForAgent(agentId: number) {
  const s = await sessionsApi.create(agentId);
  await refreshSessions();
  selectedAgentId.value = agentId;
  currentSessionId.value = s.id;
}

async function deleteSession(sessionId: string) {
  const s = sessions.value.find(x => x.id === sessionId);
  const label = s?.title || "(新对话)";
  if (!confirm(`删除 Session「${label}」？对话记录将不可恢复。`)) return;
  try {
    await sessionsApi.remove(sessionId);
    await refreshSessions();
    if (currentSessionId.value === sessionId) {
      const remaining = sessions.value.filter(x => x.agent_id === selectedAgentId.value);
      currentSessionId.value = remaining[0]?.id ?? null;
    }
  } catch (e) {
    alert(e instanceof Error ? e.message : String(e));
  }
}

function openBrandPanel() {
  panelAgent.value = null;
  panelStartForm.value = false;
  showAgentPanel.value = true;
}

function openCreateAgent() {
  panelAgent.value = null;
  panelStartForm.value = true;
  showAgentPanel.value = true;
}

async function onAgentSaved() {
  showAgentPanel.value = false;
  panelAgent.value = null;
  await refreshAgents();
}

async function onAgentDeleted() {
  await refreshAgents();
  await refreshSessions();
  if (selectedAgentId.value && !agents.value.find(a => a.id === selectedAgentId.value)) {
    selectedAgentId.value = agents.value[0]?.id ?? null;
    currentSessionId.value = sessions.value.find(s => s.agent_id === selectedAgentId.value)?.id ?? null;
  }
}

onMounted(boot);
</script>

<template>
  <div v-if="loading" class="empty-state">
    <span class="icon-wrap">⏳</span>
    <p>正在加载 Sophclaw…</p>
  </div>
  <div v-else-if="error" class="empty-state">
    <span class="icon-wrap">⚠️</span>
    <p>{{ error }}</p>
    <el-button type="primary" @click="boot">重试</el-button>
  </div>
  <div v-else class="app-shell">
    <Sidebar
      :agents="agents"
      :sessions="sessions"
      :selected-agent-id="selectedAgentId"
      :current-session-id="currentSessionId"
      @brand-click="openBrandPanel"
      @select-agent="selectAgent"
      @select-session="selectSession"
      @new-agent="openCreateAgent"
      @new-session="newSessionForAgent"
      @delete-session="deleteSession"
    />
    <ChatPanel
      v-if="currentSessionId"
      :session-id="currentSessionId"
      :agents="agents"
      :selected-agent-id="selectedAgentId"
      @session-updated="refreshSessions"
    />
    <div v-else class="main empty-state">
      <div class="icon-wrap">
        <img src="/assets/app_icon_v2.png" alt="" class="brand-icon" />
      </div>
      <h2 style="margin: 0; color: var(--text-title)">欢迎使用 Sophclaw</h2>
      <p style="margin: 0; text-align: center; max-width: 360px">
        点击左上角 Sophclaw 或「新建 Agent」创建助手，然后选择 Agent 开始对话。
      </p>
      <el-button type="primary" @click="openCreateAgent">新建 Agent</el-button>
    </div>

    <div v-if="showAgentPanel" class="agent-panel-overlay" @click.self="showAgentPanel = false">
      <AgentPanel
        :agent="panelAgent"
        :start-in-form="panelStartForm"
        @close="showAgentPanel = false"
        @saved="onAgentSaved"
        @deleted="onAgentDeleted"
      />
    </div>
  </div>
</template>
