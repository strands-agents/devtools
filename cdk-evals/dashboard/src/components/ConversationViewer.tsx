import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import Badge from "@cloudscape-design/components/badge";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import type {
  Session,
  InvokeAgentSpan,
  ExecuteToolSpan,
} from "../types/evaluation";
import { isInvokeAgentSpan, isExecuteToolSpan } from "../types/evaluation";

interface ConversationViewerProps {
  session: Session;
}

// Calculate duration in milliseconds from timestamps
function calculateDuration(startTime: string, endTime: string): number {
  try {
    const start = new Date(startTime).getTime();
    const end = new Date(endTime).getTime();
    return end - start;
  } catch {
    return 0;
  }
}

// Format duration for display
function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  return `${(ms / 60000).toFixed(1)}m`;
}

// Estimate token count (rough approximation: ~4 chars per token)
function estimateTokens(text: string): number {
  return Math.ceil(text.length / 4);
}

function ToolCallView({ span, index }: { span: ExecuteToolSpan; index: number }) {
  const truncateContent = (content: string, maxLength = 500) => {
    if (content.length <= maxLength) return content;
    return content.slice(0, maxLength) + "...";
  };

  const hasError = !!span.tool_result.error;
  const duration = calculateDuration(span.span_info.start_time, span.span_info.end_time);
  const resultContent = span.tool_result.error || span.tool_result.content;

  return (
    <Box
      padding="s"
      margin={{ bottom: "xs" }}
    >
      <div
        style={{
          borderLeft: hasError ? "3px solid #dc2626" : "3px solid #16a34a",
          paddingLeft: "12px",
        }}
      >
        <SpaceBetween size="xs">
          {/* Tool header with status badge */}
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <SpaceBetween direction="horizontal" size="xs" alignItems="center">
              <Box fontWeight="bold">
                <code style={{ fontSize: "13px" }}>{span.tool_call.name}</code>
              </Box>
              <StatusIndicator type={hasError ? "error" : "success"}>
                {hasError ? "Failed" : "Success"}
              </StatusIndicator>
            </SpaceBetween>
            <SpaceBetween direction="horizontal" size="xs">
              {duration > 0 && (
                <Badge color="grey">{formatDuration(duration)}</Badge>
              )}
              <Badge color="blue">#{index + 1}</Badge>
            </SpaceBetween>
          </div>

          {/* Arguments section - collapsible */}
          <ExpandableSection
            headerText="Arguments"
            variant="footer"
            defaultExpanded={false}
          >
            <pre
              style={{
                background: "#f8f9fa",
                padding: "8px 12px",
                borderRadius: "4px",
                fontSize: "12px",
                overflow: "auto",
                maxHeight: "150px",
                margin: 0,
                border: "1px solid #e9ecef",
              }}
            >
              {JSON.stringify(span.tool_call.arguments, null, 2)}
            </pre>
          </ExpandableSection>

          {/* Result section - always visible but collapsed if long */}
          <Box>
            <Box variant="small" color="text-body-secondary" margin={{ bottom: "xxs" }}>
              Result {resultContent && `(~${estimateTokens(resultContent)} tokens)`}
            </Box>
            <pre
              style={{
                background: hasError ? "#fef2f2" : "#f0fdf4",
                padding: "8px 12px",
                borderRadius: "4px",
                fontSize: "12px",
                overflow: "auto",
                maxHeight: "150px",
                margin: 0,
                border: hasError ? "1px solid #fecaca" : "1px solid #bbf7d0",
              }}
            >
              {truncateContent(resultContent)}
            </pre>
          </Box>
        </SpaceBetween>
      </div>
    </Box>
  );
}

