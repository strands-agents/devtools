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
import { isSession } from "../types/evaluation";
import {
  useEvaluation,
  getScoreColor,
  getStatusType,
  formatTimestamp,
  extractToolNames,
} from "../context/EvaluationContext";
import type { EvaluatorData, Manifest } from "../context/EvaluationContext";

function ServiceOverviewContainer({
  evaluators,
  manifest,
}: {
  evaluators: EvaluatorData[];
  manifest: Manifest | null;
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
  const overallPassRate = totalTests > 0 ? (totalPasses / totalTests) * 100 : 0;
  const failedCount = totalTests - totalPasses;

  const avgScore =
    evaluators.length > 0
      ? evaluators.reduce((sum, e) => sum + e.report.overall_score, 0) / evaluators.length
      : 0;

  return (
    <Container
      header={
        <Header description="Overall evaluation health and key metrics" variant="h2">
          Service overview
        </Header>
      }
    >
      <SpaceBetween size="l">
        <KeyValuePairs
          columns={4}
          items={[
            {
              label: "Overall pass rate",
              value: (
                <Box fontSize="display-l" fontWeight="bold">
                  {overallPassRate.toFixed(0)}%
                </Box>
              ),
            },
            {
              label: "Test cases",
              value: (
                <Box fontSize="display-l" fontWeight="bold">
                  {totalCases}
                </Box>
              ),
            },
            {
              label: "Evaluators",
              value: (
                <Box fontSize="display-l" fontWeight="bold">
                  {totalEvaluators}
                </Box>
              ),
            },
            {
              label: "Failed evaluations",
              value: (
                <Box
                  fontSize="display-l"
                  fontWeight="bold"
                  color={failedCount > 0 ? "text-status-error" : "text-status-success"}
                >
                  {failedCount}
                </Box>
              ),
            },
          ]}
        />
        <ColumnLayout columns={2}>
          <Box>
            <Box variant="awsui-key-label">Evaluation status</Box>
            <StatusIndicator type={getStatusType(avgScore)}>
              {avgScore >= 0.8
                ? "Healthy - all evaluators above threshold"
                : avgScore >= 0.5
                ? "Partially healthy - some evaluators below threshold"
                : "Unhealthy - multiple evaluators failing"}
            </StatusIndicator>
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
}: {
  evaluators: EvaluatorData[];
  onCaseSelect: (caseIndex: number) => void;
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
  }

  const tableItems: TestResultRow[] = cases.map((c, idx) => ({
    name: c.name || `Case ${idx + 1}`,
    caseIndex: idx,
    results: evaluators.map((e) => ({
      evaluator: e.name,
      score: e.report.scores[idx],
      passed: e.report.test_passes[idx],
      reason: e.report.reasons[idx] || "",
    })),
  }));

  return (
    <Container header={<Header variant="h2">Test results by case</Header>}>
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
  const { evaluators, manifest, selectedCase, setSelectedCase } = useEvaluation();

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
        <ServiceOverviewContainer evaluators={evaluators} manifest={manifest} />
        <EvaluatorPerformanceContainer evaluators={evaluators} selectedCase={selectedCase} />
        <CaseDetailContainer
          evaluators={evaluators}
          selectedCase={selectedCase}
          onCaseChange={setSelectedCase}
        />
        <TestResultsContainer evaluators={evaluators} onCaseSelect={setSelectedCase} />
      </Grid>
    </Layout>
  );
}
