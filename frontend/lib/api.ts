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

export async function optimizePlan(sessionToken: string): Promise<Plan> {
  const response = await fetch(`${API_BASE}/optimize/${sessionToken}`);

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || "Failed to generate plan");
  }

  const result = await response.json();
  return result.plan;
}