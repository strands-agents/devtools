import { useNavigate } from "react-router-dom";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Box from "@cloudscape-design/components/box";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Table from "@cloudscape-design/components/table";
import Link from "@cloudscape-design/components/link";
import Layout from "../components/Layout";
import { useEvaluation } from "../context/EvaluationContext";

export default function TestResultsPage() {
  const { evaluators, setSelectedCase } = useEvaluation();
  const navigate = useNavigate();

  if (evaluators.length === 0) {
    return (
      <Layout title="Test Results" description="Detailed test results across all evaluators">
        <Container>
          <Box color="text-status-inactive" textAlign="center" padding="xl">
            No test results loaded. Upload evaluation results or run evaluations first.
          </Box>
        </Container>
      </Layout>
    );
  }

  const cases = evaluators[0].report.cases;

  interface TestResultRow {
    name: string;
    caseIndex: number;
    issueType: string;
    repo: string;
    results: { evaluator: string; score: number; passed: boolean }[];
    overallPassed: boolean;
  }

  const tableItems: TestResultRow[] = cases.map((c, idx) => {
    const results = evaluators.map((e) => ({
      evaluator: e.name,
      score: e.report.scores[idx],
      passed: e.report.test_passes[idx],
    }));
    return {
      name: c.name || `Case ${idx + 1}`,
      caseIndex: idx,
      issueType: c.metadata?.issue_type || "-",
      repo: c.metadata?.repo || "-",
      results,
      overallPassed: results.every((r) => r.passed),
    };
  });

  const handleCaseClick = (caseIndex: number) => {
    setSelectedCase(caseIndex);
    navigate("/cases");
  };

  return (
    <Layout title="Test Results" description="Detailed test results across all evaluators">
      <Container>
        <Table
          header={<Header variant="h2">All Test Results</Header>}
          columnDefinitions={[
            {
              id: "status",
              header: "Status",
              cell: (item: TestResultRow) => (
                <StatusIndicator type={item.overallPassed ? "success" : "error"}>
                  {item.overallPassed ? "Passed" : "Failed"}
                </StatusIndicator>
              ),
              width: 100,
            },
            {
              id: "name",
              header: "Test Case",
              cell: (item: TestResultRow) => (
                <Link onFollow={() => handleCaseClick(item.caseIndex)}>
                  {item.name}
                </Link>
              ),
              width: 300,
            },
            {
              id: "issueType",
              header: "Issue Type",
              cell: (item: TestResultRow) => item.issueType,
              width: 120,
            },
            {
              id: "repo",
              header: "Repository",
              cell: (item: TestResultRow) => item.repo,
              width: 180,
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
              width: 110,
            })),
          ]}
          items={tableItems}
          trackBy="name"
          stripedRows
          stickyHeader
          resizableColumns
        />
      </Container>
    </Layout>
  );
}
