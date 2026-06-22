declare global {
  interface Window {
    mdToHtml?: (text: string) => string;
    renderMathInElement?: (el: Element) => void;
    worklogSummary?: (opts: {
      hasThinking: boolean;
      live: boolean;
      tools: Array<{ kind: string; done: boolean; error: boolean }>;
    }) => string;
    toolKind?: (name: string) => string;
  }
}

export interface Agent {
  id: number;
  name: string;
  description: string;
  system_prompt: string;
  provider: string;
  model: string;
  tools: string[];
  skills: string[] | null;
  max_iterations: number;
  temperature: number | null;
  group_id: number;
}

export interface Session {
  id: string;
  user_id: number;
  agent_id: number;
  group_id: number;
  title: string;
  agent_name?: string;
  override_provider: string | null;
  override_model: string | null;
  thinking_mode: string | null;
  created_at: string;
  updated_at: string;
}

export interface Message {
  id: number;
  role: string;
  content: string;
  tool_calls?: Array<{ id: string; name: string; arguments: Record<string, unknown> }>;
  reasoning?: string;
}

export interface AgentOptions {
  tools: string[];
  providers: string[];
}

export interface Me {
  id: number;
  username: string;
  role: string;
  is_admin: boolean;
}

export {};
