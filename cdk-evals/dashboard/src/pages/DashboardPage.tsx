import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import Grid from "@cloudscape-design/components/grid";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import Box from "@cloudscape-design/components/box";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import SpaceBetween from "@cloudscape-design/components/space-between";
import ProgressBar from "@cloudscape-design/components/progress-bar";
import Table from "@cloudscape-design/components/table";
import Tabs from "@cloudscape-design/components/tabs";
import Button from "@cloudscape-design/components/button";
import Link from "@cloudscape-design/components/link";
import Layout from "../components/Layout";
import EvaluatorScoresTable from "../components/EvaluatorScoresTable";
import ConversationViewer from "../components/ConversationViewer";
import RunComparisonModal from "../components/RunComparisonModal";
import { isSession } from "../types/evaluation";
import type { EvaluationReport } from "../types/evaluation";
import {
  useEvaluation,
  getScoreColor,
  getStatusType,
  formatTimestamp,
  extractToolNames,
  inferAgentType,
} from "../context/EvaluationContext";
import type { EvaluatorData, Manifest } from "../context/EvaluationContext";

// Trend indicator component
function TrendIndicator({ current, previous }: { current: number; previous: number | null }) {
  if (previous === null) return null;

  const diff = current - previous;
  const pct = Math.round(diff * 100);

  if (pct === 0) return <Box color="text-status-inactive">—</Box>;

  return (
    <span
      style={{
        color: pct >= 0 ? "#16a34a" : "#dc2626",
        fontSize: "12px",
        fontWeight: 500,
      }}
    >
      {pct >= 0 ? "↑" : "↓"} {Math.abs(pct)}%
    </span>
  );
}

interface HistoricalData {
  passRates: number[];
  scores: number[];
  previousPassRate: number | null;
  previousScore: number | null;
}

function ServiceOverviewContainer({
  evaluators,
  manifest,
  onShowFailures,
  onCompareRuns,
  historicalData,
}: {
  evaluators: EvaluatorData[];
  manifest: Manifest | null;
  onShowFailures: () => void;
  onCompareRuns: () => void;
  historicalData: HistoricalData;
}) {
  const totalCases = evaluators.length > 0 ? evaluators[0].report.cases.length : 0;
  const totalEvaluators = evaluators.length;

  const totalPasses = evaluators.reduce(
    (sum, e) => sum + e.report.test_passes.filter(Boolean).length,
    0
  );
  const totalTests = evaluators.reduce(
    (sum, e) => sum + e.report.test_passes.length,
    0
  );
  const overallPassRate = totalTests > 0 ? totalPasses / totalTests : 0;
  const failedCount = totalTests - totalPasses;

  const avgScore =
    evaluators.length > 0
      ? evaluators.reduce((sum, e) => sum + e.report.overall_score, 0) / evaluators.length
      : 0;

  return (
    <Container
      header={
        <Header
          description="Overall evaluation health and key metrics"
          variant="h2"
          actions={
            <Button onClick={onCompareRuns} iconName="view-full">
              Compare Runs
            </Button>
          }
        >
          Service overview
        </Header>
      }
    >
      <SpaceBetween size="l">
        <ColumnLayout columns={4}>
          {/* Pass Rate Card */}
          <Box>
            <Box variant="awsui-key-label">Overall pass rate</Box>
            <SpaceBetween size="xs" direction="horizontal" alignItems="center">
              <Box fontSize="display-l" fontWeight="bold">
                {(overallPassRate * 100).toFixed(0)}%
              </Box>
              <TrendIndicator current={overallPassRate} previous={historicalData.previousPassRate} />
            </SpaceBetween>
          </Box>

          {/* Test Cases Card */}
          <Box>
            <Box variant="awsui-key-label">Test cases</Box>
            <Box fontSize="display-l" fontWeight="bold">
              {totalCases}
            </Box>
          </Box>

          {/* Evaluators Card */}
          <Box>
            <Box variant="awsui-key-label">Evaluators</Box>
            <Box fontSize="display-l" fontWeight="bold">
              {totalEvaluators}
            </Box>
          </Box>

          {/* Failed Evaluations Card - Clickable */}
          <Box>
            <Box variant="awsui-key-label">Failed evaluations</Box>
            {failedCount > 0 ? (
              <div
                onClick={onShowFailures}
                style={{
                  cursor: "pointer",
                  display: "inline-block",
                }}
                role="button"
                tabIndex={0}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    onShowFailures();
                  }
                }}
              >
                <Box fontSize="display-l" fontWeight="bold" color="text-status-error">
                  <span style={{ textDecoration: "underline" }}>{failedCount}</span>
                </Box>
              </div>
            ) : (
              <Box fontSize="display-l" fontWeight="bold" color="text-status-success">
                0
              </Box>
            )}
          </Box>
        </ColumnLayout>

        <ColumnLayout columns={2}>
          <Box>
            <Box variant="awsui-key-label">Average Score</Box>
            <SpaceBetween size="xs" direction="horizontal" alignItems="center">
              <StatusIndicator type={getStatusType(avgScore)}>
                {(avgScore * 100).toFixed(0)}%
              </StatusIndicator>
              <TrendIndicator current={avgScore} previous={historicalData.previousScore} />
            </SpaceBetween>
          </Box>
          <Box>
            <Box variant="awsui-key-label">Last evaluation run</Box>
            <Box>{manifest ? formatTimestamp(manifest.timestamp) : "No data loaded"}</Box>
          </Box>
        </ColumnLayout>
      </SpaceBetween>
    </Container>
  );
}

