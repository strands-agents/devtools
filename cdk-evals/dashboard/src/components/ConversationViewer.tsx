import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import Badge from "@cloudscape-design/components/badge";
import type {
  Session,
  InvokeAgentSpan,
  ExecuteToolSpan,
} from "../types/evaluation";
import { isInvokeAgentSpan, isExecuteToolSpan } from "../types/evaluation";

interface ConversationViewerProps {
  session: Session;
}

function ToolCallView({ span }: { span: ExecuteToolSpan }) {
  const truncateContent = (content: string, maxLength = 500) => {
    if (content.length <= maxLength) return content;
    return content.slice(0, maxLength) + "...";
  };

  return (
    <Box margin={{ left: "l" }} padding="s">
      <ExpandableSection
        headerText={
          <SpaceBetween direction="horizontal" size="xs">
            <span>🔧</span>
            <code style={{ fontWeight: "bold" }}>{span.tool_call.name}</code>
          </SpaceBetween>
        }
        variant="footer"
      >
        <SpaceBetween size="s">
          <Box>
            <Box variant="h5" margin={{ bottom: "xxs" }}>Arguments</Box>
            <pre
              style={{
                background: "#f0f4f8",
                padding: "8px 12px",
                borderRadius: "4px",
                fontSize: "12px",
                overflow: "auto",
                maxHeight: "200px",
                margin: 0,
              }}
            >
              {JSON.stringify(span.tool_call.arguments, null, 2)}
            </pre>
          </Box>
          <Box>
            <Box variant="h5" margin={{ bottom: "xxs" }}>
              Result
              {span.tool_result.error && (
                <Badge color="red">Error</Badge>
              )}
            </Box>
            <pre
              style={{
                background: span.tool_result.error ? "#fef0f0" : "#f0f8f0",
                padding: "8px 12px",
                borderRadius: "4px",
                fontSize: "12px",
                overflow: "auto",
                maxHeight: "200px",
                margin: 0,
              }}
            >
              {truncateContent(span.tool_result.error || span.tool_result.content)}
            </pre>
          </Box>
        </SpaceBetween>
      </ExpandableSection>
    </Box>
  );
}

function MessageBubble({
  role,
  content,
  isSimulated,
}: {
  role: "user" | "assistant";
  content: string;
  isSimulated?: boolean;
}) {
  const isUser = role === "user";

  return (
    <Box
      padding="m"
      margin={{ bottom: "s" }}
    >
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: isUser ? "flex-start" : "flex-end",
        }}
      >
        <Box margin={{ bottom: "xxs" }}>
          <SpaceBetween direction="horizontal" size="xs">
            <span style={{ fontSize: "14px" }}>{isUser ? "👤" : "🤖"}</span>
            <Box variant="small" color="text-body-secondary">
              {isUser ? (isSimulated ? "User (Simulated)" : "User") : "Agent"}
            </Box>
          </SpaceBetween>
        </Box>
        <div
          style={{
            background: isUser ? "#e8f0fe" : "#f5f5f5",
            padding: "12px 16px",
            borderRadius: isUser ? "4px 16px 16px 16px" : "16px 4px 16px 16px",
            maxWidth: "90%",
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
            fontSize: "14px",
            lineHeight: "1.5",
            border: isSimulated ? "2px dashed #b4d7ff" : "none",
          }}
        >
          {content}
        </div>
      </div>
    </Box>
  );
}

function TurnSeparator({ turnNumber }: { turnNumber: number }) {
  return (
    <Box textAlign="center" margin={{ vertical: "m" }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: "12px",
        }}
      >
        <div style={{ flex: 1, height: "1px", background: "#d1d5db" }} />
        <Badge color="blue">Turn {turnNumber}</Badge>
        <div style={{ flex: 1, height: "1px", background: "#d1d5db" }} />
      </div>
    </Box>
  );
}

export default function ConversationViewer({ session }: ConversationViewerProps) {
  // Collect all invoke_agent spans as "turns" and their associated tool calls
  const turns: Array<{
    turnNumber: number;
    invokeSpan: InvokeAgentSpan;
    toolCalls: ExecuteToolSpan[];
  }> = [];

  session.traces.forEach((trace) => {
    const invokeSpans = trace.spans.filter(isInvokeAgentSpan);
    const toolSpans = trace.spans.filter(isExecuteToolSpan);

    invokeSpans.forEach((invokeSpan) => {
      // Find tool calls that belong to this invoke_agent span
      const relatedToolCalls = toolSpans.filter(
        (toolSpan) =>
          toolSpan.span_info.parent_span_id === invokeSpan.span_info.span_id ||
          (toolSpan.span_info.start_time >= invokeSpan.span_info.start_time &&
            toolSpan.span_info.end_time <= invokeSpan.span_info.end_time)
      );

      turns.push({
        turnNumber: turns.length + 1,
        invokeSpan,
        toolCalls: relatedToolCalls,
      });
    });
  });

  if (turns.length === 0) {
    return (
      <Box color="text-status-inactive" textAlign="center" padding="l">
        No conversation turns found in this trajectory.
      </Box>
    );
  }

  return (
    <SpaceBetween size="xs">
      {turns.map((turn, idx) => (
        <div key={idx}>
          <TurnSeparator turnNumber={turn.turnNumber} />

          {/* User message */}
          <MessageBubble
            role="user"
            content={turn.invokeSpan.user_prompt}
            isSimulated={turn.turnNumber > 1}
          />

          {/* Tool calls (collapsed by default) */}
          {turn.toolCalls.length > 0 && (
            <ExpandableSection
              headerText={`${turn.toolCalls.length} tool call${turn.toolCalls.length > 1 ? "s" : ""}`}
              variant="container"
              defaultExpanded={false}
            >
              <SpaceBetween size="s">
                {turn.toolCalls.map((toolSpan, toolIdx) => (
                  <ToolCallView key={toolIdx} span={toolSpan} />
                ))}
              </SpaceBetween>
            </ExpandableSection>
          )}

          {/* Agent response */}
          <MessageBubble role="assistant" content={turn.invokeSpan.agent_response} />
        </div>
      ))}
    </SpaceBetween>
  );
}
