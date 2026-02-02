import { useState, useEffect, useCallback, useMemo } from "react";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Select from "@cloudscape-design/components/select";
import type { SelectProps } from "@cloudscape-design/components/select";
import Spinner from "@cloudscape-design/components/spinner";
import Toggle from "@cloudscape-design/components/toggle";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
  ReferenceLine,
} from "recharts";
import Layout from "../components/Layout";
import {
  useEvaluation,
  inferAgentType,
  getAgentTypeDisplayName,
  getUniqueAgentTypes,
} from "../context/EvaluationContext";
import type { RunIndexEntry, Manifest } from "../context/EvaluationContext";
import type { EvaluationReport } from "../types/evaluation";

interface RunWithScores extends RunIndexEntry {
  overallScore: number;
  passRate: number;
  evaluatorScores: Record<string, number>;
  agentType: string;
}

// Colors for different evaluators
const EVALUATOR_COLORS = [
  "#0972d3", // aws blue
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

export default function ScoreTrendsPage() {
  const { runsIndex } = useEvaluation();
  const [runsWithScores, setRunsWithScores] = useState<RunWithScores[]>([]);
  const [loading, setLoading] = useState(false);
  const [selectedAgentType, setSelectedAgentType] = useState<SelectProps.Option | null>(null);
  const [selectedTimeframe, setSelectedTimeframe] = useState<SelectProps.Option>(TIMEFRAME_OPTIONS[0]);
  const [showEvaluators, setShowEvaluators] = useState(true);
  const [showThreshold, setShowThreshold] = useState(true);

  // Get unique agent types
  const agentTypes = useMemo(() => {
    if (!runsIndex) return [];
    return getUniqueAgentTypes(runsIndex.runs);
  }, [runsIndex]);

  // Agent type options for dropdown (with "All" option)
  const agentTypeOptions: SelectProps.Options = useMemo(() => {
    const options: SelectProps.Options = [
      { value: "all", label: "All Agents" },
    ];
    return options.concat(
      agentTypes.map((type) => ({
        value: type,
        label: getAgentTypeDisplayName(type),
      }))
    );
  }, [agentTypes]);

  // Auto-select "All" on load
  useEffect(() => {
    if (agentTypeOptions.length > 0 && !selectedAgentType) {
      setSelectedAgentType(agentTypeOptions[0]);
    }
  }, [agentTypeOptions, selectedAgentType]);

  // Load all run scores
  const loadAllRunScores = useCallback(async () => {
    if (!runsIndex) return;

    setLoading(true);
    const runsData: RunWithScores[] = [];

    // Filter runs by timeframe
    let runsToLoad = runsIndex.runs;
    if (selectedTimeframe.value !== "all") {
      const days = parseInt(selectedTimeframe.value as string, 10);
      const cutoffDate = new Date();
      cutoffDate.setDate(cutoffDate.getDate() - days);
      runsToLoad = runsIndex.runs.filter(r => new Date(r.timestamp) >= cutoffDate);
    }
    // Cap at 50 runs for performance
    runsToLoad = runsToLoad.slice(0, 50);

    for (const run of runsToLoad) {
      try {
        const manifestRes = await fetch(`/runs/${run.run_id}/manifest.json`);
        if (!manifestRes.ok) continue;

        const manifest: Manifest = await manifestRes.json();
        const evaluatorScores: Record<string, number> = {};
        let totalScore = 0;
        let totalPasses = 0;
        let totalTests = 0;

        for (const file of manifest.files) {
          const res = await fetch(`/runs/${run.run_id}/${file}`);
          if (!res.ok) continue;

          const report: EvaluationReport = await res.json();
          const evaluatorName = file.replace("eval_", "").replace(".json", "");

          evaluatorScores[evaluatorName] = report.overall_score;
          totalScore += report.overall_score;
          totalPasses += report.test_passes.filter(Boolean).length;
          totalTests += report.test_passes.length;
        }

        const overallScore = manifest.files.length > 0 ? totalScore / manifest.files.length : 0;
        const passRate = totalTests > 0 ? totalPasses / totalTests : 0;
        const agentType = inferAgentType(run.run_id, run.agent_type);

        runsData.push({
          ...run,
          overallScore,
          passRate,
          evaluatorScores,
          agentType,
        });
      } catch (err) {
        console.error(`Failed to load run ${run.run_id}:`, err);
      }
    }

    // Sort by timestamp (oldest first for chart)
    runsData.sort((a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime());

    setRunsWithScores(runsData);
    setLoading(false);
  }, [runsIndex, selectedTimeframe]);

  // Load data when timeframe changes
  useEffect(() => {
    loadAllRunScores();
  }, [loadAllRunScores]);

  // Filter runs by selected agent type
  const filteredRuns = useMemo(() => {
    if (!selectedAgentType?.value || selectedAgentType.value === "all") {
      return runsWithScores;
    }
    return runsWithScores.filter((run) => run.agentType === selectedAgentType.value);
  }, [runsWithScores, selectedAgentType]);

  // Prepare chart data
  const chartData = useMemo(() => {
    return filteredRuns.map((run, idx) => ({
      index: idx + 1,
      timestamp: new Date(run.timestamp).toLocaleDateString(),
      fullTimestamp: run.timestamp,
      runId: run.run_id,
      agentType: getAgentTypeDisplayName(run.agentType),
      overall: Math.round(run.overallScore * 100),
      passRate: Math.round(run.passRate * 100),
      ...Object.fromEntries(
        Object.entries(run.evaluatorScores).map(([name, score]) => [
          name,
          Math.round(score * 100),
        ])
      ),
    }));
  }, [filteredRuns]);

  // Get evaluators that exist in the filtered data
  const filteredEvaluators = useMemo(() => {
    const evalSet = new Set<string>();
    filteredRuns.forEach((run) => {
      Object.keys(run.evaluatorScores).forEach((e) => evalSet.add(e));
    });
    return Array.from(evalSet).sort();
  }, [filteredRuns]);

  // Calculate statistics
  const stats = useMemo(() => {
    if (filteredRuns.length === 0) return null;

    const scores = filteredRuns.map((r) => r.overallScore);
    const avg = scores.reduce((a, b) => a + b, 0) / scores.length;
    const min = Math.min(...scores);
    const max = Math.max(...scores);
    const latest = scores[scores.length - 1];
    const first = scores[0];
    const trend = latest - first;

    return { avg, min, max, latest, first, trend };
  }, [filteredRuns]);

  return (
    <Layout title="Score Trends" description="Track evaluation scores over time">
      <SpaceBetween size="l">
        {/* Filters */}
        <Container header={<Header variant="h2">Filters</Header>}>
          <SpaceBetween size="m" direction="horizontal">
            <div style={{ minWidth: 180 }}>
              <Select
                selectedOption={selectedTimeframe}
                onChange={({ detail }) => setSelectedTimeframe(detail.selectedOption)}
                options={TIMEFRAME_OPTIONS}
              />
            </div>
            <div style={{ minWidth: 200 }}>
              <Select
                selectedOption={selectedAgentType}
                onChange={({ detail }) => setSelectedAgentType(detail.selectedOption)}
                options={agentTypeOptions}
                placeholder="Filter by agent type"
              />
            </div>
            <Toggle checked={showEvaluators} onChange={({ detail }) => setShowEvaluators(detail.checked)}>
              Show individual evaluators
            </Toggle>
            <Toggle checked={showThreshold} onChange={({ detail }) => setShowThreshold(detail.checked)}>
              Show 80% threshold line
            </Toggle>
          </SpaceBetween>
        </Container>

        {loading ? (
          <Container>
            <Box textAlign="center" padding="xl">
              <Spinner size="large" />
              <Box margin={{ top: "m" }}>Loading trend data...</Box>
            </Box>
          </Container>
        ) : filteredRuns.length === 0 ? (
          <Container>
            <Box textAlign="center" color="text-status-inactive" padding="xl">
              No runs found. Run some evaluations to see trends.
            </Box>
          </Container>
        ) : (
          <>
            {/* Statistics */}
            {stats && (
              <Container header={<Header variant="h2">Statistics</Header>}>
                <SpaceBetween size="m" direction="horizontal">
                  <Box>
                    <Box variant="awsui-key-label">Average Score</Box>
                    <Box fontSize="heading-xl">{Math.round(stats.avg * 100)}%</Box>
                  </Box>
                  <Box>
                    <Box variant="awsui-key-label">Min / Max</Box>
                    <Box fontSize="heading-xl">
                      {Math.round(stats.min * 100)}% / {Math.round(stats.max * 100)}%
                    </Box>
                  </Box>
                  <Box>
                    <Box variant="awsui-key-label">Latest Score</Box>
                    <Box fontSize="heading-xl">{Math.round(stats.latest * 100)}%</Box>
                  </Box>
                  <Box>
                    <Box variant="awsui-key-label">Overall Trend</Box>
                    <Box fontSize="heading-xl">
                      <span
                        style={{
                          color: stats.trend >= 0 ? "#16a34a" : "#dc2626",
                        }}
                      >
                        {stats.trend >= 0 ? "↑" : "↓"} {Math.abs(Math.round(stats.trend * 100))}%
                      </span>
                    </Box>
                  </Box>
                  <Box>
                    <Box variant="awsui-key-label">Total Runs</Box>
                    <Box fontSize="heading-xl">{filteredRuns.length}</Box>
                  </Box>
                </SpaceBetween>
              </Container>
            )}

            {/* Main Chart */}
            <Container
              header={
                <Header
                  variant="h2"
                  description={`Showing ${filteredRuns.length} evaluation runs`}
                >
                  Score Trends Over Time
                </Header>
              }
            >
              <Box padding="m">
                <ResponsiveContainer width="100%" height={450}>
                  <LineChart data={chartData}>
                    <CartesianGrid strokeDasharray="3 3" />
                    <XAxis
                      dataKey="timestamp"
                      tick={{ fontSize: 11 }}
                      angle={-45}
                      textAnchor="end"
                      height={70}
                    />
                    <YAxis
                      domain={[0, 100]}
                      tick={{ fontSize: 12 }}
                      tickFormatter={(value) => `${value}%`}
                    />
                    <Tooltip
                      formatter={(value) => [`${value ?? 0}%`, ""]}
                      labelFormatter={(_, payload) => {
                        if (payload && payload.length > 0) {
                          const data = payload[0].payload;
                          return `${data.timestamp} - ${data.agentType}`;
                        }
                        return "";
                      }}
                    />
                    <Legend />
                    {showThreshold && (
                      <ReferenceLine
                        y={80}
                        stroke="#16a34a"
                        strokeDasharray="5 5"
                        label={{ value: "80% threshold", position: "right", fontSize: 11 }}
                      />
                    )}
                    <Line
                      type="monotone"
                      dataKey="overall"
                      name="Overall Score"
                      stroke="#0972d3"
                      strokeWidth={3}
                      dot={{ r: 3 }}
                      activeDot={{ r: 6 }}
                    />
                    {showEvaluators &&
                      filteredEvaluators.map((evaluator, idx) => (
                        <Line
                          key={evaluator}
                          type="monotone"
                          dataKey={evaluator}
                          name={evaluator}
                          stroke={EVALUATOR_COLORS[idx % EVALUATOR_COLORS.length]}
                          strokeWidth={1.5}
                          dot={{ r: 2 }}
                          strokeDasharray={idx > 3 ? "5 5" : undefined}
                          connectNulls
                        />
                      ))}
                  </LineChart>
                </ResponsiveContainer>
              </Box>
            </Container>

            {/* Pass Rate Chart */}
            <Container
              header={
                <Header variant="h2" description="Percentage of tests passed">
                  Pass Rate Trend
                </Header>
              }
            >
              <Box padding="m">
                <ResponsiveContainer width="100%" height={250}>
                  <LineChart data={chartData}>
                    <CartesianGrid strokeDasharray="3 3" />
                    <XAxis dataKey="timestamp" tick={{ fontSize: 11 }} />
                    <YAxis
                      domain={[0, 100]}
                      tick={{ fontSize: 12 }}
                      tickFormatter={(value) => `${value}%`}
                    />
                    <Tooltip formatter={(value) => [`${value ?? 0}%`, "Pass Rate"]} />
                    <Line
                      type="monotone"
                      dataKey="passRate"
                      name="Pass Rate"
                      stroke="#16a34a"
                      strokeWidth={2}
                      dot={{ r: 3 }}
                    />
                  </LineChart>
                </ResponsiveContainer>
              </Box>
            </Container>
          </>
        )}
      </SpaceBetween>
    </Layout>
  );
}
