import { useState, useEffect, useCallback, useMemo } from "react";
import Modal from "@cloudscape-design/components/modal";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Select from "@cloudscape-design/components/select";
import type { SelectProps } from "@cloudscape-design/components/select";
import Table from "@cloudscape-design/components/table";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Spinner from "@cloudscape-design/components/spinner";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import { formatTimestamp } from "../context/EvaluationContext";
import type { RunIndexEntry, Manifest } from "../context/EvaluationContext";
import type { EvaluationReport } from "../types/evaluation";

interface RunComparisonModalProps {
  visible: boolean;
  onDismiss: () => void;
  runs: RunIndexEntry[];
}

interface RunDetail {
  runId: string;
  timestamp: string;
  overallScore: number;
  evaluatorScores: Record<string, number>;
  passRate: number;
  totalCases: number;
}

interface ComparisonRow {
  evaluator: string;
  run1Score: number | null;
  run2Score: number | null;
  run3Score: number | null;
  diff12: number | null;
  diff23: number | null;
}

export default function RunComparisonModal({ visible, onDismiss, runs }: RunComparisonModalProps) {
  const [selectedRun1, setSelectedRun1] = useState<SelectProps.Option | null>(null);
  const [selectedRun2, setSelectedRun2] = useState<SelectProps.Option | null>(null);
  const [selectedRun3, setSelectedRun3] = useState<SelectProps.Option | null>(null);
  const [runDetails, setRunDetails] = useState<Map<string, RunDetail>>(new Map());
  const [loading, setLoading] = useState(false);

  // Run options for dropdowns
  const runOptions: SelectProps.Options = useMemo(() => {
    return runs.map((run) => ({
      value: run.run_id,
      label: formatTimestamp(run.timestamp),
      description: `${run.total_cases} cases`,
    }));
  }, [runs]);

  // Load run details when selected
  const loadRunDetail = useCallback(async (runId: string): Promise<RunDetail | null> => {
    try {
      const manifestRes = await fetch(`/runs/${runId}/manifest.json`);
      if (!manifestRes.ok) return null;

      const manifest: Manifest = await manifestRes.json();
      const evaluatorScores: Record<string, number> = {};
      let totalScore = 0;
      let totalPasses = 0;
      let totalTests = 0;

      for (const file of manifest.files) {
        const res = await fetch(`/runs/${runId}/${file}`);
        if (!res.ok) continue;

        const report: EvaluationReport = await res.json();
        const evaluatorName = file.replace("eval_", "").replace(".json", "");
        evaluatorScores[evaluatorName] = report.overall_score;
        totalScore += report.overall_score;
        totalPasses += report.test_passes.filter(Boolean).length;
        totalTests += report.test_passes.length;
      }

      return {
        runId,
        timestamp: manifest.timestamp,
        overallScore: manifest.files.length > 0 ? totalScore / manifest.files.length : 0,
        evaluatorScores,
        passRate: totalTests > 0 ? totalPasses / totalTests : 0,
        totalCases: manifest.total_cases,
      };
    } catch (err) {
      console.error(`Failed to load run ${runId}:`, err);
      return null;
    }
  }, []);

  // Load selected runs
  useEffect(() => {
    const loadSelectedRuns = async () => {
      const runsToLoad: string[] = [];
      if (selectedRun1?.value && !runDetails.has(selectedRun1.value)) {
        runsToLoad.push(selectedRun1.value);
      }
      if (selectedRun2?.value && !runDetails.has(selectedRun2.value)) {
        runsToLoad.push(selectedRun2.value);
      }
      if (selectedRun3?.value && !runDetails.has(selectedRun3.value)) {
        runsToLoad.push(selectedRun3.value);
      }

      if (runsToLoad.length === 0) return;

      setLoading(true);
      const newDetails = new Map(runDetails);
      for (const runId of runsToLoad) {
        const detail = await loadRunDetail(runId);
        if (detail) {
          newDetails.set(runId, detail);
        }
      }
      setRunDetails(newDetails);
      setLoading(false);
    };

    loadSelectedRuns();
  }, [selectedRun1, selectedRun2, selectedRun3, loadRunDetail, runDetails]);

  // Get run detail by selection
  const run1 = selectedRun1?.value ? runDetails.get(selectedRun1.value) : null;
  const run2 = selectedRun2?.value ? runDetails.get(selectedRun2.value) : null;
  const run3 = selectedRun3?.value ? runDetails.get(selectedRun3.value) : null;

  // Build comparison table rows
  const comparisonRows: ComparisonRow[] = useMemo(() => {
    const evaluatorSet = new Set<string>();
    [run1, run2, run3].forEach((run) => {
      if (run) {
        Object.keys(run.evaluatorScores).forEach((e) => evaluatorSet.add(e));
      }
    });

    return Array.from(evaluatorSet)
      .sort()
      .map((evaluator) => {
        const s1 = run1?.evaluatorScores[evaluator] ?? null;
        const s2 = run2?.evaluatorScores[evaluator] ?? null;
        const s3 = run3?.evaluatorScores[evaluator] ?? null;

        return {
          evaluator,
          run1Score: s1,
          run2Score: s2,
          run3Score: s3,
          diff12: s1 !== null && s2 !== null ? s2 - s1 : null,
          diff23: s2 !== null && s3 !== null ? s3 - s2 : null,
        };
      });
  }, [run1, run2, run3]);

  const renderScore = (score: number | null) => {
    if (score === null) return "-";
    const pct = Math.round(score * 100);
    return (
      <StatusIndicator type={score >= 0.8 ? "success" : score >= 0.5 ? "warning" : "error"}>
        {pct}%
      </StatusIndicator>
    );
  };

  const renderDiff = (diff: number | null) => {
    if (diff === null) return "-";
    const pct = Math.round(diff * 100);
    if (pct === 0) return "—";
    return (
      <span
        style={{
          color: pct >= 0 ? "#16a34a" : "#dc2626",
          fontWeight: 500,
        }}
      >
        {pct >= 0 ? "↑" : "↓"} {Math.abs(pct)}%
      </span>
    );
  };

  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      header="Compare Evaluation Runs"
      size="max"
      footer={
        <Box float="right">
          <Button variant="primary" onClick={onDismiss}>
            Close
          </Button>
        </Box>
      }
    >
      <SpaceBetween size="l">
        {/* Run Selectors */}
        <ColumnLayout columns={3}>
          <div>
            <Box variant="awsui-key-label" margin={{ bottom: "xs" }}>
              Run 1 (Baseline)
            </Box>
            <Select
              selectedOption={selectedRun1}
              onChange={({ detail }) => setSelectedRun1(detail.selectedOption)}
              options={runOptions}
              placeholder="Select first run"
            />
          </div>
          <div>
            <Box variant="awsui-key-label" margin={{ bottom: "xs" }}>
              Run 2
            </Box>
            <Select
              selectedOption={selectedRun2}
              onChange={({ detail }) => setSelectedRun2(detail.selectedOption)}
              options={runOptions}
              placeholder="Select second run"
            />
          </div>
          <div>
            <Box variant="awsui-key-label" margin={{ bottom: "xs" }}>
              Run 3 (Optional)
            </Box>
            <Select
              selectedOption={selectedRun3}
              onChange={({ detail }) => setSelectedRun3(detail.selectedOption)}
              options={[{ value: "", label: "None" }, ...runOptions]}
              placeholder="Select third run"
            />
          </div>
        </ColumnLayout>

        {loading && (
          <Box textAlign="center" padding="l">
            <Spinner size="large" />
          </Box>
        )}

        {/* Summary Comparison */}
        {(run1 || run2 || run3) && !loading && (
          <ColumnLayout columns={3} variant="text-grid">
            {run1 && (
              <Box>
                <Box variant="awsui-key-label">Run 1 Overall</Box>
                <Box fontSize="heading-xl">{Math.round(run1.overallScore * 100)}%</Box>
                <Box color="text-status-inactive">
                  Pass rate: {Math.round(run1.passRate * 100)}%
                </Box>
              </Box>
            )}
            {run2 && (
              <Box>
                <Box variant="awsui-key-label">
                  Run 2 Overall
                  {run1 && (
                    <span style={{ marginLeft: 8 }}>
                      {renderDiff(run2.overallScore - run1.overallScore)}
                    </span>
                  )}
                </Box>
                <Box fontSize="heading-xl">{Math.round(run2.overallScore * 100)}%</Box>
                <Box color="text-status-inactive">
                  Pass rate: {Math.round(run2.passRate * 100)}%
                </Box>
              </Box>
            )}
            {run3 && (
              <Box>
                <Box variant="awsui-key-label">
                  Run 3 Overall
                  {run2 && (
                    <span style={{ marginLeft: 8 }}>
                      {renderDiff(run3.overallScore - run2.overallScore)}
                    </span>
                  )}
                </Box>
                <Box fontSize="heading-xl">{Math.round(run3.overallScore * 100)}%</Box>
                <Box color="text-status-inactive">
                  Pass rate: {Math.round(run3.passRate * 100)}%
                </Box>
              </Box>
            )}
          </ColumnLayout>
        )}

        {/* Detailed Comparison Table */}
        {comparisonRows.length > 0 && !loading && (
          <Table
            columnDefinitions={[
              {
                id: "evaluator",
                header: "Evaluator",
                cell: (item) => item.evaluator,
                width: 200,
              },
              {
                id: "run1",
                header: "Run 1",
                cell: (item) => renderScore(item.run1Score),
              },
              {
                id: "diff12",
                header: "Δ 1→2",
                cell: (item) => renderDiff(item.diff12),
              },
              {
                id: "run2",
                header: "Run 2",
                cell: (item) => renderScore(item.run2Score),
              },
              {
                id: "diff23",
                header: "Δ 2→3",
                cell: (item) => (selectedRun3?.value ? renderDiff(item.diff23) : "-"),
              },
              {
                id: "run3",
                header: "Run 3",
                cell: (item) => (selectedRun3?.value ? renderScore(item.run3Score) : "-"),
              },
            ]}
            items={comparisonRows}
            trackBy="evaluator"
            stripedRows
            variant="embedded"
          />
        )}

        {!run1 && !run2 && !loading && (
          <Box textAlign="center" color="text-status-inactive" padding="l">
            Select runs above to compare their scores
          </Box>
        )}
      </SpaceBetween>
    </Modal>
  );
}