function MessageBubble({
  role,
  content,
  isSimulated,
  tokenCount,
}: {
  role: "user" | "assistant";
  content: string;
  isSimulated?: boolean;
  tokenCount?: number;
}) {
  const isUser = role === "user";
  const tokens = tokenCount ?? estimateTokens(content);

  return (
    <Box padding="s" margin={{ bottom: "xs" }}>
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: isUser ? "flex-start" : "flex-end",
        }}
      >
        {/* Header with role and metadata */}
        <Box margin={{ bottom: "xxs" }}>
          <SpaceBetween direction="horizontal" size="xs" alignItems="center">
            <span style={{ fontSize: "16px" }}>{isUser ? "👤" : "🤖"}</span>
            <Box variant="small" fontWeight="bold">
              {isUser ? (isSimulated ? "User (Simulated)" : "User") : "Agent"}
            </Box>
            <Badge color="grey">~{tokens} tokens</Badge>
          </SpaceBetween>
        </Box>

        {/* Message content */}
        <div
          style={{
            background: isUser ? "#eff6ff" : "#f9fafb",
            padding: "12px 16px",
            borderRadius: isUser ? "4px 12px 12px 12px" : "12px 4px 12px 12px",
            maxWidth: "95%",
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
            fontSize: "14px",
            lineHeight: "1.6",
            border: isSimulated ? "2px dashed #93c5fd" : "1px solid #e5e7eb",
            boxShadow: "0 1px 2px rgba(0,0,0,0.05)",
          }}
        >
          {content}
        </div>
      </div>
    </Box>
  );
}

function TurnSeparator({ turnNumber, duration, toolCount }: { turnNumber: number; duration?: number; toolCount: number }) {
  return (
    <Box margin={{ vertical: "m" }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: "12px",
        }}
      >
        <div style={{ flex: 1, height: "1px", background: "#d1d5db" }} />
        <SpaceBetween direction="horizontal" size="xxs">
          <Badge color="blue">Turn {turnNumber}</Badge>
          {toolCount > 0 && (
            <Badge color="grey">{toolCount} tool{toolCount > 1 ? "s" : ""}</Badge>
          )}
          {duration !== undefined && duration > 0 && (
            <Badge color="green">{formatDuration(duration)}</Badge>
          )}
        </SpaceBetween>
        <div style={{ flex: 1, height: "1px", background: "#d1d5db" }} />
      </div>
    </Box>
  );
}

function TurnSummary({ turn }: { turn: { toolCalls: ExecuteToolSpan[] } }) {
  const successCount = turn.toolCalls.filter((t) => !t.tool_result.error).length;
  const errorCount = turn.toolCalls.filter((t) => !!t.tool_result.error).length;
  const totalDuration = turn.toolCalls.reduce(
    (sum, t) => sum + calculateDuration(t.span_info.start_time, t.span_info.end_time),
    0
  );

  if (turn.toolCalls.length === 0) return null;

  return (
    <Box margin={{ bottom: "xs" }}>
      <ColumnLayout columns={4} variant="text-grid">
        <div>
          <Box variant="awsui-key-label">Tool Calls</Box>
          <Box>{turn.toolCalls.length}</Box>
        </div>
        <div>
          <Box variant="awsui-key-label">Successful</Box>
          <Box>
            <StatusIndicator type="success">{successCount}</StatusIndicator>
          </Box>
        </div>
        <div>
          <Box variant="awsui-key-label">Failed</Box>
          <Box>
            {errorCount > 0 ? (
              <StatusIndicator type="error">{errorCount}</StatusIndicator>
            ) : (
              <StatusIndicator type="success">0</StatusIndicator>
            )}
          </Box>
        </div>
        <div>
          <Box variant="awsui-key-label">Total Time</Box>
          <Box>{formatDuration(totalDuration)}</Box>
        </div>
      </ColumnLayout>
    </Box>
  );
}

