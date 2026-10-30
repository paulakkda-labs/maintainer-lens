import { useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ApiError, request } from "./api";
import { date, failure, phase, profile, text } from "./i18n";
import type { Detail, Event, Language, Review, Status } from "./types";

export function Icon({
  name,
}: {
  name: "arrow" | "copy" | "download" | "search";
}) {
  const paths = {
    arrow: "M5 12h14m-6-6 6 6-6 6",
    copy: "M9 9h11v11H9zM15 5V3H3v12h2",
    download: "M12 3v12m-5-5 5 5 5-5M4 17v4h16v-4",
    search: "m16 16 5 5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0",
  };
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={paths[name]} />
    </svg>
  );
}

export function Copy({ value, lang }: { value: string; lang: Language }) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } catch {
      setCopied(false);
    }
  }
  return (
    <button
      className="icon-button"
      type="button"
      onClick={() => void copy()}
      aria-label={text(lang, "copy")}
      title={text(lang, copied ? "copied" : "copy")}
    >
      <Icon name="copy" />
      <span className="sr-only">{text(lang, copied ? "copied" : "copy")}</span>
    </button>
  );
}

export function StatusBadge({
  status,
  lang,
  gaps = false,
}: {
  status: Status;
  lang: Language;
  gaps?: boolean;
}) {
  return (
    <span className={`status status-${status}`}>
      <span className="status-dot" />
      {text(lang, gaps ? "gaps" : status)}
    </span>
  );
}

export function TaskIdentity({
  task,
  lang,
  onRecords,
}: {
  task: Detail;
  lang: Language;
  onRecords?: () => void;
}) {
  return (
    <section className="task-identity" aria-label={text(lang, "detail")}>
      <div className="identity-top">
        <span className="repo">
          {task.repository} <span className="muted">#{task.issue_number}</span>
        </span>
        <StatusBadge
          status={task.status}
          lang={lang}
          gaps={task.result?.next_step === "needs_more_evidence"}
        />
      </div>
      {task.issue && <h2 className="issue-title">{task.issue.title}</h2>}
      <div className="identity-meta">
        <span>
          {text(lang, "task")} <code data-testid="task-id">{task.task_id}</code>
          <Copy value={task.task_id} lang={lang} />
        </span>
        <span>
          {text(lang, "generation")} {task.generation}
        </span>
        <span>
          {text(lang, "reportLanguage")}{" "}
          {task.response_language === "zh" ? "中文" : "English"}
        </span>
        {onRecords && (
          <button className="text-button" onClick={onRecords}>
            {text(lang, "viewRecords")} ↗
          </button>
        )}
      </div>
    </section>
  );
}

export function ErrorNotice({
  status,
  lang,
  onRefresh,
}: {
  status: number;
  lang: Language;
  onRefresh: () => void;
}) {
  return (
    <div className="notice notice-error" role="alert">
      <p>{text(lang, status === 404 ? "notFound" : "readError")}</p>
      <button className="text-button" onClick={onRefresh}>
        {text(lang, "refresh")}
      </button>
    </div>
  );
}

export function FailureNotice({
  task,
  lang,
}: {
  task: Detail;
  lang: Language;
}) {
  if (!task.last_error) return null;
  return (
    <div className="notice notice-error">
      <strong>{text(lang, "lastError")}</strong>
      <p>{failure(lang, task.last_error.code)}</p>
      <div className="error-meta">
        <code>{task.last_error.code}</code>
        <span>
          {text(lang, task.last_error.retryable ? "retryable" : "permanent")}
        </span>
      </div>
      <small>{text(lang, "errorNote")}</small>
    </div>
  );
}

