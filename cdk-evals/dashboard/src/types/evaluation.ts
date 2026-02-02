export interface EvaluationOutput {
  score: number;
  test_pass: boolean;
  reason: string;
}

export interface CaseMetadata {
  issue_number?: number;
  issue_type?: "bug" | "feature" | "question";
  repo?: string;
  labels?: string[];
  resolution?: string;
  good_response_criteria?: string[];
  multi_turn?: boolean;
  task_description?: string;
  max_turns?: number;
  expected_turns?: number;
  expected_pr_count?: number;
  expected_major_features?: number;
  expected_bug_fixes?: number;
  key_prs?: string[];
}

// Trajectory types for multi-turn conversation support
export interface SpanInfo {
  trace_id: string;
  span_id: string;
  session_id: string;
  parent_span_id?: string;
  start_time: string;
  end_time: string;
}

export interface TextContent {
  content_type: "text";
  text: string;
}

export interface ToolUseContent {
  content_type: "tool_use";
  name: string;
  arguments: Record<string, unknown>;
  tool_call_id: string;
}

export interface ToolResultContent {
  content_type: "tool_result";
  content: string;
  error: string | null;
  tool_call_id: string;
}

export type MessageContent = TextContent | ToolUseContent | ToolResultContent;

export interface Message {
  role: "user" | "assistant";
  content: MessageContent[];
}

export interface ToolCall {
  name: string;
  arguments: Record<string, unknown>;
  tool_call_id: string;
}

export interface ToolResult {
  content: string;
  error: string | null;
  tool_call_id: string;
}

export interface AvailableTool {
  name: string;
  description: string | null;
  parameters: unknown | null;
}

export interface InferenceSpan {
  span_info: SpanInfo;
  metadata: Record<string, unknown>;
  span_type: "inference";
  messages: Message[];
}

export interface ExecuteToolSpan {
  span_info: SpanInfo;
  metadata: Record<string, unknown>;
  span_type: "execute_tool";
  tool_call: ToolCall;
  tool_result: ToolResult;
}

export interface InvokeAgentSpan {
  span_info: SpanInfo;
  metadata: Record<string, unknown>;
  span_type: "invoke_agent";
  user_prompt: string;
  agent_response: string;
  available_tools: AvailableTool[];
}

export type Span = InferenceSpan | ExecuteToolSpan | InvokeAgentSpan;

export interface Trace {
  spans: Span[];
  trace_id: string;
  session_id: string;
}

export interface Session {
  traces: Trace[];
  session_id: string;
}

export interface EvaluationCase {
  name: string;
  input: string;
  expected_output?: string;
  actual_output?: string;
  expected_trajectory?: string[];
  actual_trajectory?: Session | string[];
  expected_interactions?: unknown[];
  actual_interactions?: unknown[];
  metadata?: CaseMetadata;
}

export interface EvaluationReport {
  overall_score: number;
  scores: number[];
  cases: EvaluationCase[];
  test_passes: boolean[];
  reasons: string[];
  detailed_results: EvaluationOutput[][];
}

export interface EvaluatorResult {
  evaluator_name: string;
  report: EvaluationReport;
}

// Helper type guards
export function isSession(trajectory: Session | string[] | undefined): trajectory is Session {
  return trajectory !== undefined && 
         typeof trajectory === 'object' && 
         !Array.isArray(trajectory) && 
         'traces' in trajectory;
}

export function isInferenceSpan(span: Span): span is InferenceSpan {
  return span.span_type === 'inference';
}

export function isExecuteToolSpan(span: Span): span is ExecuteToolSpan {
  return span.span_type === 'execute_tool';
}

export function isInvokeAgentSpan(span: Span): span is InvokeAgentSpan {
  return span.span_type === 'invoke_agent';
}
