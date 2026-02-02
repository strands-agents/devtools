import { useState, useEffect, useCallback, useMemo } from "react";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Box from "@cloudscape-design/components/box";
import Select from "@cloudscape-design/components/select";
import type { SelectProps } from "@cloudscape-design/components/select";
import Table from "@cloudscape-design/components/table";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Link from "@cloudscape-design/components/link";
import Spinner from "@cloudscape-design/components/spinner";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
} from "recharts";
import Layout from "../components/Layout";
import {
  useEvaluation,
  formatTimestamp,
  getAgentTypeDisplayName,
  getUniqueAgentTypes,
  filterRunsByAgentType,
} from "../context/EvaluationContext";
import type { RunIndexEntry, Manifest } from "../context/EvaluationContext";
import type { EvaluationReport } from "../types/evaluation";

interface RunWithScores extends RunIndexEntry {
  overallScore: number;
  passRate: number;
  evaluatorScores: Record<string, number>;
  previousRun?: RunWithScores;
  scoreDelta?: number;
}

// Colors for different evaluators
const EVALUATOR_COLORS = [
  "#2563eb", // blue
  "#16a34a", // green
  "#dc2626", // red
  "#9333ea", // purple
  "#ea580c", // orange
  "#0891b2", // cyan
  "#4f46e5", // indigo
  "#be185d", // pink
];

// Timeframe options
const TIMEFRAME_OPTIONS: SelectProps.Options = [
  { value: "5", label: "Last 5 days" },
  { value: "7", label: "Last 7 days" },
  { value: "14", label: "Last 14 days" },
  { value: "30", label: "Last 30 days" },
  { value: "all", label: "All time (max 50)" },
];

