import { useState } from "react";
import type { ReactNode } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import AppLayoutToolbar from "@cloudscape-design/components/app-layout-toolbar";
import BreadcrumbGroup from "@cloudscape-design/components/breadcrumb-group";
import Header from "@cloudscape-design/components/header";
import Button from "@cloudscape-design/components/button";
import SpaceBetween from "@cloudscape-design/components/space-between";
import SideNavigation from "@cloudscape-design/components/side-navigation";
import Alert from "@cloudscape-design/components/alert";
import Modal from "@cloudscape-design/components/modal";
import FileUpload from "@cloudscape-design/components/file-upload";
import Select from "@cloudscape-design/components/select";
import Box from "@cloudscape-design/components/box";
import { useEvaluation } from "../context/EvaluationContext";

interface LayoutProps {
  children: ReactNode;
  title: string;
  description?: string;
  breadcrumbs?: { href: string; text: string }[];
}

const ROUTE_TITLES: Record<string, string> = {
  "/": "Dashboard",
  "/results": "Test Results",
  "/evaluators": "Evaluators",
  "/trends": "Score Trends",
  "/cases": "Test Cases",
  "/agent-progress": "Agent Progress",
  "/settings": "Settings",
};

export default function Layout({ children, title, description, breadcrumbs }: LayoutProps) {
  const location = useLocation();
  const navigate = useNavigate();
  const {
    loading,
    error,
    setError,
    runOptions,
    selectedRun,
    handleRunChange,
    handleRefresh,
    handleUpload,
  } = useEvaluation();

  const [uploadModalVisible, setUploadModalVisible] = useState(false);
  const [uploadFiles, setUploadFiles] = useState<File[]>([]);

  const handleUploadSubmit = async () => {
    await handleUpload(uploadFiles);
    setUploadModalVisible(false);
    setUploadFiles([]);
  };

  const defaultBreadcrumbs = [
    { href: "/", text: "Home" },
    { href: location.pathname, text: ROUTE_TITLES[location.pathname] || title },
  ];

  const navItems = [
    { href: "/", text: "Dashboard", type: "link" as const },
    { href: "/results", text: "Test Results", type: "link" as const },
    { href: "/evaluators", text: "Evaluators", type: "link" as const },
    { type: "divider" as const },
    {
      text: "Analysis",
      type: "section" as const,
      defaultExpanded: true,
      items: [
        { href: "/agent-progress", text: "Agent Progress", type: "link" as const },
        { href: "/trends", text: "Score Trends", type: "link" as const },
        { href: "/cases", text: "Test Cases", type: "link" as const },
      ],
    },
    { type: "divider" as const },
    { href: "/settings", text: "Settings", type: "link" as const },
  ];

  return (
    <>
      <AppLayoutToolbar
        breadcrumbs={
          <BreadcrumbGroup
            items={breadcrumbs || defaultBreadcrumbs}
            onFollow={(e) => {
              e.preventDefault();
              navigate(e.detail.href);
            }}
          />
        }
        contentType="dashboard"
        navigation={
          <SideNavigation
            activeHref={location.pathname}
            header={{ href: "/", text: "Evaluation Dashboard" }}
            onFollow={(e) => {
              e.preventDefault();
              navigate(e.detail.href);
            }}
            items={navItems}
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
              description={description}
              variant="h1"
            >
              {title}
            </Header>

            {error && (
              <Alert type="info" dismissible onDismiss={() => setError(null)}>
                {error}
              </Alert>
            )}

            {children}
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
              <Button variant="primary" onClick={handleUploadSubmit} disabled={uploadFiles.length === 0}>
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
