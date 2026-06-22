<script setup lang="ts">
import { computed } from "vue";
import { BotMessageSquare, Plus, Trash2 } from "lucide-vue-next";
import type { Agent, Session } from "../types";

const props = defineProps<{
  agents: Agent[];
  sessions: Session[];
  selectedAgentId: number | null;
  currentSessionId: string | null;
}>();

const emit = defineEmits<{
  brandClick: [];
  selectAgent: [id: number];
  selectSession: [id: string];
  newAgent: [];
  newSession: [agentId: number];
  deleteSession: [id: string];
}>();

const agentSessions = computed(() => {
  if (!props.selectedAgentId) return [];
  return props.sessions.filter(s => s.agent_id === props.selectedAgentId);
});

function agentLabel(a: Agent) {
  return a.description || a.model;
}
</script>

<template>
  <aside class="sidebar">
    <div class="sidebar-brand">
      <button type="button" class="brand-btn" title="Sophclaw — 管理 Agent" @click="emit('brandClick')">
        <img src="/assets/app_icon_v2.png" alt="" class="brand-icon" />
        <img src="/assets/logo-sophclaw.png" alt="Sophclaw" class="brand-logo" />
      </button>
    </div>

    <div class="sidebar-scroll">
      <div class="section-label">Agents</div>

      <template v-if="agents.length">
        <button
          v-for="agent in agents"
          :key="agent.id"
          type="button"
          class="agent-row"
          :class="{ active: selectedAgentId === agent.id }"
          @click="emit('selectAgent', agent.id)"
        >
          <span class="agent-emoji">🦞</span>
          <div class="agent-text">
            <div class="agent-title">{{ agent.name }}</div>
            <div class="agent-sub">{{ agentLabel(agent) }}</div>
          </div>
          <BotMessageSquare v-if="selectedAgentId === agent.id" :size="14" color="var(--brand-primary)" />
        </button>
      </template>
      <div v-else class="agent-sub" style="padding: 8px 10px">暂无 Agent</div>

      <button type="button" class="action-row" @click="emit('newAgent')">
        <Plus :size="14" />
        <span>新建 Agent</span>
      </button>

      <template v-if="selectedAgentId">
        <div class="section-label" style="margin-top: 16px">Sessions</div>
        <div
          v-for="s in agentSessions"
          :key="s.id"
          class="session-row"
          :class="{ active: currentSessionId === s.id }"
        >
          <button type="button" class="session-row-main" @click="emit('selectSession', s.id)">
            <div class="agent-text">
              <div class="agent-title">{{ s.title || "(新对话)" }}</div>
              <div class="agent-sub">{{ s.updated_at?.slice(0, 16) }}</div>
            </div>
          </button>
          <button
            type="button"
            class="session-delete-btn"
            title="删除 Session"
            @click.stop="emit('deleteSession', s.id)"
          >
            <Trash2 :size="14" />
          </button>
        </div>
        <button type="button" class="action-row" @click="emit('newSession', selectedAgentId)">
          <Plus :size="14" />
          <span>新建 Session</span>
        </button>
      </template>
    </div>
  </aside>
</template>
