import type { Agent, AgentOptions, Me, Message, Session } from "../types";

let token = localStorage.getItem("sophclaw_token") || "";

export function getToken() {
  return token;
}

export function setToken(t: string) {
  token = t;
  if (t) localStorage.setItem("sophclaw_token", t);
  else localStorage.removeItem("sophclaw_token");
}

async function api(path: string, opts: RequestInit = {}) {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(opts.headers as Record<string, string> | undefined),
  };
  if (token) headers.Authorization = `Bearer ${token}`;
  const body =
    opts.body && typeof opts.body !== "string"
      ? JSON.stringify(opts.body)
      : opts.body;
  const resp = await fetch(path, { ...opts, headers, body });
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const j = await resp.json();
      detail = j.detail || detail;
    } catch {
      /* ignore */
    }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return resp;
}

async function apiJson<T>(path: string, opts: RequestInit = {}): Promise<T> {
  const resp = await api(path, opts);
  if (resp.status === 204) return undefined as T;
  return resp.json();
}

export async function bootstrapAuth(): Promise<Me> {
  try {
    const auto = await apiJson<{
      token: string;
      username: string;
      role: string;
      is_admin: boolean;
    }>("/api/auth/auto");
    setToken(auto.token);
    return {
      id: 0,
      username: auto.username,
      role: auto.role,
      is_admin: auto.is_admin,
    };
  } catch {
    const me = await apiJson<Me>("/api/auth/me");
    return me;
  }
}

export const agentsApi = {
  list: () => apiJson<Agent[]>("/api/agents"),
  options: () => apiJson<AgentOptions>("/api/agents/meta/options"),
  create: (body: Record<string, unknown>) =>
    apiJson<Agent>("/api/agents", { method: "POST", body: JSON.stringify(body) }),
  update: (id: number, body: Record<string, unknown>) =>
    apiJson<Agent>(`/api/agents/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  remove: (id: number) => apiJson(`/api/agents/${id}`, { method: "DELETE" }),
};

export const sessionsApi = {
  list: () => apiJson<Session[]>("/api/sessions"),
  create: (agent_id: number, title = "") =>
    apiJson<Session>("/api/sessions", {
      method: "POST",
      body: JSON.stringify({ agent_id, title }),
    }),
  get: (id: string) =>
    apiJson<Session & { messages: Message[] }>(`/api/sessions/${id}`),
  remove: (id: string) => apiJson(`/api/sessions/${id}`, { method: "DELETE" }),
  stop: (id: string) => apiJson(`/api/sessions/${id}/stop`, { method: "POST" }),
  truncate: (id: string, message_id: number) =>
    apiJson(`/api/sessions/${id}/truncate`, {
      method: "POST",
      body: JSON.stringify({ message_id }),
    }),
  chat: (id: string, content: string) =>
    api(`/api/sessions/${id}/chat`, {
      method: "POST",
      body: JSON.stringify({ content }),
    }),
};

export async function uploadFile(file: File): Promise<{ path: string; name: string }> {
  const dataUrl = await new Promise<string>((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(r.result as string);
    r.onerror = () => reject(new Error("read failed"));
    r.readAsDataURL(file);
  });
  const r = await apiJson<{ entry: { path: string; name: string } }>("/api/files/upload", {
    method: "POST",
    body: JSON.stringify({ path: file.name, data_url: dataUrl }),
  });
  return { path: r.entry.path, name: r.entry.name };
}