function EvaluatorPerformanceContainer({
  evaluators,
  selectedCase,
}: {
  evaluators: EvaluatorData[];
  selectedCase: number;
}) {
  const cases = evaluators.length > 0 ? evaluators[0].report.cases : [];
  const caseName = cases[selectedCase]?.name || `Case ${selectedCase + 1}`;

  return (
    <Container
      header={
        <Header variant="h2" description={`Scores for: ${caseName}`}>
          Evaluator performance
        </Header>
      }
    >
      <SpaceBetween size="m">
        {evaluators.map((evaluator) => {
          const caseScore = evaluator.report.scores[selectedCase] ?? 0;
          const casePassed = evaluator.report.test_passes[selectedCase] ?? false;
          return (
            <Box key={evaluator.name}>
              <SpaceBetween size="xxs">
                <Box fontWeight="bold">{evaluator.name}</Box>
                <ProgressBar
                  value={caseScore * 100}
                  status={getScoreColor(caseScore) === "error" ? "error" : undefined}
                  description={casePassed ? "Passed" : "Failed"}
                  additionalInfo={`Score: ${(caseScore * 100).toFixed(0)}%`}
                />
              </SpaceBetween>
            </Box>
          );
        })}
        {evaluators.length === 0 && (
          <Box color="text-status-inactive">No evaluator data loaded</Box>
        )}
      </SpaceBetween>
    </Container>
  );
}

function TestResultsContainer({
  evaluators,
  onCaseSelect,
  showOnlyFailed,
  onToggleFilter,
}: {
  evaluators: EvaluatorData[];
  onCaseSelect: (caseIndex: number) => void;
  showOnlyFailed: boolean;
  onToggleFilter: () => void;
}) {
  if (evaluators.length === 0) {
    return (
      <Container header={<Header variant="h2">Test results</Header>}>
        <Box color="text-status-inactive">No test results loaded</Box>
      </Container>
    );
  }

  const cases = evaluators[0].report.cases;

  interface TestResultRow {
    name: string;
    caseIndex: number;
    results: { evaluator: string; score: number; passed: boolean; reason: string }[];
    hasFailed: boolean;
  }

  const allItems: TestResultRow[] = cases.map((c, idx) => {
    const results = evaluators.map((e) => ({
      evaluator: e.name,
      score: e.report.scores[idx],
      passed: e.report.test_passes[idx],
      reason: e.report.reasons[idx] || "",
    }));
    return {
      name: c.name || `Case ${idx + 1}`,
      caseIndex: idx,
      results,
      hasFailed: results.some((r) => !r.passed),
    };
  });

  const tableItems = showOnlyFailed ? allItems.filter((item) => item.hasFailed) : allItems;
  const failedCount = allItems.filter((item) => item.hasFailed).length;

  return (
    <Container
      header={
        <Header
          variant="h2"
          actions={
            failedCount > 0 && (
              <Button
                variant={showOnlyFailed ? "primary" : "normal"}
                onClick={onToggleFilter}
                iconName={showOnlyFailed ? "filter" : undefined}
              >
                {showOnlyFailed ? `Showing ${failedCount} failed` : `Show only failed (${failedCount})`}
              </Button>
            )
          }
        >
          Test results by case
        </Header>
      }
    >
      <Table
        columnDefinitions={[
          {
            id: "name",
            header: "Test Case",
            cell: (item: TestResultRow) => (
              <Link onFollow={() => onCaseSelect(item.caseIndex)}>
                <Box fontWeight="bold">{item.name}</Box>
              </Link>
            ),
            width: 250,
          },
          ...evaluators.map((e) => ({
            id: e.name,
            header: e.name.replace("Evaluator", ""),
            cell: (item: TestResultRow) => {
              const result = item.results.find((r) => r.evaluator === e.name);
              if (!result) return "-";
              return (
                <StatusIndicator type={result.passed ? "success" : "error"}>
                  {(result.score * 100).toFixed(0)}%
                </StatusIndicator>
              );
            },
            width: 120,
          })),
        ]}
        items={tableItems}
        trackBy="name"
        variant="embedded"
        stripedRows
        empty={
          <Box textAlign="center" color="text-status-inactive" padding="l">
            {showOnlyFailed ? "No failed test cases!" : "No test cases"}
          </Box>
        }
      />
    </Container>
  );
}

