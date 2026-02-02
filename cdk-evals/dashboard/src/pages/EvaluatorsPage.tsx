import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import ProgressBar from "@cloudscape-design/components/progress-bar";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import Table from "@cloudscape-design/components/table";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Layout from "../components/Layout";
import { useEvaluation, getScoreColor } from "../context/EvaluationContext";

export default function EvaluatorsPage() {
  const { evaluators } = useEvaluation();

  if (evaluators.length === 0) {
    return (
      <Layout title="Evaluators" description="Detailed evaluator performance analysis">
        <Container>
          <Box color="text-status-inactive" textAlign="center" padding="xl">
            No evaluator data loaded. Upload evaluation results or run evaluations first.
          </Box>
        </Container>
      </Layout>
    );
  }

  return (
    <Layout title="Evaluators" description="Detailed evaluator performance analysis">
      <SpaceBetween size="l">
        {evaluators.map((evaluator) => {
          const passCount = evaluator.report.test_passes.filter(Boolean).length;
          const totalCount = evaluator.report.test_passes.length;
          const passRate = (passCount / totalCount) * 100;

          return (
            <Container
              key={evaluator.name}
              header={
                <Header
                  variant="h2"
                  description={`${passCount}/${totalCount} tests passed`}
                >
                  {evaluator.name}
                </Header>
              }
            >
              <SpaceBetween size="l">
                <ColumnLayout columns={3}>
                  <Box>
                    <Box variant="awsui-key-label">Overall Score</Box>
                    <Box fontSize="display-l" fontWeight="bold">
                      {(evaluator.report.overall_score * 100).toFixed(0)}%
                    </Box>
                  </Box>
                  <Box>
                    <Box variant="awsui-key-label">Pass Rate</Box>
                    <Box fontSize="display-l" fontWeight="bold">
                      {passRate.toFixed(0)}%
                    </Box>
                  </Box>
                  <Box>
                    <Box variant="awsui-key-label">Tests</Box>
                    <Box fontSize="display-l" fontWeight="bold">
                      {passCount} / {totalCount}
                    </Box>
                  </Box>
                </ColumnLayout>

                <ProgressBar
                  value={evaluator.report.overall_score * 100}
                  status={getScoreColor(evaluator.report.overall_score) === "error" ? "error" : undefined}
                  description="Overall evaluator score"
                />

                <Table
                  header={<Header variant="h3">Test Case Results</Header>}
                  columnDefinitions={[
                    {
                      id: "case",
                      header: "Test Case",
                      cell: (item) => item.caseName,
                      width: 250,
                    },
                    {
                      id: "status",
                      header: "Status",
                      cell: (item) => (
                        <StatusIndicator type={item.passed ? "success" : "error"}>
                          {item.passed ? "Passed" : "Failed"}
                        </StatusIndicator>
                      ),
                      width: 100,
                    },
                    {
                      id: "score",
                      header: "Score",
                      cell: (item) => `${(item.score * 100).toFixed(0)}%`,
                      width: 80,
                    },
                    {
                      id: "reason",
                      header: "Reason",
                      cell: (item) => item.reason || "-",
                    },
                  ]}
                  items={evaluator.report.scores.map((score, idx) => ({
                    caseName: evaluator.report.cases[idx]?.name || `Case ${idx + 1}`,
                    score,
                    passed: evaluator.report.test_passes[idx],
                    reason: evaluator.report.reasons[idx],
                  }))}
                  variant="embedded"
                  stripedRows
                />
              </SpaceBetween>
            </Container>
          );
        })}
      </SpaceBetween>
    </Layout>
  );
}