export default function AgentProgressPage() {
  const { runsIndex, handleRunChange } = useEvaluation();
  const [selectedAgentType, setSelectedAgentType] = useState<SelectProps.Option | null>(null);
  const [selectedTimeframe, setSelectedTimeframe] = useState<SelectProps.Option>(TIMEFRAME_OPTIONS[0]);
  const [runsWithScores, setRunsWithScores] = useState<RunWithScores[]>([]);
  const [loading, setLoading] = useState(false);
  const [allEvaluators, setAllEvaluators] = useState<string[]>([]);

  // Get unique agent types from runs index
  const agentTypes = useMemo(() => {
    if (!runsIndex) return [];
    return getUniqueAgentTypes(runsIndex.runs);
  }, [runsIndex]);

  // Agent type options for dropdown
  const agentTypeOptions: SelectProps.Options = useMemo(() => {
    return agentTypes.map((type) => ({
      value: type,
      label: getAgentTypeDisplayName(type),
    }));
  }, [agentTypes]);

  // Auto-select first agent type
  useEffect(() => {
    if (agentTypeOptions.length > 0 && !selectedAgentType) {
      setSelectedAgentType(agentTypeOptions[0]);
    }
  }, [agentTypeOptions, selectedAgentType]);

  // Load scores for all runs of selected agent type
  const loadRunScores = useCallback(async (agentType: string) => {
    if (!runsIndex) return;

    setLoading(true);
    let filteredRuns = filterRunsByAgentType(runsIndex.runs, agentType);
    
    // Filter by timeframe
    if (selectedTimeframe.value !== "all") {
      const days = parseInt(selectedTimeframe.value as string, 10);
      const cutoffDate = new Date();
      cutoffDate.setDate(cutoffDate.getDate() - days);
      filteredRuns = filteredRuns.filter(r => new Date(r.timestamp) >= cutoffDate);
    }
    // Cap at 50 runs for performance
    filteredRuns = filteredRuns.slice(0, 50);
    
    const runsData: RunWithScores[] = [];
    const evaluatorSet = new Set<string>();

    // Load each run's manifest and calculate scores
    for (const run of filteredRuns) {
      try {
        const manifestRes = await fetch(`/runs/${run.run_id}/manifest.json`);
        if (!manifestRes.ok) continue;

        const manifest: Manifest = await manifestRes.json();
        const evaluatorScores: Record<string, number> = {};
        let totalScore = 0;
        let totalPasses = 0;
        let totalTests = 0;

        // Load each evaluator file
        for (const file of manifest.files) {
          const res = await fetch(`/runs/${run.run_id}/${file}`);
          if (!res.ok) continue;

          const report: EvaluationReport = await res.json();
          const evaluatorName = file.replace("eval_", "").replace(".json", "");
          evaluatorSet.add(evaluatorName);

          evaluatorScores[evaluatorName] = report.overall_score;
          totalScore += report.overall_score;
          totalPasses += report.test_passes.filter(Boolean).length;
          totalTests += report.test_passes.length;
        }

        const overallScore = manifest.files.length > 0 ? totalScore / manifest.files.length : 0;
        const passRate = totalTests > 0 ? totalPasses / totalTests : 0;

        runsData.push({
          ...run,
          overallScore,
          passRate,
          evaluatorScores,
        });
      } catch (err) {
        console.error(`Failed to load run ${run.run_id}:`, err);
      }
    }

    // Sort by timestamp (oldest first for chart)
    runsData.sort((a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime());

    // Calculate deltas from previous run
    for (let i = 1; i < runsData.length; i++) {
      runsData[i].previousRun = runsData[i - 1];
      runsData[i].scoreDelta = runsData[i].overallScore - runsData[i - 1].overallScore;
    }

    setAllEvaluators(Array.from(evaluatorSet).sort());
    setRunsWithScores(runsData);
    setLoading(false);
  }, [runsIndex, selectedTimeframe]);

  // Load data when agent type or timeframe changes
  useEffect(() => {
    if (selectedAgentType?.value) {
      loadRunScores(selectedAgentType.value);
    }
  }, [selectedAgentType, loadRunScores]);

  // Prepare chart data
  const chartData = useMemo(() => {
    return runsWithScores.map((run) => ({
      timestamp: new Date(run.timestamp).toLocaleDateString(),
      fullTimestamp: run.timestamp,
      runId: run.run_id,
      overall: Math.round(run.overallScore * 100),
      passRate: Math.round(run.passRate * 100),
      ...Object.fromEntries(
        Object.entries(run.evaluatorScores).map(([name, score]) => [
          name,
          Math.round(score * 100),
        ])
      ),
    }));
  }, [runsWithScores]);

  // Table items (reverse order - newest first)
  const tableItems = useMemo(() => {
    return [...runsWithScores].reverse();
  }, [runsWithScores]);

  const handleNavigateToRun = (runId: string) => {
    const run = runsIndex?.runs.find((r) => r.run_id === runId);
    if (run) {
      handleRunChange({
        value: run.run_id,
        label: formatTimestamp(run.timestamp),
      });
    }
  };

  return (
    <Layout
      title="Agent Progress"
      description="Track evaluation progress for a specific agent over time"
    >
      <SpaceBetween size="l">
        {/* Filters */}
        <Container
          header={<Header variant="h2">Filters</Header>}
        >
          <SpaceBetween size="m" direction="horizontal">
            <div style={{ minWidth: 200 }}>
              <Select
                selectedOption={selectedAgentType}
                onChange={({ detail }) => setSelectedAgentType(detail.selectedOption)}
                options={agentTypeOptions}
                placeholder="Select an agent type"
                expandToViewport
              />
            </div>
            <div style={{ minWidth: 180 }}>
              <Select
                selectedOption={selectedTimeframe}
                onChange={({ detail }) => setSelectedTimeframe(detail.selectedOption)}
                options={TIMEFRAME_OPTIONS}
              />
            </div>
          </SpaceBetween>
        </Container>

        {loading ? (
          <Container>
            <Box textAlign="center" padding="xl">
              <Spinner size="large" />
              <Box margin={{ top: "m" }}>Loading run data...</Box>
            </Box>
          </Container>
        ) : runsWithScores.length === 0 ? (
          <Container>
            <Box textAlign="center" color="text-status-inactive" padding="xl">
              No runs found for the selected agent type.
            </Box>
          </Container>
        ) : (
          <>
            {/* Score Trend Chart */}
            <Container
              header={
                <Header
                  variant="h2"
                  description={`${runsWithScores.length} evaluation runs`}
                >
                  Score Trend Over Time
                </Header>
              }
            >
              <Box padding="m">
                <ResponsiveContainer width="100%" height={400}>
                  <LineChart data={chartData}>
                    <CartesianGrid strokeDasharray="3 3" />
                    <XAxis
                      dataKey="timestamp"
                      tick={{ fontSize: 12 }}
                    />
                    <YAxis
                      domain={[0, 100]}
                      tick={{ fontSize: 12 }}
                      tickFormatter={(value) => `${value}%`}
                    />
                    <Tooltip
                      formatter={(value) => [`${value ?? 0}%`, ""]}
                      labelFormatter={(label) => `Date: ${label}`}
                    />
                    <Legend />
                    <Line
                      type="monotone"
                      dataKey="overall"
                      name="Overall Score"
                      stroke="#0972d3"
                      strokeWidth={3}
                      dot={{ r: 4 }}
                      activeDot={{ r: 6 }}
                    />
                    {allEvaluators.map((evaluator, idx) => (
                      <Line
                        key={evaluator}
                        type="monotone"
                        dataKey={evaluator}
                        name={evaluator}
                        stroke={EVALUATOR_COLORS[idx % EVALUATOR_COLORS.length]}
                        strokeWidth={1.5}
                        dot={{ r: 2 }}
                        strokeDasharray={idx > 3 ? "5 5" : undefined}
                      />
                    ))}
                  </LineChart>
                </ResponsiveContainer>
              </Box>
            </Container>

            {/* Summary Stats */}
            <Container
              header={<Header variant="h2">Progress Summary</Header>}
            >
              {runsWithScores.length >= 2 && (
                <SpaceBetween size="m" direction="horizontal">
                  <Box>
                    <Box variant="awsui-key-label">First Run Score</Box>
                    <Box fontSize="heading-xl">
                      {Math.round(runsWithScores[0].overallScore * 100)}%
                    </Box>
                  </Box>
                  <Box>
                    <Box variant="awsui-key-label">Latest Run Score</Box>
                    <Box fontSize="heading-xl">
                      {Math.round(runsWithScores[runsWithScores.length - 1].overallScore * 100)}%
                    </Box>
                  </Box>
                  <Box>
                    <Box variant="awsui-key-label">Total Change</Box>
                    <Box fontSize="heading-xl">
                      {(() => {
                        const delta =
                          runsWithScores[runsWithScores.length - 1].overallScore -
                          runsWithScores[0].overallScore;
                        const deltaPercent = Math.round(delta * 100);
                        return (
                          <span
                            style={{
                              color: delta >= 0 ? "#16a34a" : "#dc2626",
                            }}
                          >
                            {delta >= 0 ? "↑" : "↓"} {Math.abs(deltaPercent)}%
                          </span>
                        );
                      })()}
                    </Box>
                  </Box>
                  <Box>
                    <Box variant="awsui-key-label">Total Runs</Box>
                    <Box fontSize="heading-xl">{runsWithScores.length}</Box>
                  </Box>
                </SpaceBetween>
              )}
            </Container>

            {/* Runs Table */}
            <Container
              header={
                <Header variant="h2" description="Click a run to view details">
                  All Runs
                </Header>
              }
            >
              <Table
                columnDefinitions={[
                  {
                    id: "timestamp",
                    header: "Timestamp",
                    cell: (item) => (
                      <Link onFollow={() => handleNavigateToRun(item.run_id)}>
                        {formatTimestamp(item.timestamp)}
                      </Link>
                    ),
                    sortingField: "timestamp",
                  },
                  {
                    id: "score",
                    header: "Overall Score",
                    cell: (item) => (
                      <StatusIndicator
                        type={
                          item.overallScore >= 0.8
                            ? "success"
                            : item.overallScore >= 0.5
                            ? "warning"
                            : "error"
                        }
                      >
                        {Math.round(item.overallScore * 100)}%
                      </StatusIndicator>
                    ),
                    sortingField: "overallScore",
                  },
                  {
                    id: "delta",
                    header: "Change",
                    cell: (item) => {
                      if (item.scoreDelta === undefined) return "-";
                      const deltaPercent = Math.round(item.scoreDelta * 100);
                      if (deltaPercent === 0) return "—";
                      return (
                        <span
                          style={{
                            color: deltaPercent >= 0 ? "#16a34a" : "#dc2626",
                            fontWeight: 500,
                          }}
                        >
                          {deltaPercent >= 0 ? "↑" : "↓"} {Math.abs(deltaPercent)}%
                        </span>
                      );
                    },
                  },
                  {
                    id: "passRate",
                    header: "Pass Rate",
                    cell: (item) => `${Math.round(item.passRate * 100)}%`,
                  },
                  {
                    id: "cases",
                    header: "Test Cases",
                    cell: (item) => item.total_cases,
                  },
                  {
                    id: "evaluators",
                    header: "Evaluators",
                    cell: (item) => item.evaluator_count,
                  },
                ]}
                items={tableItems}
                trackBy="run_id"
                stripedRows
                sortingDisabled={false}
                variant="embedded"
              />
            </Container>
          </>
        )}
      </SpaceBetween>
    </Layout>
  );
}