function CaseDetailContainer({
  evaluators,
  selectedCase,
  onCaseChange,
}: {
  evaluators: EvaluatorData[];
  selectedCase: number;
  onCaseChange: (index: number) => void;
}) {
  if (evaluators.length === 0) {
    return (
      <Container header={<Header variant="h2">Case details</Header>}>
        <Box color="text-status-inactive">No case data loaded</Box>
      </Container>
    );
  }

  const cases = evaluators[0].report.cases;
  const currentCase = cases[selectedCase];

  return (
    <Container
      header={
        <Header
          variant="h2"
          description="Detailed view of individual test case"
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                disabled={selectedCase === 0}
                onClick={() => onCaseChange(selectedCase - 1)}
                iconName="angle-left"
              >
                Previous
              </Button>
              <Button
                disabled={selectedCase === cases.length - 1}
                onClick={() => onCaseChange(selectedCase + 1)}
                iconName="angle-right"
                iconAlign="right"
              >
                Next
              </Button>
            </SpaceBetween>
          }
        >
          {currentCase?.name || `Case ${selectedCase + 1}`}
        </Header>
      }
    >
      <Tabs
        tabs={[
          {
            id: "overview",
            label: "Overview",
            content: (
              <SpaceBetween size="m">
                {currentCase?.metadata && (
                  <KeyValuePairs
                    columns={4}
                    items={[
                      { label: "Issue Type", value: currentCase.metadata.issue_type || "-" },
                      {
                        label: "Issue Number",
                        value: currentCase.metadata.issue_number?.toString() || "-",
                      },
                      { label: "Repository", value: currentCase.metadata.repo || "-" },
                      { label: "Resolution", value: currentCase.metadata.resolution || "-" },
                    ]}
                  />
                )}
                <Box variant="h4">Evaluator Scores</Box>
                <EvaluatorScoresTable
                  items={evaluators.map((e) => ({
                    evaluator: e.name,
                    score: e.report.scores[selectedCase],
                    passed: e.report.test_passes[selectedCase],
                    reason: e.report.reasons[selectedCase] || "",
                  }))}
                />
              </SpaceBetween>
            ),
          },
          {
            id: "input",
            label: "Input",
            content: (
              <Box>
                <pre
                  style={{ whiteSpace: "pre-wrap", fontFamily: "monospace", fontSize: "13px" }}
                >
                  {currentCase?.input || "No input data"}
                </pre>
              </Box>
            ),
          },
          {
            id: "output",
            label: "Expected vs Actual",
            content: (
              <ColumnLayout columns={2}>
                <Box>
                  <Box variant="h4">Expected Output</Box>
                  <pre
                    style={{
                      whiteSpace: "pre-wrap",
                      fontFamily: "monospace",
                      fontSize: "13px",
                      background: "#f5f5f5",
                      padding: "12px",
                      borderRadius: "4px",
                    }}
                  >
                    {currentCase?.expected_output || "No expected output"}
                  </pre>
                </Box>
                <Box>
                  <Box variant="h4">Actual Output</Box>
                  <pre
                    style={{
                      whiteSpace: "pre-wrap",
                      fontFamily: "monospace",
                      fontSize: "13px",
                      background: "#f5f5f5",
                      padding: "12px",
                      borderRadius: "4px",
                    }}
                  >
                    {currentCase?.actual_output || "No actual output recorded"}
                  </pre>
                </Box>
              </ColumnLayout>
            ),
          },
          {
            id: "trajectory",
            label: "Tool Trajectory",
            content: (() => {
              const actualTools = extractToolNames(currentCase?.actual_trajectory);
              return (
                <ColumnLayout columns={2}>
                  <Box>
                    <Box variant="h4">Expected Tools</Box>
                    {currentCase?.expected_trajectory?.map((tool, i) => (
                      <Box key={i} padding={{ vertical: "xxs" }}>
                        <code>{tool}</code>
                      </Box>
                    )) || <Box color="text-status-inactive">None specified</Box>}
                  </Box>
                  <Box>
                    <Box variant="h4">Actual Tools</Box>
                    {actualTools.length > 0 ? (
                      actualTools.map((tool, i) => (
                        <Box key={i} padding={{ vertical: "xxs" }}>
                          <code>{tool}</code>
                        </Box>
                      ))
                    ) : (
                      <Box color="text-status-inactive">None recorded</Box>
                    )}
                  </Box>
                </ColumnLayout>
              );
            })(),
          },
          ...(isSession(currentCase?.actual_trajectory)
            ? [
                {
                  id: "conversation",
                  label: "Conversation",
                  content: (
                    <Box>
                      <ConversationViewer session={currentCase.actual_trajectory} />
                    </Box>
                  ),
                },
              ]
            : []),
        ]}
      />
    </Container>
  );
}

