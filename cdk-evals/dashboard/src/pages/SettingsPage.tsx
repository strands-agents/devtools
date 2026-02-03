import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Box from "@cloudscape-design/components/box";
import Alert from "@cloudscape-design/components/alert";
import Layout from "../components/Layout";

export default function SettingsPage() {
  return (
    <Layout title="Settings" description="Configure dashboard preferences">
      <Container header={<Header variant="h2">Settings</Header>}>
        <Alert type="info">
          Settings configuration is coming soon. This page will allow you to customize
          dashboard behavior and preferences.
        </Alert>
        <Box padding={{ top: "l" }} color="text-status-inactive" textAlign="center">
          <Box variant="h3">Features planned:</Box>
          <ul style={{ textAlign: "left", maxWidth: "400px", margin: "16px auto" }}>
            <li>Pass/fail threshold configuration</li>
            <li>Default evaluator selection</li>
            <li>Data retention settings</li>
            <li>Export format preferences</li>
          </ul>
        </Box>
      </Container>
    </Layout>
  );
}
