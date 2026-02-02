import { createContext, useContext, useState, useCallback, useEffect } from "react";
import type { ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import type { SelectProps } from "@cloudscape-design/components/select";
import type { EvaluationReport, Session } from "../types/evaluation";

export interface EvaluatorData {
  name: string;
  report: EvaluationReport;
}

export interface Manifest {
  run_id?: string;
  timestamp: string;
  evaluators: string[];
  total_cases: number;
  files: string[];
}

interface RunIndexEntry {
  run_id: string;
  timestamp: string;
  total_cases: number;
  evaluator_count: number;
}

interface RunsIndex {
  runs: RunIndexEntry[];
}

// Helper functions
export function formatTimestamp(isoString: string): string {
  try {
    return new Date(isoString).toLocaleString();
  } catch {
    return isoString;
  }
}

export function extractToolNames(trajectory: string[] | Session | undefined): string[] {
  if (!trajectory) return [];
  
  if (Array.isArray(trajectory)) {
    return trajectory;
  }
  
  const toolNames: string[] = [];
  if (trajectory.traces) {
    for (const trace of trajectory.traces) {
      if (trace.spans) {
        for (const span of trace.spans) {
          if (span.span_type === "execute_tool") {
            const toolSpan = span as { tool_call: { name: string } };
            if (toolSpan.tool_call?.name) {
              toolNames.push(toolSpan.tool_call.name);
            }
          }
        }
      }
    }
  }
  return toolNames;
}

export function getScoreColor(score: number): "success" | "warning" | "error" {
  if (score >= 0.8) return "success";
  if (score >= 0.5) return "warning";
  return "error";
}

export function getStatusType(score: number): "success" | "warning" | "error" {
  if (score >= 0.8) return "success";
  if (score >= 0.5) return "warning";
  return "error";
}

// Context
interface EvaluationContextType {
  evaluators: EvaluatorData[];
  manifest: Manifest | null;
  loading: boolean;
  error: string | null;
  setError: (error: string | null) => void;
  selectedCase: number;
  setSelectedCase: (index: number) => void;
  runsIndex: RunsIndex | null;
  selectedRun: SelectProps.Option | null;
  runOptions: SelectProps.Options;
  handleRunChange: (option: SelectProps.Option) => Promise<void>;
  handleRefresh: () => Promise<void>;
  handleUpload: (files: File[]) => Promise<void>;
}

const EvaluationContext = createContext<EvaluationContextType | null>(null);

export function useEvaluation() {
  const context = useContext(EvaluationContext);
  if (!context) {
    throw new Error("useEvaluation must be used within EvaluationProvider");
  }
  return context;
}

export function EvaluationProvider({ children }: { children: ReactNode }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const [evaluators, setEvaluators] = useState<EvaluatorData[]>([]);
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedCase, setSelectedCase] = useState<number>(0);
  const [runsIndex, setRunsIndex] = useState<RunsIndex | null>(null);
  const [selectedRun, setSelectedRun] = useState<SelectProps.Option | null>(null);

  const loadRunsIndex = useCallback(async () => {
    try {
      const res = await fetch("/runs_index.json");
      if (res.ok) {
        const data: RunsIndex = await res.json();
        setRunsIndex(data);
        return data;
      }
    } catch {
      // No runs index yet
    }
    return null;
  }, []);

  const loadRun = useCallback(async (runId: string) => {
    setLoading(true);
    setError(null);
    try {
      const manifestRes = await fetch(`/runs/${runId}/manifest.json`);
      if (!manifestRes.ok) {
        setError(`Failed to load run ${runId}`);
        setLoading(false);
        return;
      }
      const manifestData: Manifest = await manifestRes.json();
      setManifest(manifestData);

      const evaluatorData: EvaluatorData[] = [];
      for (const file of manifestData.files) {
        const res = await fetch(`/runs/${runId}/${file}`);
        if (res.ok) {
          const report: EvaluationReport = await res.json();
          const name = file.replace("eval_", "").replace(".json", "");
          evaluatorData.push({ name, report });
        }
      }
      setEvaluators(evaluatorData);
      setSelectedCase(0);
    } catch (err) {
      setError(`Failed to load run: ${err}`);
    }
    setLoading(false);
  }, []);

  const loadLegacyFormat = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const manifestRes = await fetch("/eval_manifest.json");
      if (!manifestRes.ok) {
        setError("No evaluation data found. Run evaluations or upload JSON files.");
        setLoading(false);
        return false;
      }
      const manifestData: Manifest = await manifestRes.json();
      setManifest(manifestData);

      const evaluatorData: EvaluatorData[] = [];
      for (const file of manifestData.files) {
        const res = await fetch(`/${file}`);
        if (res.ok) {
          const report: EvaluationReport = await res.json();
          const name = file.replace("eval_", "").replace(".json", "");
          evaluatorData.push({ name, report });
        }
      }
      setEvaluators(evaluatorData);
      setSelectedCase(0);
      return true;
    } catch {
      return false;
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const init = async () => {
      const index = await loadRunsIndex();
      if (index && index.runs.length > 0) {
        const runIdFromUrl = searchParams.get("run");
        const targetRun = runIdFromUrl 
          ? index.runs.find(r => r.run_id === runIdFromUrl) || index.runs[0]
          : index.runs[0];
        setSelectedRun({ value: targetRun.run_id, label: formatTimestamp(targetRun.timestamp) });
        await loadRun(targetRun.run_id);
      } else {
        await loadLegacyFormat();
      }
    };
    init();
  }, [loadRunsIndex, loadRun, loadLegacyFormat, searchParams]);

  const handleRunChange = async (option: SelectProps.Option) => {
    setSelectedRun(option);
    if (option.value) {
      setSearchParams({ run: option.value });
      await loadRun(option.value);
    }
  };

  const handleRefresh = async () => {
    const index = await loadRunsIndex();
    if (selectedRun?.value) {
      await loadRun(selectedRun.value);
    } else if (index && index.runs.length > 0) {
      const latestRun = index.runs[0];
      setSelectedRun({ value: latestRun.run_id, label: formatTimestamp(latestRun.timestamp) });
      await loadRun(latestRun.run_id);
    } else {
      await loadLegacyFormat();
    }
  };

  const handleUpload = async (files: File[]) => {
    if (files.length === 0) return;

    const evaluatorData: EvaluatorData[] = [];

    for (const file of files) {
      try {
        const text = await file.text();
        const report: EvaluationReport = JSON.parse(text);
        const name = file.name.replace("eval_", "").replace(".json", "");
        evaluatorData.push({ name, report });
      } catch (err) {
        console.error(`Failed to parse ${file.name}:`, err);
      }
    }

    if (evaluatorData.length > 0) {
      setEvaluators(evaluatorData);
      setManifest({
        timestamp: new Date().toISOString(),
        evaluators: evaluatorData.map((e) => e.name),
        total_cases: evaluatorData[0]?.report.cases.length || 0,
        files: files.map((f) => f.name),
      });
      setSelectedRun(null);
      setError(null);
      setSelectedCase(0);
    }
  };

  const runOptions: SelectProps.Options = runsIndex?.runs.map((run) => ({
    value: run.run_id,
    label: formatTimestamp(run.timestamp),
    description: `${run.total_cases} cases, ${run.evaluator_count} evaluators`,
  })) || [];

  const value: EvaluationContextType = {
    evaluators,
    manifest,
    loading,
    error,
    setError,
    selectedCase,
    setSelectedCase,
    runsIndex,
    selectedRun,
    runOptions,
    handleRunChange,
    handleRefresh,
    handleUpload,
  };

  return (
    <EvaluationContext.Provider value={value}>
      {children}
    </EvaluationContext.Provider>
  );
}
