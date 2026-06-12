/**
 * page.tsx
 * --------
 * GopherPath home page.
 * Handles the APAS upload flow and displays the generated plan.
 *
 * State machine:
 *   idle → uploading → confirming → optimizing → plan
 *
 * Each state has its own UI component rendered below.
 */

"use client";

import { useState, useCallback, useEffect, useRef } from "react";
import {
  parseAPAS,
  optimizePlan,
  loadSession,
  saveChatHistory,
  streamChat,
  streamPlanExplanation,
  ParsedAPAS,
  Plan,
  ChatMessage,
  SavedMessage,
  RequirementCoverageItem,
} from "@/lib/api";

const SESSION_KEY = "gopherpath_session";

interface PreferenceData {
  difficulty: string;
  timeline: string;
  freeText: string;
}

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type AppState =
  | { stage: "idle"; savedToken?: string }
  | { stage: "restoring" }
  | { stage: "uploading" }
  | { stage: "confirming"; sessionToken: string; parsedData: ParsedAPAS }
  | { stage: "preferences"; sessionToken: string; parsedData: ParsedAPAS }
  | { stage: "optimizing"; sessionToken: string; parsedData: ParsedAPAS }
  | {
      stage: "plan";
      sessionToken: string;
      parsedData: ParsedAPAS;
      plan: Plan;
      savedMessages?: SavedMessage[];
    }
  | { stage: "error"; message: string };

// ---------------------------------------------------------------------------
// Upload component
// ---------------------------------------------------------------------------

function UploadStage({
  onUpload,
  savedToken,
  onResume,
}: {
  onUpload: (file: File) => void;
  savedToken?: string;
  onResume?: () => void;
}) {
  const [dragging, setDragging] = useState(false);

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      setDragging(false);
      const file = e.dataTransfer.files[0];
      if (file?.name.endsWith(".pdf")) onUpload(file);
    },
    [onUpload]
  );

  return (
    <div className="min-h-screen bg-gray-50 flex flex-col items-center justify-center p-8">
      <div className="max-w-lg w-full">
        {/* Header */}
        <div className="text-center mb-10">
          <h1 className="text-4xl font-bold text-gray-900 mb-3">
            Gopher<span className="text-red-600">Path</span>
          </h1>
          <p className="text-lg text-gray-600">
            AI-powered course planning for UMN students
          </p>
        </div>

        {/* Resume banner */}
        {savedToken && onResume && (
          <div className="mb-6 bg-red-50 border border-red-200 rounded-xl p-4 flex items-center justify-between">
            <div>
              <p className="text-sm font-medium text-gray-900">You have an existing plan</p>
              <p className="text-xs text-gray-500 mt-0.5">Continue where you left off without re-uploading.</p>
            </div>
            <div className="flex gap-2 shrink-0 ml-4">
              <button
                onClick={onResume}
                className="px-4 py-2 bg-[#7A0019] text-white text-xs font-medium rounded-lg hover:bg-red-900 transition-colors"
              >
                Continue my plan
              </button>
              <button
                onClick={() => {
                  localStorage.removeItem(SESSION_KEY);
                  window.location.reload();
                }}
                className="px-4 py-2 bg-white border border-gray-200 text-gray-600 text-xs font-medium rounded-lg hover:bg-gray-50 transition-colors"
              >
                Start fresh
              </button>
            </div>
          </div>
        )}

        {/* Upload box */}
        <div
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={handleDrop}
          className={`
            border-2 border-dashed rounded-xl p-12 text-center transition-colors
            ${dragging
              ? "border-red-400 bg-red-50"
              : "border-gray-300 bg-white hover:border-gray-400"
            }
          `}
        >
          <div className="text-5xl mb-4">📄</div>
          <p className="text-lg font-medium text-gray-700 mb-2">
            Upload your APAS report
          </p>
          <p className="text-sm text-gray-500 mb-6">
            Drag and drop your PDF here, or click to browse
          </p>
          <label className="cursor-pointer">
            <span className="bg-red-600 text-white px-6 py-2.5 rounded-lg text-sm font-medium hover:bg-red-700 transition-colors">
              Choose PDF
            </span>
            <input
              type="file"
              accept=".pdf"
              className="hidden"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) onUpload(file);
              }}
            />
          </label>
        </div>

        {/* Instructions */}
        <div className="mt-6 bg-white rounded-xl p-6 border border-gray-200">
          <p className="text-sm font-medium text-gray-700 mb-3">
            How to get your APAS:
          </p>
          <ol className="text-sm text-gray-600 space-y-1.5 list-decimal list-inside">
            <li>Log in to One Stop (onestop.umn.edu)</li>
            <li>Go to Academics → Degree Progress (APAS)</li>
            <li>Click "Printer Friendly" and save as PDF</li>
          </ol>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Loading component
