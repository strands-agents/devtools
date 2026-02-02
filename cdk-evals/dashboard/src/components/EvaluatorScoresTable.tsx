import { useState } from "react";
import Table from "@cloudscape-design/components/table";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Modal from "@cloudscape-design/components/modal";
import Link from "@cloudscape-design/components/link";
import Box from "@cloudscape-design/components/box";

export interface EvaluatorScore {
  evaluator: string;
  score: number;
  passed: boolean;
  reason: string;
}

interface EvaluatorScoresTableProps {
  items: EvaluatorScore[];
}

export default function EvaluatorScoresTable({ items }: EvaluatorScoresTableProps) {
  const [reasonModal, setReasonModal] = useState<{ evaluator: string; reason: string } | null>(null);

  return (
    <>
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
          {
            id: "reason",
            header: "Reason",
            cell: (item) => {
              if (!item.reason) return "-";
              const truncated = item.reason.length > 80 ? item.reason.slice(0, 80) + "..." : item.reason;
              return (
                <Link onFollow={() => setReasonModal({ evaluator: item.evaluator, reason: item.reason })}>
                  {truncated}
                </Link>
              );
            },
          },
        ]}
        items={items}
        trackBy="evaluator"
        variant="embedded"
      />
      <Modal
        visible={reasonModal !== null}
        onDismiss={() => setReasonModal(null)}
        header={reasonModal?.evaluator || "Reason"}
        size="large"
      >
        <Box>
          <pre
            style={{
              whiteSpace: "pre-wrap",
              fontFamily: "monospace",
              fontSize: "13px",
              lineHeight: "1.5",
            }}
          >
            {reasonModal?.reason}
          </pre>
        </Box>
      </Modal>
    </>
  );
}