export default function ConversationViewer({ session }: ConversationViewerProps) {
  // Collect all invoke_agent spans as "turns" and their associated tool calls
  const turns: Array<{
    turnNumber: number;
    invokeSpan: InvokeAgentSpan;
    toolCalls: ExecuteToolSpan[];
    duration: number;
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

      const duration = calculateDuration(
        invokeSpan.span_info.start_time,
        invokeSpan.span_info.end_time
      );

      turns.push({
        turnNumber: turns.length + 1,
        invokeSpan,
        toolCalls: relatedToolCalls,
        duration,
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

  // Calculate session totals
  const totalTools = turns.reduce((sum, t) => sum + t.toolCalls.length, 0);
  const totalErrors = turns.reduce(
    (sum, t) => sum + t.toolCalls.filter((tc) => !!tc.tool_result.error).length,
    0
  );
  const totalDuration = turns.reduce((sum, t) => sum + t.duration, 0);

  // Calculate total tokens across entire conversation
  const totalTokens = turns.reduce((sum, turn) => {
    // User prompt tokens
    const userTokens = estimateTokens(turn.invokeSpan.user_prompt);
    // Agent response tokens
    const agentTokens = estimateTokens(turn.invokeSpan.agent_response);
    // Tool call tokens (arguments + results)
    const toolTokens = turn.toolCalls.reduce((toolSum, tc) => {
      const argsTokens = estimateTokens(JSON.stringify(tc.tool_call.arguments));
      const resultTokens = estimateTokens(tc.tool_result.error || tc.tool_result.content);
      return toolSum + argsTokens + resultTokens;
    }, 0);
    return sum + userTokens + agentTokens + toolTokens;
  }, 0);

  // Format token count for display
  const formatTokenCount = (count: number): string => {
    if (count >= 1000) {
      return `${(count / 1000).toFixed(1)}k`;
    }
    return count.toString();
  };

  return (
    <SpaceBetween size="s">
      {/* Session Summary */}
      <Box padding="s" margin={{ bottom: "m" }}>
        <div
          style={{
            background: "#f8fafc",
            padding: "12px 16px",
            borderRadius: "8px",
            border: "1px solid #e2e8f0",
          }}
        >
          <ColumnLayout columns={5} variant="text-grid">
            <div>
              <Box variant="awsui-key-label">Total Turns</Box>
              <Box fontSize="heading-m">{turns.length}</Box>
            </div>
            <div>
              <Box variant="awsui-key-label">Total Tool Calls</Box>
              <Box fontSize="heading-m">{totalTools}</Box>
            </div>
            <div>
              <Box variant="awsui-key-label">Tool Errors</Box>
              <Box fontSize="heading-m">
                {totalErrors > 0 ? (
                  <StatusIndicator type="error">{totalErrors}</StatusIndicator>
                ) : (
                  <StatusIndicator type="success">0</StatusIndicator>
                )}
              </Box>
            </div>
            <div>
              <Box variant="awsui-key-label">Total Duration</Box>
              <Box fontSize="heading-m">{formatDuration(totalDuration)}</Box>
            </div>
            <div>
              <Box variant="awsui-key-label">Est. Context Size</Box>
              <Box fontSize="heading-m">~{formatTokenCount(totalTokens)} tokens</Box>
            </div>
          </ColumnLayout>
        </div>
      </Box>

      {/* Conversation turns */}
      {turns.map((turn, idx) => (
        <div key={idx}>
          <TurnSeparator
            turnNumber={turn.turnNumber}
            duration={turn.duration}
            toolCount={turn.toolCalls.length}
          />

          {/* User message */}
          <MessageBubble
            role="user"
            content={turn.invokeSpan.user_prompt}
            isSimulated={turn.turnNumber > 1}
          />

          {/* Tool calls section */}
          {turn.toolCalls.length > 0 && (
            <Box margin={{ left: "l", right: "l", bottom: "s" }}>
              <ExpandableSection
                headerText={
                  <SpaceBetween direction="horizontal" size="xs">
                    <span>🔧 Tool Calls</span>
                    <Badge color={turn.toolCalls.some((t) => t.tool_result.error) ? "red" : "green"}>
                      {turn.toolCalls.filter((t) => !t.tool_result.error).length}/{turn.toolCalls.length} success
                    </Badge>
                  </SpaceBetween>
                }
                variant="container"
                defaultExpanded={turn.toolCalls.some((t) => !!t.tool_result.error)}
              >
                <SpaceBetween size="s">
                  <TurnSummary turn={turn} />
                  {turn.toolCalls.map((toolSpan, toolIdx) => (
                    <ToolCallView key={toolIdx} span={toolSpan} index={toolIdx} />
                  ))}
                </SpaceBetween>
              </ExpandableSection>
            </Box>
          )}

          {/* Agent response */}
          <MessageBubble role="assistant" content={turn.invokeSpan.agent_response} />
        </div>
      ))}
    </SpaceBetween>
  );
}