export function Timeline({
  events,
  lang,
  task,
}: {
  events: Event[];
  lang: Language;
  task: Detail | null;
}) {
  const active = task?.status === "pending" || task?.status === "running";
  return (
    <section className="timeline-section" aria-label={text(lang, "progress")}>
      <div className="section-label">
        <h2>{text(lang, "progress")}</h2>
        {active && (
          <span className="live-dot" aria-label={text(lang, "running")} />
        )}
      </div>
      <p className="subtle timeline-note">{text(lang, "stageNote")}</p>
      {!events.length ? (
        <p className="subtle">{text(lang, "noEvents")}</p>
      ) : (
        <ol className="timeline">
          {events.map((event, index) => (
            <li
              key={event.event_id}
              className={`${index === events.length - 1 ? "latest" : ""} ${event.error ? "event-error" : ""}`}
            >
              <span className="timeline-dot" />
              <div>
                <div className="event-label">
                  {phase(lang, event.phase, event.kind)}
                </div>
                <div className="event-meta">
                  <time dateTime={event.created_at}>
                    {date(lang, event.created_at)}
                  </time>
                  <span>
                    {text(lang, "generation")} {event.generation}
                    {event.search_round
                      ? ` · ${text(lang, "round")} ${event.search_round}`
                      : ""}
                  </span>
                </div>
                {event.error && (
                  <p className="event-error-message">
                    {failure(lang, event.error.code)}{" "}
                    <code>{event.error.code}</code>
                  </p>
                )}
              </div>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

export function Report({
  markdown,
  lang,
  filename,
  historical = false,
  ownerId,
  freshness,
}: {
  markdown: string;
  lang: Language;
  filename: string;
  historical?: boolean;
  ownerId: string;
  freshness?: string;
}) {
  const [raw, setRaw] = useState(false);
  function download() {
    const href = URL.createObjectURL(
      new Blob([markdown], { type: "text/markdown;charset=utf-8" }),
    );
    const anchor = document.createElement("a");
    anchor.href = href;
    anchor.download = filename;
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(href), 1000);
  }
  const freshnessKeys = [
    "current",
    "stale",
    "unknown",
    "checking",
    "not_checked",
  ] as const;
  const currentFreshness = freshnessKeys.find((key) => key === freshness);
  return (
    <section className="report-section" aria-label={text(lang, "report")}>
      <div className="report-heading">
        <div>
          <p className="eyebrow">
            {text(lang, historical ? "historical" : "report")}
          </p>
          <p className="subtle">
            {text(lang, historical ? "historicalNote" : "reportNote")}
          </p>
        </div>
        {currentFreshness && (
          <span className="freshness">{text(lang, currentFreshness)}</span>
        )}
      </div>
      {historical && (
        <div className="historical-owner">
          <code>{ownerId}</code>
          <Copy value={ownerId} lang={lang} />
        </div>
      )}
      <div className="report-toolbar">
        <div className="segmented" aria-label={text(lang, "report")}>
          <button
            className={!raw ? "selected" : ""}
            onClick={() => setRaw(false)}
            aria-pressed={!raw}
          >
            {text(lang, "preview")}
          </button>
          <button
            className={raw ? "selected" : ""}
            onClick={() => setRaw(true)}
            aria-pressed={raw}
          >
            {text(lang, "markdown")}
          </button>
        </div>
        <div className="toolbar-actions">
          <Copy value={markdown} lang={lang} />
          <button className="text-button download" onClick={download}>
            <Icon name="download" />
            {text(lang, "download")}
          </button>
        </div>
      </div>
      <article
        className="report-paper"
        data-testid="report-content"
        tabIndex={0}
        aria-label={text(lang, "report")}
      >
        {raw ? (
          <pre className="raw-markdown">{markdown}</pre>
        ) : (
          <div className="markdown">
            <ReactMarkdown
              skipHtml
              remarkPlugins={[remarkGfm]}
              components={{
                img: () => null,
                a: ({ children, href }) =>
                  href ? (
                    <a href={href} target="_blank" rel="noopener noreferrer">
                      {children}
                    </a>
                  ) : (
                    <span>{children}</span>
                  ),
              }}
            >
              {markdown}
            </ReactMarkdown>
          </div>
        )}
      </article>
    </section>
  );
}

export function ReviewPanel({
  review,
  lang,
  onDecided,
}: {
  review: Review;
  lang: Language;
  onDecided: () => void;
}) {
  const [selectedProfile, setProfile] = useState(
    review.allowed_knowledge_profiles[0] || "",
  );
  const [tokens, setTokens] = useState(
    String(
      Math.min(
        review.context.output_tokens * 2,
        review.output_ceiling ?? Infinity,
      ),
    ),
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<"conflict" | "network" | "">("");
  const decision = useRef({ key: "", id: "" });
  const output = review.context.reason === "analysis_generation_limit";
  const pending = !review.decision;
  async function decide(
    action: "increase_budget" | "accept_partial" | "reject",
  ) {
    const fields =
      action !== "increase_budget"
        ? {}
        : output
          ? { max_output_tokens: Number(tokens) }
          : { knowledge_budget_profile: selectedProfile };
    const key = JSON.stringify({ review: review.review_id, action, ...fields });
    if (key !== decision.current.key)
      decision.current = { key, id: crypto.randomUUID() };
    setBusy(true);
    setError("");
    try {
      await request(
        `/investigations/${encodeURIComponent(review.task_id)}/reviews/${review.review_id}/decision`,
        undefined,
        { action, decision_id: decision.current.id, ...fields },
      );
      onDecided();
    } catch (exc) {
      setError(
        exc instanceof ApiError && exc.status === 409 ? "conflict" : "network",
      );
    } finally {
      setBusy(false);
    }
  }
  const validTokens =
    /^\d+$/.test(tokens) &&
    Number.isSafeInteger(Number(tokens)) &&
    Number(tokens) > review.context.output_tokens &&
    (review.output_ceiling === null || Number(tokens) <= review.output_ceiling);
  const reason =
    (
      {
        source_files: ["Source 文件数", "Source file count"],
        source_bytes: ["Source 总字节数", "Source bytes"],
        source_chunks: ["Source 切片数", "Source chunks"],
        preparation_timeout_seconds: ["知识准备期限", "Preparation deadline"],
        analysis_generation_limit: [
          "分析输出预算",
          "Analysis output allowance",
        ],
      } as Record<string, string[]>
    )[review.context.reason]?.[lang === "zh" ? 0 : 1] ?? review.context.reason;
  if (!pending) return null;
  return (
    <section className="review-panel">
      <p className="eyebrow">{text(lang, "waiting_review")}</p>
      <h2>{text(lang, "review")}</h2>
      <p>{text(lang, "reviewNote")}</p>
      <div className="review-context">
        <span>{reason}</span>
        {review.context.observed !== null && (
          <span>
            {text(lang, "observed")}: {review.context.observed}
          </span>
        )}
      </div>
      {review.can_increase_budget && (
        <div className="review-budget">
          {output ? (
            <label>
              {text(lang, "outputTokens")}
              <input
                type="number"
                min={review.context.output_tokens + 1}
                max={review.output_ceiling ?? undefined}
                value={tokens}
                onChange={(event) => setTokens(event.target.value)}
              />
              <small>{text(lang, "outputHelp")}</small>
            </label>
          ) : (
            <label>
              {text(lang, "approveProfile")}
              <select
                value={selectedProfile}
                onChange={(event) => setProfile(event.target.value)}
              >
                {review.allowed_knowledge_profiles.map((value) => (
                  <option key={value} value={value}>
                    {profile(lang, value)}
                  </option>
                ))}
              </select>
            </label>
          )}
          <button
            className="button primary"
            disabled={busy || (output ? !validTokens : !selectedProfile)}
            onClick={() => void decide("increase_budget")}
          >
            {text(lang, busy ? "deciding" : "increase")}
          </button>
        </div>
      )}
      {review.can_accept_partial && review.context.partial_result && (
        <div className="partial-option">
          <p>{text(lang, "acceptNote")}</p>
          <details className="partial-preview" open>
            <summary>{text(lang, "partialPreview")}</summary>
            <p>{review.context.partial_result.preliminary_analysis.summary}</p>
            <h3>{text(lang, "findings")}</h3>
            <ul>
              {review.context.partial_result.preliminary_analysis.findings.map(
                (finding, index) => (
                  <li key={index}>
                    {finding.statement}
                    <small>{finding.evidence_ids.join(" · ")}</small>
                  </li>
                ),
              )}
            </ul>
            <h3>{text(lang, "hypotheses")}</h3>
            <ul>
              {review.context.partial_result.preliminary_analysis.hypotheses.map(
                (value, index) => (
                  <li key={index}>{value}</li>
                ),
              )}
            </ul>
            <h3>{text(lang, "questions")}</h3>
            <ul>
              {review.context.partial_result.preliminary_analysis.unanswered_questions.map(
                (value, index) => (
                  <li key={index}>{value}</li>
                ),
              )}
            </ul>
            <h3>{text(lang, "suppliedEvidence")}</h3>
            {review.context.partial_result.analysis_evidence.map((evidence) => (
              <details key={evidence.evidence_id}>
                <summary>
                  <code>{evidence.evidence_id}</code>
                </summary>
                <a
                  href={
                    evidence.source_url.startsWith("https://github.com/")
                      ? evidence.source_url
                      : undefined
                  }
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  {evidence.source_url}
                </a>
                <pre>{evidence.content}</pre>
              </details>
            ))}
          </details>
          <button
            className="button secondary"
            disabled={busy}
            onClick={() => void decide("accept_partial")}
          >
            {text(lang, "accept")}
          </button>
        </div>
      )}
      <button
        className="text-button danger"
        disabled={busy}
        onClick={() => void decide("reject")}
      >
        {text(lang, "reject")}
      </button>
      {error && (
        <p role="alert" className="form-error">
          {text(lang, error)}
        </p>
      )}
    </section>
  );
}
