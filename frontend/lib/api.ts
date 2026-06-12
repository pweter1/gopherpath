/**
 * api.ts
 * ------
 * API client for the GopherPath backend.
 * All fetch calls to the FastAPI backend go through this file.
 * This makes it easy to swap the base URL for production deployment.
 */

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export interface Student {
  name: string;
  major: string;
  expected_graduation: string;
  credits_earned: number;
  gpa_overall: number;
}

export interface Course {
  subject: string;
  number: string;
  title: string;
  credits: number;
  requirement_category: string;
  is_pinned: boolean;
}

export interface TermPlan {
  term_code: string;
  term_label: string;
  courses: Course[];
  total_credits: number;
}

export interface PreferenceData {
  difficulty: string;
  timeline: string;
  freeText: string;
}

export interface Plan {
  status: string;
  plan: TermPlan[];
  total_scheduled_credits: number;
  unscheduled: Course[];
  message: string;
}

export interface ParsedAPAS {
  student: {
    name: string;
    major: string;
    expected_graduation: string;
    advisor: string;
  };
  credits: {
    earned: number;
    in_progress: number;
    needed: number;
    total_required: number;
  };
  gpa: {
    overall: number;
    major: number;
  };
  completed_courses: any[];
  remaining_requirements: any[];
}

export interface RequirementCoverageItem {
  category: string;
  displayName: string;
  status: "addressed" | "unscheduled" | "unaddressed";
  courseCode?: string;
  semester?: string;
}

export interface SavedMessage {
  role: "user" | "assistant";
  content: string;
  timestamp: string; // ISO string
  type?: "text" | "requirements_coverage";
  requirementsData?: RequirementCoverageItem[];
}

export interface SessionState {
  parsed_apas: ParsedAPAS;
  generated_plan: Plan | null;
  plan_explanation: string | null;
  chat_history: SavedMessage[];
}

export async function loadSession(sessionToken: string): Promise<SessionState | null> {
  const response = await fetch(`${API_BASE}/student/${sessionToken}`);
  if (!response.ok) return null;
  const result = await response.json();
  const d = result.data;
  if (!d.generated_plan) return null; // not yet optimized, don't restore
  return {
    parsed_apas: d.parsed_apas,
    generated_plan: d.generated_plan,
    plan_explanation: d.plan_explanation || null,
    chat_history: d.chat_history || [],
  };
}

export async function saveExplanation(sessionToken: string, explanation: string): Promise<void> {
  await fetch(`${API_BASE}/save-explanation/${sessionToken}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ explanation }),
  });
}

export async function saveChatHistory(
  sessionToken: string,
  messages: SavedMessage[]
): Promise<void> {
  await fetch(`${API_BASE}/chat-history/${sessionToken}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ messages }),
  });
}

export async function parseAPAS(file: File): Promise<{
  session_token: string;
  data: ParsedAPAS;
}> {
  const formData = new FormData();
  formData.append("file", file);

  const response = await fetch(`${API_BASE}/parse-apas`, {
    method: "POST",
    body: formData,
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || "Failed to parse APAS");
  }

  const result = await response.json();
  return result;
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}

async function* _readSSEStream(
  response: Response
): AsyncGenerator<string> {
  if (!response.body) throw new Error("No response body");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";
    for (const line of lines) {
      if (!line.startsWith("data: ")) continue;
      const data = line.slice(6).trim();
      if (data === "[DONE]") return;
      try {
        const parsed = JSON.parse(data);
        if (parsed.text) yield parsed.text;
      } catch {}
    }
  }
}

export async function* streamChat(
  sessionToken: string,
  message: string,
  history: ChatMessage[],
  isOpening = false,
  signal?: AbortSignal
): AsyncGenerator<string> {
  const response = await fetch(`${API_BASE}/chat/${sessionToken}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, history, is_opening: isOpening }),
    signal,
  });
  if (!response.ok) throw new Error("Chat request failed");
  yield* _readSSEStream(response);
}

export async function* streamPlanExplanation(
  sessionToken: string,
  signal?: AbortSignal
): AsyncGenerator<string> {
  const response = await fetch(`${API_BASE}/plan-explanation/${sessionToken}`, {
    signal,
  });
  if (!response.ok) throw new Error("Plan explanation request failed");
  yield* _readSSEStream(response);
}

export async function optimizePlan(
  sessionToken: string,
  preferences: PreferenceData
): Promise<Plan> {
  const response = await fetch(`${API_BASE}/optimize/${sessionToken}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      difficulty: preferences.difficulty,
      timeline: preferences.timeline,
      free_text: preferences.freeText,
    }),
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || "Failed to generate plan");
  }

  const result = await response.json();
  return result.plan.plan ? result.plan : result.plan;
}