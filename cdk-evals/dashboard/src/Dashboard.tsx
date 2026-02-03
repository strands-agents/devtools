import { useEffect, useState, useCallback } from "react";
import AppLayoutToolbar from "@cloudscape-design/components/app-layout-toolbar";
import BreadcrumbGroup from "@cloudscape-design/components/breadcrumb-group";
import Header from "@cloudscape-design/components/header";
import Button from "@cloudscape-design/components/button";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Grid from "@cloudscape-design/components/grid";
import Container from "@cloudscape-design/components/container";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import Box from "@cloudscape-design/components/box";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Table from "@cloudscape-design/components/table";
import SideNavigation from "@cloudscape-design/components/side-navigation";
import ProgressBar from "@cloudscape-design/components/progress-bar";
import Alert from "@cloudscape-design/components/alert";
import Modal from "@cloudscape-design/components/modal";
import FileUpload from "@cloudscape-design/components/file-upload";
import Tabs from "@cloudscape-design/components/tabs";
import Select from "@cloudscape-design/components/select";
import type { SelectProps } from "@cloudscape-design/components/select";
import Link from "@cloudscape-design/components/link";

// Types
interface TraceSpan {
  span_type: string;
  tool_call?: {
    name: string;
  };
}

interface Trace {
  spans: TraceSpan[];
}

interface ActualTrajectoryObject {
  traces: Trace[];
}

interface EvaluationCase {
  name: string;
  input: string;
  expected_output?: string;
  actual_output?: string;
  expected_trajectory?: string[];
  actual_trajectory?: string[] | ActualTrajectoryObject;
  metadata?: {
    issue_number?: number;
    issue_type?: string;
    repo?: string;
    labels?: string[];
    resolution?: string;
    good_response_criteria?: string[];
  };
}

interface EvaluationReport {
  overall_score: number;
  scores: number[];
  cases: EvaluationCase[];
  test_passes: boolean[];
  reasons: string[];
  detailed_results: unknown[][];
}

interface EvaluatorData {
  name: string;
  report: EvaluationReport;
}

