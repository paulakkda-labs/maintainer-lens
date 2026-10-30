export type Language = "zh" | "en";
export type Status =
  "pending" | "running" | "succeeded" | "failed" | "waiting_review";
export interface Failure {
  code: string;
  message: string;
  retryable: boolean;
  not_before?: string | null;
}
export interface Event {
  event_id: number;
  generation: number;
  kind: string;
  phase: string;
  search_round: number | null;
  error: Failure | null;
  created_at: string;
}
export interface Freshness {
  status: string;
  checked_at: string | null;
  observed_issue_state: string | null;
  reason_code?: string | null;
}
export interface Resolution {
  task_id: string;
  response_language: Language;
  status: Status;
  generation: number;
  decision: string;
  report_task_id: string | null;
  report_markdown: string | null;
  freshness: Freshness;
}
export interface Detail {
  task_id: string;
  repository: string;
  issue_number: number;
  response_language: Language;
  status: Status;
  generation: number;
  progress: Event | null;
  last_error: Failure | null;
  issue: { title: string; state: string; source_url: string } | null;
  result: {
    next_step: string;
    response_language: Language;
    search_round: number;
  } | null;
  report_markdown: string | null;
  previous_report_task_id: string | null;
  previous_report_markdown: string | null;
  freshness: Freshness;
}
export interface TaskSummary {
  task_id: string;
  status: Status;
  response_language: Language;
  generation: number;
  created_at: string;
  updated_at: string;
}
export interface Lookup {
  tasks: TaskSummary[];
  next_before: string | null;
}
export interface Budget {
  profile: string;
  source_max_files: number | null;
  source_max_bytes: number | null;
  source_max_chunks: number | null;
  history_max_items: number | null;
  preparation_timeout_seconds: number | null;
}
export interface Review {
  review_id: string;
  task_id: string;
  generation: number;
  context: {
    partial_result?: {
      preliminary_analysis: {
        summary: string;
        findings: { statement: string; evidence_ids: string[] }[];
        hypotheses: string[];
        unanswered_questions: string[];
      };
      analysis_evidence: {
        evidence_id: string;
        content: string;
        source_url: string;
      }[];
    } | null;
    reason: string;
    observed: number | null;
    output_tokens: number;
    knowledge_budget: Budget;
    base_execution: { response_language: Language };
  };
  decision: { action: string } | null;
  can_increase_budget: boolean;
  allowed_knowledge_profiles: string[];
  can_accept_partial: boolean;
  output_ceiling: number | null;
  budget_resumes_used: number;
  user_recoveries_used: number;
}
