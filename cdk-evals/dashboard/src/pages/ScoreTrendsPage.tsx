import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Box from "@cloudscape-design/components/box";
import Alert from "@cloudscape-design/components/alert";
import Layout from "../components/Layout";

export default function ScoreTrendsPage() {
  return (
    <Layout title="Score Trends" description="Track evaluation scores over time">
      <Container header={<Header variant="h2">Score Trends</Header>}>
        <Alert type="info">
          Score trends visualization is coming soon. This feature will display historical
          evaluation performance across multiple runs, allowing you to track improvements
          and regressions over time.
        </Alert>
        <Box padding={{ top: "l" }} color="text-status-inactive" textAlign="center">
          <Box variant="h3">Features planned:</Box>
          <ul style={{ textAlign: "left", maxWidth: "400px", margin: "16px auto" }}>
            <li>Line charts showing score progression over runs</li>
            <li>Per-evaluator trend analysis</li>
            <li>Regression detection alerts</li>
            <li>Historical comparison views</li>
          </ul>
        </Box>
      </Container>
    </Layout>
  );
}
