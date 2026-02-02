import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Button from "@cloudscape-design/components/button";
import Tabs from "@cloudscape-design/components/tabs";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import Layout from "../components/Layout";
import EvaluatorScoresTable from "../components/EvaluatorScoresTable";
import ConversationViewer from "../components/ConversationViewer";
import { useEvaluation, extractToolNames } from "../context/EvaluationContext";
import { isSession } from "../types/evaluation";

export default function TestCasesPage() {
  const { evaluators, selectedCase, setSelectedCase } = useEvaluation();

  if (evaluators.length === 0) {
    return (
      <Layout title="Test Cases" description="Browse and inspect individual test cases">
        <Container>
          <Box color="text-status-inactive" textAlign="center" padding="xl">
            No test case data loaded. Upload evaluation results or run evaluations first.
          </Box>
        </Container>
      </Layout>
    );
  }

  const cases = evaluators[0].report.cases;
  const currentCase = cases[selectedCase];
  const actualTools = extractToolNames(currentCase?.actual_trajectory);

  return (
    <Layout title="Test Cases" description="Browse and inspect individual test cases">
      <SpaceBetween size="l">
        <Container
          header={
            <Header
              variant="h2"
              actions={
                <SpaceBetween direction="horizontal" size="xs">
                  <Button
                    disabled={selectedCase === 0}
                    onClick={() => setSelectedCase(selectedCase - 1)}
                    iconName="angle-left"
                  >
                    Previous
                  </Button>
                  <Box padding={{ vertical: "xs" }}>
                    {selectedCase + 1} of {cases.length}
                  </Box>
                  <Button
                    disabled={selectedCase === cases.length - 1}
                    onClick={() => setSelectedCase(selectedCase + 1)}
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
                  <SpaceBetween size="l">
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
                      style={{
                        whiteSpace: "pre-wrap",
                        fontFamily: "monospace",
                        fontSize: "13px",
                        background: "#f5f5f5",
                        padding: "16px",
                        borderRadius: "4px",
                        maxHeight: "500px",
                        overflow: "auto",
                      }}
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
                          maxHeight: "400px",
                          overflow: "auto",
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
                          maxHeight: "400px",
                          overflow: "auto",
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
                content: (
                  <ColumnLayout columns={2}>
                    <Box>
                      <Box variant="h4">Expected Tools</Box>
                      {currentCase?.expected_trajectory && currentCase.expected_trajectory.length > 0 ? (
                        <SpaceBetween size="xs">
                          {currentCase.expected_trajectory.map((tool, i) => (
                            <Box key={i} padding={{ vertical: "xxs" }}>
                              <code
                                style={{
                                  background: "#f5f5f5",
                                  padding: "4px 8px",
                                  borderRadius: "4px",
                                }}
                              >
                                {tool}
                              </code>
                            </Box>
                          ))}
                        </SpaceBetween>
                      ) : (
                        <Box color="text-status-inactive">None specified</Box>
                      )}
                    </Box>
                    <Box>
                      <Box variant="h4">Actual Tools</Box>
                      {actualTools.length > 0 ? (
                        <SpaceBetween size="xs">
                          {actualTools.map((tool, i) => (
                            <Box key={i} padding={{ vertical: "xxs" }}>
                              <code
                                style={{
                                  background: "#f5f5f5",
                                  padding: "4px 8px",
                                  borderRadius: "4px",
                                }}
                              >
                                {tool}
                              </code>
                            </Box>
                          ))}
                        </SpaceBetween>
                      ) : (
                        <Box color="text-status-inactive">None recorded</Box>
                      )}
                    </Box>
                  </ColumnLayout>
                ),
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
      </SpaceBetween>
    </Layout>
  );
}