export default function DashboardPage() {
  const { evaluators, manifest, selectedCase, setSelectedCase, runsIndex, selectedRun } = useEvaluation();
  const [showOnlyFailed, setShowOnlyFailed] = useState(false);
  const [compareModalVisible, setCompareModalVisible] = useState(false);
  const testResultsRef = useRef<HTMLDivElement>(null);
  const [historicalData, setHistoricalData] = useState<HistoricalData>({
    passRates: [],
    scores: [],
    previousPassRate: null,
    previousScore: null,
  });

  // Determine current agent type from selected run
  const currentAgentType = useMemo(() => {
    if (!selectedRun?.value) return null;
    return inferAgentType(selectedRun.value);
  }, [selectedRun]);

  // Load historical data for sparklines - filtered by agent type
  const loadHistoricalData = useCallback(async () => {
    if (!runsIndex || runsIndex.runs.length < 2) return;

    // Filter runs by current agent type
    const filteredRuns = currentAgentType
      ? runsIndex.runs.filter((run) => inferAgentType(run.run_id, run.agent_type) === currentAgentType)
      : runsIndex.runs;

    if (filteredRuns.length < 2) {
      setHistoricalData({
        passRates: [],
        scores: [],
        previousPassRate: null,
        previousScore: null,
      });
      return;
    }

    const passRates: number[] = [];
    const scores: number[] = [];

    // Load last 10 runs of same agent type for sparklines
    const runsToLoad = filteredRuns.slice(0, 10).reverse();

    for (const run of runsToLoad) {
      try {
        const manifestRes = await fetch(`/runs/${run.run_id}/manifest.json`);
        if (!manifestRes.ok) continue;

        const runManifest: Manifest = await manifestRes.json();
        let totalScore = 0;
        let totalPasses = 0;
        let totalTests = 0;

        for (const file of runManifest.files) {
          const res = await fetch(`/runs/${run.run_id}/${file}`);
          if (!res.ok) continue;

          const report: EvaluationReport = await res.json();
          totalScore += report.overall_score;
          totalPasses += report.test_passes.filter(Boolean).length;
          totalTests += report.test_passes.length;
        }

        const avgScore = runManifest.files.length > 0 ? totalScore / runManifest.files.length : 0;
        const passRate = totalTests > 0 ? totalPasses / totalTests : 0;

        passRates.push(passRate);
        scores.push(avgScore);
      } catch {
        // Skip failed runs
      }
    }

    // Get previous run's data (second to last in array)
    const previousPassRate = passRates.length >= 2 ? passRates[passRates.length - 2] : null;
    const previousScore = scores.length >= 2 ? scores[scores.length - 2] : null;

    setHistoricalData({
      passRates,
      scores,
      previousPassRate,
      previousScore,
    });
  }, [runsIndex, currentAgentType]);

  useEffect(() => {
    loadHistoricalData();
  }, [loadHistoricalData]);

  const handleShowFailures = () => {
    setShowOnlyFailed(true);
    // Scroll to test results table after state update
    setTimeout(() => {
      testResultsRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    }, 100);
  };

  return (
    <Layout
      title="Evaluation Dashboard"
      description="Monitor and analyze AI agent performance across quality evaluators and test cases"
    >
      <Grid
        gridDefinition={[
          { colspan: { default: 12 } },
          { colspan: { default: 12, l: 6 } },
          { colspan: { default: 12, l: 6 } },
          { colspan: { default: 12 } },
        ]}
      >
        <ServiceOverviewContainer
          evaluators={evaluators}
          manifest={manifest}
          onShowFailures={handleShowFailures}
          onCompareRuns={() => setCompareModalVisible(true)}
          historicalData={historicalData}
        />
        <EvaluatorPerformanceContainer evaluators={evaluators} selectedCase={selectedCase} />
        <CaseDetailContainer
          evaluators={evaluators}
          selectedCase={selectedCase}
          onCaseChange={setSelectedCase}
        />
        <div ref={testResultsRef}>
          <TestResultsContainer
            evaluators={evaluators}
            onCaseSelect={setSelectedCase}
            showOnlyFailed={showOnlyFailed}
            onToggleFilter={() => setShowOnlyFailed(!showOnlyFailed)}
          />
        </div>
      </Grid>

      {/* Run Comparison Modal */}
      <RunComparisonModal
        visible={compareModalVisible}
        onDismiss={() => setCompareModalVisible(false)}
        runs={runsIndex?.runs || []}
      />
    </Layout>
  );
}