interface Manifest {
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
function getScoreColor(score: number): "success" | "warning" | "error" {
  if (score >= 0.8) return "success";
  if (score >= 0.5) return "warning";
  return "error";
}

function getStatusType(score: number): "success" | "warning" | "error" {
  if (score >= 0.8) return "success";
  if (score >= 0.5) return "warning";
  return "error";
}

function formatTimestamp(isoString: string): string {
  try {
    return new Date(isoString).toLocaleString();
  } catch {
    return isoString;
  }
}

function extractToolNames(trajectory: string[] | ActualTrajectoryObject | undefined): string[] {
  if (!trajectory) return [];
  
  if (Array.isArray(trajectory)) {
    return trajectory;
  }
  
  // Extract tool names from trace spans
  const toolNames: string[] = [];
  if (trajectory.traces) {
    for (const trace of trajectory.traces) {
      if (trace.spans) {
        for (const span of trace.spans) {
          if (span.span_type === "execute_tool" && span.tool_call?.name) {
            toolNames.push(span.tool_call.name);
          }
        }
      }
    }
  }
  return toolNames;
}

// Components
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
                <Box fontSize="display-l" fontWeight="bold" color={failedCount > 0 ? "text-status-error" : "text-status-success"}>
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

function EvaluatorPerformanceContainer({ evaluators }: { evaluators: EvaluatorData[] }) {
  return (
    <Container header={<Header variant="h2">Evaluator performance</Header>}>
      <SpaceBetween size="m">
        {evaluators.map((evaluator) => {
          const passRate =
            evaluator.report.test_passes.filter(Boolean).length /
            evaluator.report.test_passes.length;
          return (
            <Box key={evaluator.name}>
              <SpaceBetween size="xxs">
                <Box fontWeight="bold">{evaluator.name}</Box>
                <ProgressBar
                  value={evaluator.report.overall_score * 100}
                  status={getScoreColor(evaluator.report.overall_score) === "error" ? "error" : undefined}
                  description={`${(passRate * 100).toFixed(0)}% pass rate (${
                    evaluator.report.test_passes.filter(Boolean).length
                  }/${evaluator.report.test_passes.length})`}
                  additionalInfo={`Score: ${(evaluator.report.overall_score * 100).toFixed(0)}%`}
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
  onCaseSelect 
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
  onCaseChange
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
                      { label: "Issue Number", value: currentCase.metadata.issue_number?.toString() || "-" },
                      { label: "Repository", value: currentCase.metadata.repo || "-" },
                      { label: "Resolution", value: currentCase.metadata.resolution || "-" },
                    ]}
                  />
                )}
                <Box variant="h4">Evaluator Scores</Box>
                <Table
                  columnDefinitions={[
                    { id: "evaluator", header: "Evaluator", cell: (item) => item.evaluator },
                    {
                      id: "score",
                      header: "Score",
                      cell: (item) => (
                        <StatusIndicator type={item.passed ? "success" : "error"}>
                          {(item.score * 100).toFixed(0)}%
                        </StatusIndicator>
                      ),
                    },
                    { id: "reason", header: "Reason", cell: (item) => item.reason || "-" },
                  ]}
                  items={evaluators.map((e) => ({
                    evaluator: e.name,
                    score: e.report.scores[selectedCase],
                    passed: e.report.test_passes[selectedCase],
                    reason: e.report.reasons[selectedCase],
                  }))}
                  trackBy="evaluator"
                  variant="embedded"
                />
              </SpaceBetween>
            ),
          },
          {
            id: "input",
            label: "Input",
            content: (
              <Box>
                <pre style={{ whiteSpace: "pre-wrap", fontFamily: "monospace", fontSize: "13px" }}>
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
                  <pre style={{ whiteSpace: "pre-wrap", fontFamily: "monospace", fontSize: "13px", background: "#f5f5f5", padding: "12px", borderRadius: "4px" }}>
                    {currentCase?.expected_output || "No expected output"}
                  </pre>
                </Box>
                <Box>
                  <Box variant="h4">Actual Output</Box>
                  <pre style={{ whiteSpace: "pre-wrap", fontFamily: "monospace", fontSize: "13px", background: "#f5f5f5", padding: "12px", borderRadius: "4px" }}>
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
                    {actualTools.length > 0 ? actualTools.map((tool, i) => (
                      <Box key={i} padding={{ vertical: "xxs" }}>
                        <code>{tool}</code>
                      </Box>
                    )) : <Box color="text-status-inactive">None recorded</Box>}
                  </Box>
                </ColumnLayout>
              );
            })(),
          },
        ]}
      />
    </Container>
  );
}

