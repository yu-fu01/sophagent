<script setup lang="ts">
import { onMounted, reactive, ref, watch } from "vue";
import { agentsApi, providersApi } from "../api/client";
import type { Agent } from "../types";

const props = defineProps<{ agent: Agent | null; startInForm?: boolean }>();

const emit = defineEmits<{
  close: [];
  saved: [];
  deleted: [];
}>();

const agents = ref<Agent[]>([]);
const options = ref<{ tools: string[]; providers: string[] }>({ tools: [], providers: [] });
const editing = ref<Agent | null>(null);
const mode = ref<"list" | "form">("list");
const saving = ref(false);
const deleting = ref(false);
const error = ref("");
const modelSuggestions = ref<string[]>([]);

const form = reactive({
  name: "",
  description: "",
  provider: "",
  model: "",
  system_prompt: "You are a helpful assistant.",
  tools: [] as string[],
});

function fillForm(a: Agent | null) {
  if (a) {
    editing.value = a;
    form.name = a.name;
    form.description = a.description;
    form.provider = a.provider;
    form.model = a.model;
    form.system_prompt = a.system_prompt;
    form.tools = [...a.tools];
    mode.value = "form";
  } else {
    editing.value = null;
    form.name = "";
    form.description = "";
    form.provider = options.value.providers[0] || "";
    form.model = "";
    form.system_prompt = "You are a helpful assistant.";
    form.tools = [...options.value.tools];
    mode.value = "form";
  }
  error.value = "";
  void loadModelSuggestions(form.provider);
}

async function loadModelSuggestions(provider: string) {
  modelSuggestions.value = await providersApi.listModels(provider);
  if (form.model && !modelSuggestions.value.includes(form.model)) {
    modelSuggestions.value = [form.model, ...modelSuggestions.value];
  }
}

function queryModels(query: string, cb: (items: { value: string }[]) => void) {
  const q = query.trim().toLowerCase();
  const list = modelSuggestions.value
    .filter(m => !q || m.toLowerCase().includes(q))
    .slice(0, 30)
    .map(m => ({ value: m }));
  cb(list);
}

async function load() {
  agents.value = await agentsApi.list();
  options.value = await agentsApi.options();
  if (props.agent) fillForm(props.agent);
  else if (props.startInForm) fillForm(null);
  else mode.value = "list";
}

async function save() {
  saving.value = true;
  error.value = "";
  const body = {
    name: form.name.trim(),
    description: form.description.trim(),
    provider: form.provider,
    model: form.model.trim(),
    system_prompt: form.system_prompt,
    tools: form.tools,
  };
  try {
    if (editing.value) {
      await agentsApi.update(editing.value.id, body);
    } else {
      await agentsApi.create(body);
    }
    emit("saved");
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    saving.value = false;
  }
}

async function removeAgent(a: Agent) {
  if (!confirm(`删除 Agent「${a.name}」？其所有 Session 和对话记录将一并删除。`)) return;
  deleting.value = true;
  try {
    await agentsApi.remove(a.id);
    if (editing.value?.id === a.id) {
      editing.value = null;
      mode.value = "list";
    }
    await load();
    emit("deleted");
  } catch (e) {
    alert(e instanceof Error ? e.message : String(e));
  } finally {
    deleting.value = false;
  }
}

watch(() => props.agent, () => {
  if (props.agent) fillForm(props.agent);
});
watch(() => form.provider, (p) => {
  if (mode.value === "form") void loadModelSuggestions(p);
});
onMounted(load);
</script>

<template>
  <div class="agent-panel" @click.stop>
    <template v-if="mode === 'list'">
      <h3>Sophclaw Agent 管理</h3>
      <table class="agent-list-table">
        <thead>
          <tr>
            <th>名称</th>
            <th>模型</th>
            <th>工具</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="a in agents" :key="a.id">
            <td>
              <strong>{{ a.name }}</strong>
              <div style="font-size: 11px; color: var(--text-tertiary)">{{ a.description }}</div>
            </td>
            <td>{{ a.provider }} / {{ a.model }}</td>
            <td style="font-size: 11px">{{ a.tools.join(", ") }}</td>
            <td>
              <el-button size="small" text @click="fillForm(a)">编辑</el-button>
              <el-button size="small" text type="danger" :loading="deleting" @click="removeAgent(a)">
                删除
              </el-button>
            </td>
          </tr>
        </tbody>
      </table>
      <div class="panel-actions">
        <el-button type="primary" @click="fillForm(null)">+ 新建 Agent</el-button>
        <el-button @click="emit('close')">关闭</el-button>
      </div>
    </template>

    <template v-else>
      <h3>{{ editing ? `编辑 Agent — ${editing.name}` : "新建 Agent" }}</h3>
      <div class="form-grid">
        <div>
          <label>名称 (name)</label>
          <el-input v-model="form.name" :disabled="!!editing" placeholder="helper" />
        </div>
        <div>
          <label>描述</label>
          <el-input v-model="form.description" placeholder="简短说明" />
        </div>
        <div>
          <label>Provider</label>
          <el-select v-model="form.provider" style="width: 100%">
            <el-option v-for="p in options.providers" :key="p" :label="p" :value="p" />
          </el-select>
        </div>
        <div>
          <label>Model</label>
          <el-autocomplete
            v-model="form.model"
            :fetch-suggestions="queryModels"
            clearable
            placeholder="输入或从建议中选择，如 gpt-4o"
            style="width: 100%"
          />
          <div class="field-hint">
            建议列表来自 provider API，仅供参考；可手动输入你账号实际可用的模型名。
          </div>
        </div>
        <div class="full">
          <label>System prompt</label>
          <el-input v-model="form.system_prompt" type="textarea" :rows="4" />
        </div>
        <div class="full">
          <label>Tools</label>
          <div class="tools-grid">
            <label v-for="t in options.tools" :key="t">
              <input v-model="form.tools" type="checkbox" :value="t" />
              {{ t }}
            </label>
          </div>
        </div>
      </div>
      <p v-if="error" style="color: #d14343; font-size: 13px">{{ error }}</p>
      <div class="panel-actions">
        <el-button type="primary" :loading="saving" @click="save">
          {{ editing ? "保存" : "创建" }}
        </el-button>
        <el-button
          v-if="editing"
          type="danger"
          plain
          :loading="deleting"
          @click="removeAgent(editing)"
        >
          删除 Agent
        </el-button>
        <el-button @click="props.agent ? emit('close') : (mode = 'list')">
          {{ props.agent ? "取消" : "返回列表" }}
        </el-button>
      </div>
    </template>
  </div>
</template>
