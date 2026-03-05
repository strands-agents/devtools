import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import Badge from "@cloudscape-design/components/badge";
import Layout from "../components/Layout";
import { useEvaluation, getAgentTypeDisplayName } from "../context/EvaluationContext";
import type { Insight } from "../types/evaluation";

function getSeverityIndicator(severity: Insight["severity"]) {
  switch (severity) {
    case "high":
      return <StatusIndicator type="error">High</StatusIndicator>;
    case "medium":
      return <StatusIndicator type="warning">Medium</StatusIndicator>;
    case "low":
      return <StatusIndicator type="info">Low</StatusIndicator>;
  }
}

function getCategoryLabel(category: Insight["category"]) {
  switch (category) {
    case "sop_improvement":
      return "SOP Improvement";
    case "tool_usage":
      return "Tool Usage";
    case "behavior_pattern":
      return "Behavior Pattern";
    case "efficiency":
      return "Efficiency";
  }
}

export default function InsightsPage() {
  const { insights, manifest } = useEvaluation();

  if (!insights) {
    return (
      <Layout title="Insights" description="AI-generated improvement recommendations">
        <Container>
          <Box color="text-status-inactive" textAlign="center" padding="xl">
            No insights available for this run. Insights are generated automatically after each evaluation.
          </Box>
        </Container>
      </Layout>
    );
  }

  const agentType = getAgentTypeDisplayName(insights.agent_type);

  return (
    <Layout title="Insights" description="AI-generated improvement recommendations">
      <SpaceBetween size="l">
        {/* Summary */}
        <Container
          header={
            <Header variant="h2" description={`${agentType} — ${manifest?.run_id || insights.run_id}`}>
              Analysis Summary
            </Header>
          }
        >
          <SpaceBetween size="m">
            <Box fontSize="heading-m">{insights.summary}</Box>
            <ColumnLayout columns={3}>
              <div>
                <Box variant="awsui-key-label">Weakest Evaluator</Box>
                <Box fontSize="heading-s">{insights.score_analysis.lowest_scoring_evaluator}</Box>
              </div>
              <div>
                <Box variant="awsui-key-label">Lowest Score</Box>
                <Box fontSize="heading-s">{(insights.score_analysis.lowest_score * 100).toFixed(0)}%</Box>
              </div>
              <div>
                <Box variant="awsui-key-label">Primary Weakness</Box>
                <Box fontSize="heading-s">{insights.score_analysis.primary_weakness}</Box>
              </div>
            </ColumnLayout>
          </SpaceBetween>
        </Container>

        {/* Insights List */}
        <Container
          header={
            <Header variant="h2" counter={`(${insights.insights.length})`}>
              Improvement Recommendations
            </Header>
          }
        >
          <SpaceBetween size="m">
            {insights.insights.map((insight, idx) => (
              <Container
                key={idx}
                header={
                  <Header
                    variant="h3"
                    actions={
                      <SpaceBetween direction="horizontal" size="xs">
                        <Badge color={insight.severity === "high" ? "red" : insight.severity === "medium" ? "blue" : "grey"}>
                          {getCategoryLabel(insight.category)}
                        </Badge>
                        {getSeverityIndicator(insight.severity)}
                      </SpaceBetween>
                    }
                  >
                    {insight.title}
                  </Header>
                }
              >
                <SpaceBetween size="s">
                  <Box>{insight.description}</Box>

                  {(insight.sop_section || insight.suggested_change) && (
                    <ExpandableSection headerText="Suggested Change" variant="footer">
                      <SpaceBetween size="s">
                        {insight.sop_section && (
                          <div>
                            <Box variant="awsui-key-label">SOP Section</Box>
                            <Box><code>{insight.sop_section}</code></Box>
                          </div>
                        )}
                        <div>
                          <Box variant="awsui-key-label">Recommended Change</Box>
                          <Box>
                            <pre style={{ whiteSpace: "pre-wrap", margin: 0, fontFamily: "inherit" }}>
                              {insight.suggested_change}
                            </pre>
                          </Box>
                        </div>
                      </SpaceBetween>
                    </ExpandableSection>
                  )}
                </SpaceBetween>
              </Container>
            ))}
          </SpaceBetween>
        </Container>
      </SpaceBetween>
    </Layout>
  );
}