function EvaluationDashboard() {
  const [evaluators, setEvaluators] = useState<EvaluatorData[]>([]);
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [uploadModalVisible, setUploadModalVisible] = useState(false);
  const [uploadFiles, setUploadFiles] = useState<File[]>([]);
  const [selectedCase, setSelectedCase] = useState<number>(0);
  
  // Multi-run support
  const [runsIndex, setRunsIndex] = useState<RunsIndex | null>(null);
  const [selectedRun, setSelectedRun] = useState<SelectProps.Option | null>(null);

  // Load runs index
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

  // Load a specific run by ID
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

  // Load legacy format from public folder root
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

  // Initial load
  useEffect(() => {
    const init = async () => {
      const index = await loadRunsIndex();
      if (index && index.runs.length > 0) {
        // Load most recent run
        const latestRun = index.runs[0];
        setSelectedRun({ value: latestRun.run_id, label: formatTimestamp(latestRun.timestamp) });
        await loadRun(latestRun.run_id);
      } else {
        // Fall back to legacy format
        await loadLegacyFormat();
      }
    };
    init();
  }, [loadRunsIndex, loadRun, loadLegacyFormat]);

  // Handle run selection change
  const handleRunChange = async (option: SelectProps.Option) => {
    setSelectedRun(option);
    if (option.value) {
      await loadRun(option.value);
    }
  };

  // Refresh - reload current run or index
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

  // Handle file upload
  const handleUpload = async () => {
    if (uploadFiles.length === 0) return;

    const evaluatorData: EvaluatorData[] = [];

    for (const file of uploadFiles) {
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
        files: uploadFiles.map((f) => f.name),
      });
      setSelectedRun(null);
      setError(null);
      setSelectedCase(0);
    }

    setUploadModalVisible(false);
    setUploadFiles([]);
  };

  // Build run options for dropdown
  const runOptions: SelectProps.Options = runsIndex?.runs.map((run) => ({
    value: run.run_id,
    label: formatTimestamp(run.timestamp),
    description: `${run.total_cases} cases, ${run.evaluator_count} evaluators`,
  })) || [];

  return (
    <>
      <AppLayoutToolbar
        breadcrumbs={
          <BreadcrumbGroup
            items={[
              { href: "#", text: "Home" },
              { href: "#/", text: "Evaluation Dashboard" },
            ]}
          />
        }
        contentType="dashboard"
        navigation={
          <SideNavigation
            activeHref="#/"
            header={{ href: "#/", text: "Evaluation Dashboard" }}
            items={[
              { href: "#/", text: "Dashboard", type: "link" },
              { href: "#/results", text: "Test Results", type: "link" },
              { href: "#/evaluators", text: "Evaluators", type: "link" },
              { type: "divider" },
              {
                text: "Analysis",
                type: "section",
                defaultExpanded: true,
                items: [
                  { href: "#/trends", text: "Score Trends", type: "link" },
                  { href: "#/cases", text: "Test Cases", type: "link" },
                ],
              },
              { type: "divider" },
              { href: "#/settings", text: "Settings", type: "link" },
            ]}
          />
        }
        content={
          <SpaceBetween size="m">
            <Header
              actions={
                <SpaceBetween direction="horizontal" size="xs">
                  {runOptions.length > 0 && (
                    <Select
                      selectedOption={selectedRun}
                      onChange={({ detail }) => handleRunChange(detail.selectedOption)}
                      options={runOptions}
                      placeholder="Select evaluation run"
                      expandToViewport
                    />
                  )}
                  <Button iconName="upload" onClick={() => setUploadModalVisible(true)}>
                    Upload results
                  </Button>
                  <Button iconName="refresh" onClick={handleRefresh} loading={loading}>
                    Refresh
                  </Button>
                  <Button iconName="download" variant="primary">
                    Export report
                  </Button>
                </SpaceBetween>
              }
              description="Monitor and analyze AI agent performance across quality evaluators and test cases"
              variant="h1"
            >
              Evaluation Dashboard
            </Header>

            {error && (
              <Alert type="info" dismissible onDismiss={() => setError(null)}>
                {error}
              </Alert>
            )}

            <Grid
              gridDefinition={[
                { colspan: { default: 12 } },
                { colspan: { default: 12, l: 6 } },
                { colspan: { default: 12, l: 6 } },
                { colspan: { default: 12 } },
              ]}
            >
              <ServiceOverviewContainer evaluators={evaluators} manifest={manifest} />
              <EvaluatorPerformanceContainer evaluators={evaluators} />
              <CaseDetailContainer 
                evaluators={evaluators} 
                selectedCase={selectedCase}
                onCaseChange={setSelectedCase}
              />
              <TestResultsContainer 
                evaluators={evaluators} 
                onCaseSelect={setSelectedCase}
              />
            </Grid>
          </SpaceBetween>
        }
      />

      <Modal
        visible={uploadModalVisible}
        onDismiss={() => setUploadModalVisible(false)}
        header="Upload evaluation results"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setUploadModalVisible(false)}>
                Cancel
              </Button>
              <Button variant="primary" onClick={handleUpload} disabled={uploadFiles.length === 0}>
                Load files
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <Box>
            Upload JSON files exported from the evaluation runner. You can upload multiple evaluator
            result files at once.
          </Box>
          <FileUpload
            onChange={({ detail }) => setUploadFiles(detail.value)}
            value={uploadFiles}
            i18nStrings={{
              uploadButtonText: (e) => (e ? "Choose files" : "Choose file"),
              dropzoneText: (e) => (e ? "Drop files to upload" : "Drop file to upload"),
              removeFileAriaLabel: (e) => `Remove file ${e + 1}`,
              limitShowFewer: "Show fewer files",
              limitShowMore: "Show more files",
              errorIconAriaLabel: "Error",
            }}
            accept=".json"
            multiple
            showFileLastModified
            showFileSize
          />
        </SpaceBetween>
      </Modal>
    </>
  );
}

export default EvaluationDashboard;
