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

import { useState, useCallback } from "react";
import { parseAPAS, optimizePlan, ParsedAPAS, Plan } from "@/lib/api";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type AppState =
  | { stage: "idle" }
  | { stage: "uploading" }
  | { stage: "confirming"; sessionToken: string; parsedData: ParsedAPAS }
  | { stage: "optimizing"; sessionToken: string; parsedData: ParsedAPAS }
  | { stage: "plan"; parsedData: ParsedAPAS; plan: Plan }
  | { stage: "error"; message: string };

// ---------------------------------------------------------------------------
// Upload component
// ---------------------------------------------------------------------------

function UploadStage({ onUpload }: { onUpload: (file: File) => void }) {
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
// Plan display component
// ---------------------------------------------------------------------------

function PlanStage({
  parsedData,
  plan,
  onReset,
}: {
  parsedData: ParsedAPAS;
  plan: Plan;
  onReset: () => void;
}) {
  const termColors: Record<string, string> = {
    F26: "bg-orange-50 border-orange-200",
    SP27: "bg-blue-50 border-blue-200",
    F27: "bg-orange-50 border-orange-200",
    SP28: "bg-blue-50 border-blue-200",
  };

  return (
    <div className="min-h-screen bg-gray-50 p-8">
      <div className="max-w-4xl mx-auto">
        {/* Header */}
        <div className="flex items-center justify-between mb-8">
          <div>
            <h1 className="text-3xl font-bold text-gray-900">
              Gopher<span className="text-red-600">Path</span>
            </h1>
            <p className="text-gray-600 mt-1">
              {parsedData.student.name} · {parsedData.student.major} ·
              Graduating {parsedData.student.expected_graduation}
            </p>
          </div>
          <button
            onClick={onReset}
            className="text-sm text-gray-500 hover:text-gray-700 underline"
          >
            Start over
          </button>
        </div>

        {/* Status banner */}
        {plan.status === "partial" && (
          <div className="bg-yellow-50 border border-yellow-200 rounded-xl p-4 mb-6 text-sm text-yellow-800">
            ⚠️ {plan.message}
          </div>
        )}

        {/* Plan grid */}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6 mb-8">
          {plan.plan.map((term) => (
            <div
              key={term.term_code}
              className={`rounded-xl border p-5 ${
                termColors[term.term_code] || "bg-gray-50 border-gray-200"
              }`}
            >
              <div className="flex items-center justify-between mb-4">
                <h3 className="font-semibold text-gray-900">
                  {term.term_label}
                </h3>
                <span className="text-sm text-gray-500">
                  {term.total_credits} credits
                </span>
              </div>

              {term.courses.length === 0 ? (
                <p className="text-sm text-gray-400 italic">No courses scheduled</p>
              ) : (
                <div className="space-y-2">
                  {term.courses.map((course, idx) => (
                    <div
                      key={idx}
                      className="bg-white rounded-lg p-3 border border-white/80"
                    >
                      <div className="flex items-start justify-between gap-2">
                        <div>
                          <span className="text-xs font-mono font-medium text-gray-500">
                            {course.subject} {course.number}
                          </span>
                          <p className="text-sm font-medium text-gray-900 mt-0.5">
                            {course.title}
                          </p>
                          <p className="text-xs text-gray-400 mt-0.5">
                            {course.requirement_category}
                          </p>
                        </div>
                        <span className="text-xs text-gray-500 shrink-0">
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
          <div className="bg-white rounded-xl border border-gray-200 p-6">
            <h3 className="font-semibold text-gray-900 mb-3">
              Could Not Schedule
            </h3>
            <div className="space-y-2">
              {plan.unscheduled.map((course, idx) => (
                <div key={idx} className="flex justify-between text-sm">
                  <span className="text-gray-700">
                    {course.subject} {course.number} — {course.title}
                  </span>
                  <span className="text-gray-400">
                    {course.requirement_category}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main page — state machine
// ---------------------------------------------------------------------------

export default function Home() {
  const [state, setState] = useState<AppState>({ stage: "idle" });

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

  const handleConfirm = async () => {
    if (state.stage !== "confirming") return;
    const { sessionToken, parsedData } = state;
    setState({ stage: "optimizing", sessionToken, parsedData });
    try {
      const plan = await optimizePlan(sessionToken);
      setState({ stage: "plan", parsedData, plan });
    } catch (err: any) {
      setState({ stage: "error", message: err.message });
    }
  };

  const handleReset = () => setState({ stage: "idle" });

  if (state.stage === "idle") {
    return <UploadStage onUpload={handleUpload} />;
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

  if (state.stage === "optimizing") {
    return <LoadingStage message="Generating your course plan..." />;
  }

  if (state.stage === "plan") {
    return (
      <PlanStage
        parsedData={state.parsedData}
        plan={state.plan}
        onReset={handleReset}
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