// ---------------------------------------------------------------------------

function LoadingStage({ message }: { message: string }) {
  return (
    <div className="min-h-screen bg-gray-50 flex flex-col items-center justify-center">
      <div className="text-center">
        <div className="w-12 h-12 border-4 border-red-600 border-t-transparent rounded-full animate-spin mx-auto mb-4" />
        <p className="text-lg text-gray-700">{message}</p>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Confirmation component
// ---------------------------------------------------------------------------

function ConfirmStage({
  parsedData,
  onConfirm,
  onReset,
}: {
  parsedData: ParsedAPAS;
  onConfirm: () => void;
  onReset: () => void;
}) {
  const { student, credits, gpa } = parsedData;

  return (
    <div className="min-h-screen bg-gray-50 flex flex-col items-center justify-center p-8">
      <div className="max-w-lg w-full">
        <div className="text-center mb-8">
          <div className="text-4xl mb-3">✅</div>
          <h2 className="text-2xl font-bold text-gray-900">
            APAS Parsed Successfully
          </h2>
          <p className="text-gray-600 mt-1">
            Confirm your information before generating a plan
          </p>
        </div>

        <div className="bg-white rounded-xl border border-gray-200 p-6 mb-6 space-y-4">
          <div className="flex justify-between">
            <span className="text-gray-500">Name</span>
            <span className="font-medium text-gray-900">{student.name}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-gray-500">Major</span>
            <span className="font-medium text-gray-900">{student.major}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-gray-500">Expected Graduation</span>
            <span className="font-medium text-gray-900">
              {student.expected_graduation}
            </span>
          </div>
          <div className="flex justify-between">
            <span className="text-gray-500">Credits Earned</span>
            <span className="font-medium text-gray-900">
              {credits.earned} / {credits.total_required}
            </span>
          </div>
          <div className="flex justify-between">
            <span className="text-gray-500">GPA</span>
            <span className="font-medium text-gray-900">{gpa.overall}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-gray-500">Remaining Requirements</span>
            <span className="font-medium text-gray-900">
              {parsedData.remaining_requirements.length} items
            </span>
          </div>
        </div>

        <div className="flex gap-3">
          <button
            onClick={onReset}
            className="flex-1 py-2.5 border border-gray-300 rounded-lg text-gray-700 text-sm font-medium hover:bg-gray-50 transition-colors"
          >
            Upload Different File
          </button>
          <button
            onClick={onConfirm}
            className="flex-1 py-2.5 bg-red-600 text-white rounded-lg text-sm font-medium hover:bg-red-700 transition-colors"
          >
            Generate My Plan →
          </button>
        </div>
      </div>
    </div>
  );
}


// ---------------------------------------------------------------------------
// Preferences component
// ---------------------------------------------------------------------------

function PreferencesStage({
  parsedData,
  onSubmit,
  onReset,
}: {
  parsedData: ParsedAPAS;
  onSubmit: (prefs: PreferenceData) => void;
  onReset: () => void;
}) {
  const [difficulty, setDifficulty] = useState<string>("any");
  const [timeline, setTimeline] = useState<string>("on_time");
  const [freeText, setFreeText] = useState<string>("");

  return (
    <div className="min-h-screen bg-gray-50 flex flex-col items-center justify-center p-8">
      <div className="max-w-lg w-full">
        <div className="text-center mb-8">
          <h2 className="text-2xl font-bold text-gray-900">
            Customize Your Plan
          </h2>
          <p className="text-gray-600 mt-1">
            Tell us what matters most to you
          </p>
        </div>

        {/* Student summary */}
        <div className="bg-white rounded-xl border border-gray-200 p-4 mb-6 flex justify-between text-sm">
          <span className="text-gray-500">{parsedData.student.name}</span>
          <span className="text-gray-500">{parsedData.student.major}</span>
          <span className="text-gray-500">GPA {parsedData.gpa.overall}</span>
          <span className="text-gray-500">Grad {parsedData.student.expected_graduation}</span>
        </div>

        <div className="space-y-6">
          {/* Difficulty */}
          <div className="bg-white rounded-xl border border-gray-200 p-6">
            <h3 className="font-semibold text-gray-900 mb-1">
              Course Difficulty
            </h3>
            <p className="text-sm text-gray-500 mb-4">
              How challenging do you want your remaining courses to be?
            </p>
            <div className="grid grid-cols-2 gap-3">
              {[
                { value: "easy", label: "Easy", desc: "Lower-level courses, higher avg GPA" },
                { value: "any", label: "Balanced", desc: "Mix of levels, no preference" },
                { value: "medium", label: "Moderate", desc: "Some challenging courses" },
                { value: "hard", label: "Rigorous", desc: "Upper-division, more demanding" },
              ].map((opt) => (
                <button
                  key={opt.value}
                  onClick={() => setDifficulty(opt.value)}
                  className={`p-3 rounded-lg border text-left transition-colors ${
                    difficulty === opt.value
                      ? "border-red-500 bg-red-50"
                      : "border-gray-200 hover:border-gray-300"
                  }`}
                >
                  <div className="font-medium text-sm text-gray-900">
                    {opt.label}
                  </div>
                  <div className="text-xs text-gray-500 mt-0.5">
                    {opt.desc}
                  </div>
                </button>
              ))}
            </div>
          </div>

          {/* Timeline */}
          <div className="bg-white rounded-xl border border-gray-200 p-6">
            <h3 className="font-semibold text-gray-900 mb-1">
              Graduation Timeline
            </h3>
            <p className="text-sm text-gray-500 mb-4">
              Your expected graduation is{" "}
              <span className="font-medium text-gray-700">
                {parsedData.student.expected_graduation}
              </span>
            </p>
            <div className="grid grid-cols-2 gap-3">
              {[
                { value: "asap", label: "As Soon As Possible", desc: "Max credits each semester" },
                { value: "on_time", label: "On Expected Date", desc: "Balanced workload" },
              ].map((opt) => (
                <button
                  key={opt.value}
                  onClick={() => setTimeline(opt.value)}
                  className={`p-3 rounded-lg border text-left transition-colors ${
                    timeline === opt.value
                      ? "border-red-500 bg-red-50"
                      : "border-gray-200 hover:border-gray-300"
                  }`}
                >
                  <div className="font-medium text-sm text-gray-900">
                    {opt.label}
                  </div>
                  <div className="text-xs text-gray-500 mt-0.5">
                    {opt.desc}
                  </div>
                </button>
              ))}
            </div>
          </div>

          {/* Free text */}
          <div className="bg-white rounded-xl border border-gray-200 p-6">
            <h3 className="font-semibold text-gray-900 mb-1">
              Anything else?
            </h3>
            <p className="text-sm text-gray-500 mb-3">
              Tell your AI advisor anything that should influence your plan
            </p>
            <textarea
              value={freeText}
              onChange={(e) => setFreeText(e.target.value)}
              placeholder="e.g. I'm burnt out and want an easy semester, I want to focus on machine learning courses, I'm thinking about grad school..."
              className="w-full border border-gray-200 rounded-lg p-3 text-sm text-gray-700 placeholder-gray-400 resize-none focus:outline-none focus:border-red-400"
              rows={3}
            />
          </div>
        </div>

        <div className="flex gap-3 mt-6">
          <button
            onClick={onReset}
            className="flex-1 py-2.5 border border-gray-300 rounded-lg text-gray-700 text-sm font-medium hover:bg-gray-50 transition-colors"
          >
            Back
          </button>
          <button
            onClick={() => onSubmit({ difficulty, timeline, freeText })}
            className="flex-1 py-2.5 bg-red-600 text-white rounded-lg text-sm font-medium hover:bg-red-700 transition-colors"
          >
            Generate My Plan →
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Helpers + requirements coverage card (rendered inside chat bubble)
// ---------------------------------------------------------------------------

function stripPrefix(category: string): string {
  const idx = category.indexOf(" - ");
  return idx !== -1 ? category.slice(idx + 3) : category;
}

const SKIP_KEYWORDS = [
  "Minimum Degree Credits",
  "Minimum Major Credits",
  "Major GPA",
  "University Credit",
  "GPA Requirements",
  "S Grade Credit",
  "Credits Completed",
  "Degree-Applicable",
  "Elective Credits",
  "In Progress",
];

function computeRequirementsCoverage(
  parsedData: ParsedAPAS,
  plan: Plan
): RequirementCoverageItem[] {
  const scheduledMap = new Map<
    string,
    { subject: string; number: string; termLabel: string }
  >();
  for (const term of plan.plan) {
    for (const course of term.courses) {
      // A course may double-dip across requirements; credit every category
      // it satisfies, not just its primary one.
      const cats = course.satisfies_categories ?? [course.requirement_category];
      for (const cat of cats) {
        if (!scheduledMap.has(cat)) {
          scheduledMap.set(cat, {
            subject: course.subject,
            number: course.number,
            termLabel: term.term_label,
          });
        }
      }
    }
  }
  const unscheduledCategories = new Set(
    plan.unscheduled.flatMap(
      (c) => c.satisfies_categories ?? [c.requirement_category]
    )
  );

  return parsedData.remaining_requirements
    .filter(
      (req: any) =>
        !SKIP_KEYWORDS.some((kw: string) =>
          req.category?.toLowerCase().includes(kw.toLowerCase())
        )
    )
    .map((req: any): RequirementCoverageItem => {
      const hit = scheduledMap.get(req.category);
      const isUnscheduled = unscheduledCategories.has(req.category);
      return {
        category: req.category,
        displayName: stripPrefix(req.category),
        status: hit ? "addressed" : isUnscheduled ? "unscheduled" : "unaddressed",
        courseCode: hit ? `${hit.subject} ${hit.number}` : undefined,
        semester: hit ? hit.termLabel : undefined,
      };
    });
}

function RequirementsCoverageCard({ items }: { items: RequirementCoverageItem[] }) {
  return (
    <div className="bg-white rounded-xl border border-gray-100 p-4 shadow-sm min-w-[240px]">
      <p className="text-[11px] font-semibold text-gray-400 uppercase tracking-wider mb-3">
        Requirements at a Glance
      </p>
      <div className="space-y-2">
        {items.map((item, idx) => (
          <div key={idx} className="flex items-center gap-2">
            <span
              className={`text-sm shrink-0 ${
                item.status === "addressed"
                  ? "text-green-500"
                  : item.status === "unscheduled"
                  ? "text-amber-500"
                  : "text-gray-300"
              }`}
            >
              {item.status === "addressed" ? "✓" : item.status === "unscheduled" ? "⚠" : "○"}
            </span>
            <span className="text-sm text-gray-700 flex-1 min-w-0 truncate leading-snug">
              {item.displayName}
            </span>
            {item.courseCode && (
              <span className="text-[11px] font-mono text-gray-400 shrink-0">
                {item.courseCode}
              </span>
            )}
            {item.semester && (
              <span className="text-[11px] text-gray-400 shrink-0 ml-1">
                {item.semester}
              </span>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// AI Advisor Chat component
// ---------------------------------------------------------------------------

interface Message {
  role: "user" | "assistant";
  content: string;
  type: "text" | "requirements_coverage";
  requirementsData?: RequirementCoverageItem[];
  timestamp: Date;
}

function AdvisorChat({
  sessionToken,
  plan,
  parsedData,
  savedMessages,
}: {
  sessionToken: string;
  plan: Plan;
  parsedData: ParsedAPAS;
  savedMessages?: SavedMessage[];
}) {
  const [messages, setMessages] = useState<Message[]>(() =>
    (savedMessages ?? []).map((m) => ({
      role: m.role,
      content: m.content,
      type: m.type ?? "text",
      requirementsData: m.requirementsData,
      timestamp: new Date(m.timestamp),
    }))
  );
  const [input, setInput] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const messagesRef = useRef<Message[]>(messages);
  const prevMsgCountRef = useRef(messages.length);
  const isInitialLoadRef = useRef(true);
  const initFiredRef = useRef((savedMessages?.length ?? 0) > 0);
  const initCompleteRef = useRef((savedMessages?.length ?? 0) > 0);

  useEffect(() => { messagesRef.current = messages; }, [messages]);

  // Scroll: instant on initial load / streaming updates, smooth for new messages
  useEffect(() => {
    if (!messagesEndRef.current) return;
    const countIncreased = messages.length > prevMsgCountRef.current;
    prevMsgCountRef.current = messages.length;
    const behavior: ScrollBehavior =
      countIncreased && !isInitialLoadRef.current ? "smooth" : "auto";
    messagesEndRef.current.scrollIntoView({ behavior });
    if (isInitialLoadRef.current && messages.length > 0) isInitialLoadRef.current = false;
  }, [messages]);

  // Save chat history only after init sequence is fully complete
  const prevIsStreamingRef = useRef(false);
  useEffect(() => {
    if (
      prevIsStreamingRef.current &&
      !isStreaming &&
      initCompleteRef.current &&
      messagesRef.current.length > 0
    ) {
      const serialized: SavedMessage[] = messagesRef.current.map((m) => ({
        role: m.role,
        content: m.content,
        type: m.type,
        requirementsData: m.requirementsData,
        timestamp: m.timestamp.toISOString(),
      }));
      saveChatHistory(sessionToken, serialized).catch(() => {});
    }
    prevIsStreamingRef.current = isStreaming;
  }, [isStreaming, sessionToken]);

  // Init sequence: explanation → requirements card → follow-up (skip on restore)
  useEffect(() => {
    if (initFiredRef.current) return;
    initFiredRef.current = true;

    const controller = new AbortController();
    abortRef.current = controller;

    (async () => {
      const now = new Date();

      // ── Step 1: Stream plan explanation ──
      setIsStreaming(true);
      setMessages([{ role: "assistant", content: "", type: "text", timestamp: now }]);

      try {
        for await (const chunk of streamPlanExplanation(sessionToken, controller.signal)) {
          setMessages((prev) => {
            const u = [...prev];
            u[0] = { ...u[0], content: u[0].content + chunk };
            return u;
          });
        }
      } catch (err: any) {
        if (err.name === "AbortError") { setIsStreaming(false); return; }
        setMessages((prev) => {
          const u = [...prev];
          u[0] = { ...u[0], content: "Unable to load plan explanation." };
          return u;
        });
      }

      // ── 800ms pause ──
      await new Promise((res) => setTimeout(res, 800));
      if (controller.signal.aborted) { setIsStreaming(false); return; }

      // ── Step 2: Requirements coverage card (instant) ──
      const coverageData = computeRequirementsCoverage(parsedData, plan);
      setMessages((prev) => [
        ...prev,
        {
          role: "assistant",
          content: "",
          type: "requirements_coverage",
          requirementsData: coverageData,
          timestamp: new Date(),
        },
      ]);

      // ── 800ms pause ──
      await new Promise((res) => setTimeout(res, 800));
      if (controller.signal.aborted) { setIsStreaming(false); return; }

      // ── Step 3: Stream follow-up ──
      const historyForChat: ChatMessage[] = messagesRef.current.map((m) => ({
        role: m.role,
        content: m.content,
      }));
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: "", type: "text", timestamp: new Date() },
      ]);

      try {
        for await (const chunk of streamChat(
          sessionToken,
          "",
          historyForChat,
          true,
          controller.signal
        )) {
          setMessages((prev) => {
            const updated = [...prev];
            updated[updated.length - 1] = {
              ...updated[updated.length - 1],
              content: updated[updated.length - 1].content + chunk,
            };
            return updated;
          });
        }
      } catch (err: any) {
        if (err.name !== "AbortError") {
          setMessages((prev) => {
            const updated = [...prev];
            updated[updated.length - 1] = {
              ...updated[updated.length - 1],
              content: "Feel free to ask me anything about your plan!",
            };
            return updated;
          });
        }
      } finally {
        initCompleteRef.current = true;
        setIsStreaming(false);
        abortRef.current = null;
      }
    })();

    return () => {
      controller.abort();
      initFiredRef.current = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function sendMessage(userMessage: string) {
    if (isStreaming) return;
    if (!userMessage.trim()) return;

    const historySnapshot: ChatMessage[] = messagesRef.current.map((m) => ({
      role: m.role,
      content: m.content,
    }));
    const now = new Date();

    setIsStreaming(true);
    setInput("");
    setMessages((prev) => [
      ...prev,
      { role: "user", content: userMessage, type: "text", timestamp: now },
      { role: "assistant", content: "", type: "text", timestamp: now },
    ]);

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      for await (const chunk of streamChat(
        sessionToken,
        userMessage,
        historySnapshot,
        false,
        controller.signal
      )) {
        setMessages((prev) => {
          const updated = [...prev];
          updated[updated.length - 1] = {
            ...updated[updated.length - 1],
            content: updated[updated.length - 1].content + chunk,
          };
          return updated;
        });
      }
    } catch (err: any) {
      if (err.name !== "AbortError") {
        setMessages((prev) => {
          const updated = [...prev];
          updated[updated.length - 1] = {
            ...updated[updated.length - 1],
            content: "Sorry, I ran into an issue. Please try again.",
          };
          return updated;
        });
      }
    } finally {
      setIsStreaming(false);
      abortRef.current = null;
    }
  }

  const formatTime = (d: Date) =>
    d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

  return (
    <div className="flex flex-col flex-1 min-h-0">
      {/* Chat header */}
      <div className="flex-none flex items-center gap-2.5 px-6 py-3 border-b border-gray-100 bg-white">
        <span className="text-sm font-semibold text-gray-800">Your AI Advisor</span>
        <span
          className={`w-2 h-2 rounded-full animate-pulse ${
            isStreaming ? "bg-amber-400" : "bg-green-400"
          }`}
        />
      </div>

      {/* Message list */}
      <div className="flex-1 min-h-0 overflow-y-auto px-4 py-4 space-y-4 bg-gray-50">
        {messages.length === 0 && (
          <div className="flex items-center justify-center h-full">
            <p className="text-xs text-gray-300">Preparing your analysis…</p>
          </div>
        )}
        {messages.map((msg, idx) => (
          <div
            key={idx}
            className={`flex items-end gap-2 ${
              msg.role === "user" ? "justify-end" : "justify-start"
            }`}
          >
            {msg.role === "assistant" && (
              <div className="flex-none w-7 h-7 rounded-full bg-[#7A0019] flex items-center justify-center text-white text-xs font-bold shrink-0 mb-4">
                N
              </div>
            )}
            <div
              className={`flex flex-col gap-1 ${
                msg.role === "user" ? "items-end max-w-[75%]" : "items-start max-w-[85%]"
              }`}
            >
              {msg.type === "requirements_coverage" ? (
                <div className="bg-gray-100 rounded-2xl rounded-bl-sm p-3">
                  {msg.requirementsData && (
                    <RequirementsCoverageCard items={msg.requirementsData} />
                  )}
                </div>
              ) : (
                <div
                  className={`px-4 py-3 rounded-2xl text-sm leading-relaxed whitespace-pre-wrap ${
                    msg.role === "user"
                      ? "bg-[#7A0019] text-white rounded-br-sm"
                      : "bg-white border border-gray-200 text-gray-800 rounded-bl-sm shadow-sm"
                  }`}
                >
                  {msg.content === "" && isStreaming && idx === messages.length - 1 ? (
                    <span className="flex gap-1 py-0.5">
                      {[0, 150, 300].map((delay) => (
                        <span
                          key={delay}
                          className="w-1.5 h-1.5 bg-gray-300 rounded-full animate-bounce"
                          style={{ animationDelay: `${delay}ms` }}
                        />
                      ))}
                    </span>
                  ) : (
                    msg.content
                  )}
                </div>
              )}
              <span className="text-[10px] text-gray-300 px-1">
                {formatTime(msg.timestamp)}
              </span>
            </div>
          </div>
        ))}
        <div ref={messagesEndRef} />
      </div>

      {/* Input area */}
      <div className="flex-none px-4 py-3 border-t border-gray-200 bg-white flex gap-2">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !isStreaming) {
              e.preventDefault();
              sendMessage(input);
            }
          }}
          placeholder="Ask about your plan..."
          disabled={isStreaming}
          className="flex-1 text-sm border border-gray-200 rounded-xl px-4 py-2.5 focus:outline-none focus:border-[#7A0019] disabled:bg-gray-50 disabled:text-gray-400 transition-colors"
        />
        {isStreaming ? (
          <button
            onClick={() => abortRef.current?.abort()}
            className="px-4 py-2.5 text-xs font-medium bg-gray-100 text-gray-600 rounded-xl hover:bg-gray-200 transition-colors"
          >
            Stop
          </button>
        ) : (
          <button
            onClick={() => sendMessage(input)}
            disabled={!input.trim()}
            className="px-4 py-2.5 text-xs font-medium bg-[#7A0019] text-white rounded-xl hover:bg-red-900 transition-colors disabled:opacity-40"
          >
            Send
          </button>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Plan display component
// ---------------------------------------------------------------------------

function PlanStage({
  sessionToken,
  parsedData,
  plan,
  onReset,
  savedMessages,
}: {
  sessionToken: string;
  parsedData: ParsedAPAS;
  plan: Plan;
  onReset: () => void;
  savedMessages?: SavedMessage[];
}) {
  const termColors: Record<string, string> = {
    F26: "bg-orange-50 border-orange-200",
    SP27: "bg-blue-50 border-blue-200",
    F27: "bg-orange-50 border-orange-200",
    SP28: "bg-blue-50 border-blue-200",
  };

  return (
    <div className="flex flex-col h-screen bg-white overflow-hidden">

      {/* ── Full-width header ── */}
      <div className="flex-none flex items-center justify-between px-8 py-4 bg-white border-b border-gray-200">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">
            Gopher<span className="text-red-600">Path</span>
          </h1>
          <p className="text-sm text-gray-500 mt-0.5">
            {parsedData.student.name} · {parsedData.student.major} ·
            Graduating {parsedData.student.expected_graduation}
          </p>
        </div>
        <button
          onClick={onReset}
          className="text-sm text-gray-400 hover:text-gray-600 underline"
        >
          Start over
        </button>
      </div>

      {/* ── Two-column body ── */}
      <div className="flex flex-1 min-h-0 overflow-hidden">

        {/* Left column — schedule (scrollable) */}
        <div className="w-3/5 overflow-y-auto p-8 bg-gray-50">

          {/* Disclaimer */}
          <div className="bg-white border border-gray-200 rounded-xl p-4 mb-5 text-sm text-gray-400 leading-relaxed">
            This plan is AI-generated. Always verify with your academic advisor
            before registering. Course availability and requirement fulfillment
            should be confirmed through One Stop.
          </div>

          {/* Partial plan warning */}
          {plan.status === "partial" && (
            <div className="bg-yellow-50 border border-yellow-200 rounded-xl p-4 mb-5 text-sm text-yellow-800">
              ⚠️ {plan.message}
            </div>
          )}

          {/* Semester grid */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-5 mb-6">
            {plan.plan.map((term) => (
              <div
                key={term.term_code}
                className={`rounded-xl border p-5 ${
                  termColors[term.term_code] || "bg-white border-gray-200"
                }`}
              >
                <div className="flex items-center justify-between mb-4">
                  <h3 className="font-semibold text-gray-900">{term.term_label}</h3>
                  <span className="text-sm text-gray-500">{term.total_credits} cr</span>
                </div>
                {term.courses.length === 0 ? (
                  <p className="text-sm text-gray-400 italic">No courses scheduled</p>
                ) : (
                  <div className="space-y-2">
                    {term.courses.map((course, idx) => (
                      <div
                        key={idx}
                        className="bg-white rounded-lg p-3 border border-white/80 shadow-sm"
                      >
                        <div className="flex items-start justify-between gap-2">
                          <div>
                            <span className="text-xs font-mono font-medium text-gray-400">
                              {course.subject} {course.number}
                            </span>
                            <p className="text-sm font-medium text-gray-900 mt-0.5">
                              {course.title}
                            </p>
                            <p className="text-xs text-gray-400 mt-0.5">
                              {course.requirement_category}
                            </p>
                          </div>
                          <span className="text-xs text-gray-400 shrink-0">
                            {course.credits}cr
                          </span>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>

          {/* Unscheduled */}
          {plan.unscheduled.length > 0 && (
            <div className="bg-white rounded-xl border border-gray-200 p-6 mb-6">
              <h3 className="font-semibold text-gray-900 mb-3">Could Not Schedule</h3>
              <div className="space-y-2">
                {plan.unscheduled.map((course, idx) => (
                  <div key={idx} className="flex justify-between text-sm">
                    <span className="text-gray-700">
                      {course.subject} {course.number} — {course.title}
                    </span>
                    <span className="text-gray-400">{course.requirement_category}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* Right column — AI advisor (full height flex column) */}
        <div className="w-2/5 flex flex-col h-full border-l border-gray-200 bg-white">
          <AdvisorChat
            sessionToken={sessionToken}
            plan={plan}
            parsedData={parsedData}
            savedMessages={savedMessages}
          />
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main page — state machine
// ---------------------------------------------------------------------------

export default function Home() {
  const [state, setState] = useState<AppState>({ stage: "restoring" });

  // On mount: check localStorage for a saved session token
  useEffect(() => {
    const token = localStorage.getItem(SESSION_KEY);
    if (!token) {
      setState({ stage: "idle" });
      return;
    }
    // Show upload page with resume banner immediately — restore happens on click
    setState({ stage: "idle", savedToken: token });
  }, []);

  const handleResume = useCallback(async () => {
    const token = localStorage.getItem(SESSION_KEY);
    if (!token) return;
    setState({ stage: "restoring" });
    try {
      const saved = await loadSession(token);
      if (!saved || !saved.generated_plan) {
        setState({ stage: "idle" });
        return;
      }
      setState({
        stage: "plan",
        sessionToken: token,
        parsedData: saved.parsed_apas,
        plan: saved.generated_plan,
        savedMessages: saved.chat_history.length > 0 ? saved.chat_history : undefined,
      });
    } catch {
      setState({ stage: "idle" });
    }
  }, []);

  const handleUpload = async (file: File) => {
    setState({ stage: "uploading" });
    try {
      const result = await parseAPAS(file);
      setState({
        stage: "confirming",
        sessionToken: result.session_token,
        parsedData: result.data,
      });
    } catch (err: any) {
      setState({ stage: "error", message: err.message });
    }
  };

  const handleConfirm = () => {
    if (state.stage !== "confirming") return;
    setState({
      stage: "preferences",
      sessionToken: state.sessionToken,
      parsedData: state.parsedData,
    });
  };

  const handlePreferences = async (prefs: PreferenceData) => {
    if (state.stage !== "preferences") return;
    const { sessionToken, parsedData } = state;
    setState({ stage: "optimizing", sessionToken, parsedData });
    try {
      const plan = await optimizePlan(sessionToken, prefs);
      localStorage.setItem(SESSION_KEY, sessionToken);
      setState({ stage: "plan", sessionToken, parsedData, plan });
    } catch (err: any) {
      setState({ stage: "error", message: err.message });
    }
  };

  const handleReset = () => {
    localStorage.removeItem(SESSION_KEY);
    setState({ stage: "idle" });
  };

  if (state.stage === "restoring") {
    return <LoadingStage message="Loading your plan..." />;
  }

  if (state.stage === "idle") {
    return (
      <UploadStage
        onUpload={handleUpload}
        savedToken={state.savedToken}
        onResume={state.savedToken ? handleResume : undefined}
      />
    );
  }

  if (state.stage === "uploading") {
    return <LoadingStage message="Parsing your APAS with AI..." />;
  }

  if (state.stage === "confirming") {
    return (
      <ConfirmStage
        parsedData={state.parsedData}
        onConfirm={handleConfirm}
        onReset={handleReset}
      />
    );
  }

  if (state.stage === "preferences") {
    return (
      <PreferencesStage
        parsedData={state.parsedData}
        onSubmit={handlePreferences}
        onReset={() =>
          setState({
            stage: "confirming",
            sessionToken: state.sessionToken,
            parsedData: state.parsedData,
          })
        }
      />
    );
  }

  if (state.stage === "optimizing") {
    return <LoadingStage message="Building your semester plan..." />;
  }

  if (state.stage === "plan") {
    return (
      <PlanStage
        sessionToken={state.sessionToken}
        parsedData={state.parsedData}
        plan={state.plan}
        onReset={handleReset}
        savedMessages={state.savedMessages}
      />
    );
  }

  if (state.stage === "error") {
    return (
      <div className="min-h-screen bg-gray-50 flex flex-col items-center justify-center p-8">
        <div className="max-w-lg w-full text-center">
          <div className="text-4xl mb-4">❌</div>
          <h2 className="text-xl font-bold text-gray-900 mb-2">
            Something went wrong
          </h2>
          <p className="text-gray-600 mb-6">{state.message}</p>
          <button
            onClick={handleReset}
            className="bg-red-600 text-white px-6 py-2.5 rounded-lg text-sm font-medium hover:bg-red-700"
          >
            Try Again
          </button>
        </div>
      </div>
    );
  }
}