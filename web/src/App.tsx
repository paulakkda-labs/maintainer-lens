import { useEffect, useState } from "react";
import { ApiError, readSaved, request, save } from "./api";
import {
  Copy,
  ErrorNotice,
  FailureNotice,
  Icon,
  Report,
  ReviewPanel,
  StatusBadge,
  TaskIdentity,
  Timeline,
} from "./components";
import { date, profile, text } from "./i18n";
import { useTask } from "./useTask";
import type {
  Budget,
  Language,
  Lookup,
  Resolution,
  TaskSummary,
} from "./types";

type Workspace = { url: string; taskId: string; profile: string };
const validUrl = (value: string) =>
  /^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/issues\/[1-9][0-9]*\/?$/.test(
    value,
  );
function initialWorkspace(): Workspace {
  const stored = readSaved<Partial<Workspace>>("ml-investigation", {});
  return {
    url: typeof stored.url === "string" ? stored.url : "",
    taskId: typeof stored.taskId === "string" ? stored.taskId : "",
    profile: typeof stored.profile === "string" ? stored.profile : "standard",
  };
}
function getPage() {
  return location.pathname === "/logs" ? "logs" : "investigate";
}

export default function App() {
  const [lang, setLang] = useState<Language>(() =>
    readSaved<Language>("ml-language", "en", true) === "zh" ? "zh" : "en",
  );
  const [page, setPage] = useState(getPage);
  const [workspace, setWorkspace] = useState(initialWorkspace);
  const [resolution, setResolution] = useState<Resolution | null>(null);
  const [budgets, setBudgets] = useState<Budget[]>([]);
  const [budgetError, setBudgetError] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<"invalid" | "network" | "">(
    "",
  );
  const [logInput, setLogInput] = useState(() =>
    readSaved<string>("ml-log-input", ""),
  );
  const [logTaskId, setLogTaskId] = useState(() =>
    readSaved<string>("ml-log-task", ""),
  );
  const [matches, setMatches] = useState<TaskSummary[] | null>(null);
  const [lookupCursor, setLookupCursor] = useState<string | null>(null);
  const [lookupUrl, setLookupUrl] = useState("");
  const [lookupError, setLookupError] = useState<
    "invalid" | "notFound" | "readError" | ""
  >("");
  const [looking, setLooking] = useState(false);
  const investigation = useTask(workspace.taskId);
  const logs = useTask(logTaskId);
  const task = investigation.detail;
  const logTask = logs.detail;

  useEffect(() => {
    save("ml-language", lang, true);
    document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
  }, [lang]);
  useEffect(() => {
    save("ml-investigation", workspace);
  }, [workspace]);
  useEffect(() => {
    save("ml-log-input", logInput);
    save("ml-log-task", logTaskId);
  }, [logInput, logTaskId]);
  useEffect(() => {
    const controller = new AbortController();
    request<Budget[]>("/investigations/budget-profiles", controller.signal)
      .then(setBudgets)
      .catch(() => {
        if (!controller.signal.aborted) setBudgetError(true);
      });
    const change = () => setPage(getPage());
    window.addEventListener("popstate", change);
    return () => {
      controller.abort();
      window.removeEventListener("popstate", change);
    };
  }, []);
  function navigate(next: "logs" | "investigate") {
    const path = next === "logs" ? "/logs" : "/";
    if (location.pathname !== path) history.pushState(null, "", path);
    setPage(next);
    window.scrollTo({ top: 0, behavior: "instant" });
  }
  function openRecords(id: string) {
    setLogTaskId(id);
    setLogInput(id);
    setMatches(null);
    setLookupError("");
    navigate("logs");
  }
  async function submit(url = workspace.url) {
    const selectedUrl = url.trim();
    if (!validUrl(selectedUrl)) {
      setSubmitError("invalid");
      return;
    }
    setSubmitting(true);
    setSubmitError("");
    try {
      const result = await request<Resolution>(
        "/investigations/from-url",
        undefined,
        {
          issue_url: selectedUrl,
          response_language: lang,
          knowledge_budget_profile: workspace.profile,
        },
      );
      setResolution(result);
      setWorkspace((old) => ({
        ...old,
        url: selectedUrl,
        taskId: result.task_id,
      }));
      if (result.task_id === workspace.taskId) investigation.refresh();
    } catch (exc) {
      setSubmitError(
        exc instanceof ApiError && exc.status === 422 ? "invalid" : "network",
      );
    } finally {
      setSubmitting(false);
    }
  }
  async function lookup(older = false) {
    const query = (older ? lookupUrl : logInput).trim();
    setLooking(true);
    setLookupError("");
    try {
      if (query.startsWith("https://") || query.includes("github.com")) {
        if (!validUrl(query)) {
          setLookupError("invalid");
          return;
        }
        const params = new URLSearchParams({ issue_url: query, limit: "20" });
        if (older && lookupCursor) params.set("before", lookupCursor);
        const result = await request<Lookup>(
          `/investigations/lookup?${params}`,
        );
        setMatches((old) =>
          older ? [...(old || []), ...result.tasks] : result.tasks,
        );
        setLookupCursor(result.next_before);
        setLookupUrl(query);
      } else if (
        query &&
        query.length <= 64 &&
        /^[A-Za-z0-9_-]+$/.test(query)
      ) {
        setMatches(null);
        setLogTaskId(query);
        if (query === logTaskId) logs.refresh();
      } else setLookupError("notFound");
    } catch {
      setLookupError("readError");
    } finally {
      setLooking(false);
    }
  }
  const selectedBudget = budgets.find(
    (budget) => budget.profile === workspace.profile,
  );
  const markdown =
    task?.report_markdown ||
    task?.previous_report_markdown ||
    (resolution?.task_id === workspace.taskId
      ? resolution.report_markdown
      : null);
  const ownerId = task?.report_markdown
    ? task.task_id
    : task?.previous_report_task_id || resolution?.report_task_id || "";
  const historical = !!markdown && ownerId !== workspace.taskId;
  const reportLang = task?.response_language || resolution?.response_language;
  const fresh =
    resolution?.report_task_id === ownerId
      ? resolution?.freshness
      : task?.freshness;
  const filename = `${task?.repository.replaceAll("/", "-") || "investigation"}-${task?.issue_number || "report"}-${reportLang || lang}.md`;
  const activeReview = investigation.reviews.find(
    (review) => !review.decision && review.generation === task?.generation,
  );

  return (
    <>
      <header className="site-header">
        <a
          className="brand"
          href="/"
          onClick={(event) => {
            event.preventDefault();
            navigate("investigate");
          }}
        >
          <span className="brand-mark">M</span>MaintainerLens
          <span className="brand-period">.</span>
        </a>
        <div className="header-controls">
          <nav aria-label={lang === "zh" ? "主导航" : "Main navigation"}>
            <a
              href="/"
              className={page === "investigate" ? "active" : ""}
              onClick={(event) => {
                event.preventDefault();
                navigate("investigate");
              }}
            >
              {text(lang, "investigate")}
            </a>
            <a
              href="/logs"
              className={page === "logs" ? "active" : ""}
              onClick={(event) => {
                event.preventDefault();
                navigate("logs");
              }}
            >
              {text(lang, "records")}
            </a>
          </nav>
          <div className="language-switch" aria-label="Language">
            <button
              aria-pressed={lang === "en"}
              className={lang === "en" ? "active" : ""}
              onClick={() => setLang("en")}
            >
              EN
            </button>
            <span>/</span>
            <button
              aria-pressed={lang === "zh"}
              className={lang === "zh" ? "active" : ""}
              onClick={() => setLang("zh")}
            >
              中文
            </button>
          </div>
        </div>
      </header>
      <main>
        {page === "investigate" ? (
          <>
            <section className="intro">
              <p className="eyebrow">
                <span className="tiny-rule" />
                {text(lang, "workspace")}
              </p>
              <h1>{text(lang, "heading")}</h1>
              <p>{text(lang, "intro")}</p>
            </section>
            <section className="query-section">
              <form
                onSubmit={(event) => {
                  event.preventDefault();
                  void submit();
                }}
              >
                <label className="sr-only" htmlFor="issue-url">
                  {text(lang, "url")}
                </label>
                <div className="query-bar">
                  <span className="query-symbol">↗</span>
                  <input
                    id="issue-url"
                    type="url"
                    value={workspace.url}
                    onChange={(event) =>
                      setWorkspace((old) => ({
                        ...old,
                        url: event.target.value,
                      }))
                    }
                    placeholder="https://github.com/owner/repository/issues/123"
                    autoComplete="off"
                    required
                  />
                  <button
                    className="button primary"
                    disabled={submitting || !workspace.url.trim()}
                    type="submit"
                  >
                    {text(lang, submitting ? "submitting" : "submit")}
                    <Icon name="arrow" />
                  </button>
                </div>
              </form>
              <div className="query-foot">
                <span>{text(lang, "example")}</span>
                <details className="budget-options">
                  <summary>
                    <span className="budget-emphasis" aria-hidden="true">
                      *
                    </span>
                    {text(lang, "advanced")}{" "}
                    <span className="budget-profile">
                      {profile(lang, workspace.profile)}
                    </span>
                    ⌄
                  </summary>
                  <div className="budget-popover">
                    <label>
                      {text(lang, "profile")}
                      <select
                        value={workspace.profile}
                        onChange={(event) =>
                          setWorkspace((old) => ({
                            ...old,
                            profile: event.target.value,
                          }))
                        }
                      >
                        {(budgets.length
                          ? budgets.map((b) => b.profile)
                          : ["small", "standard", "large", "unlimited"]
                        ).map((value) => (
                          <option key={value} value={value}>
                            {profile(lang, value)}
                          </option>
                        ))}
                      </select>
                    </label>
                    {selectedBudget?.source_max_files !== null &&
                      selectedBudget && (
                        <p className="budget-numbers">
                          Source ≤ {selectedBudget.source_max_files}{" "}
                          {text(lang, "files")} ·{" "}
                          {Math.round(
                            (selectedBudget.source_max_bytes || 0) / 1024,
                          )}{" "}
                          KiB · {selectedBudget.source_max_chunks}{" "}
                          {text(lang, "chunks")}
                          <br />
                          History ≤ {selectedBudget.history_max_items}{" "}
                          {text(lang, "items")}
                        </p>
                      )}
                    <p>{text(lang, "budgetNote")}</p>
                    {workspace.profile === "unlimited" && (
                      <p className="budget-warning">
                        {text(lang, "unlimitedNote")}
                      </p>
                    )}
                    {budgetError && <p>{text(lang, "budgetUnavailable")}</p>}
                  </div>
                </details>
              </div>
              {submitError && (
                <p role="alert" className="form-error">
                  {text(lang, submitError)}
                </p>
              )}
            </section>
            {workspace.taskId && !task && (
              <div className="loading-task">
                <span className="live-dot" />
                <span>{text(lang, "task")}</span>
                <code>{workspace.taskId}</code>
                <Copy value={workspace.taskId} lang={lang} />
              </div>
            )}
            {investigation.error !== null && workspace.taskId && (
              <ErrorNotice
                status={investigation.error}
                lang={lang}
                onRefresh={investigation.refresh}
              />
            )}
            {task && (
              <>
                <TaskIdentity
                  task={task}
                  lang={lang}
                  onRecords={() => openRecords(task.task_id)}
                />
                <div className="investigation-layout">
                  <aside>
                    <Timeline
                      events={investigation.events}
                      lang={lang}
                      task={task}
                    />
                    <button
                      className="text-button refresh-link"
                      onClick={investigation.refresh}
                    >
                      {text(lang, "refresh")} ↻
                    </button>
                  </aside>
                  <div className="result-column">
                    <FailureNotice task={task} lang={lang} />
                    {activeReview && (
                      <ReviewPanel
                        key={activeReview.review_id}
                        review={activeReview}
                        lang={lang}
                        onDecided={investigation.refresh}
                      />
                    )}
                    {reportLang && reportLang !== lang && (
                      <div className="notice language-notice">
                        <p>{text(lang, "languageNote")}</p>
                        <button
                          className="text-button"
                          disabled={submitting}
                          onClick={() =>
                            void submit(
                              `https://github.com/${task.repository}/issues/${task.issue_number}`,
                            )
                          }
                        >
                          {text(lang, "generate")} ↗
                        </button>
                      </div>
                    )}
                    {markdown ? (
                      <Report
                        markdown={markdown}
                        lang={lang}
                        filename={filename}
                        historical={historical}
                        ownerId={ownerId}
                        freshness={fresh?.status}
                      />
                    ) : (
                      <div className="pending-report">
                        <span className="paper-outline" />
                        <h2>
                          {text(
                            lang,
                            task.status === "succeeded"
                              ? "unavailable"
                              : "report",
                          )}
                        </h2>
                        <p>
                          {text(
                            lang,
                            task.status === "succeeded"
                              ? "reportNote"
                              : task.status === "failed"
                                ? "reportFailed"
                                : "reportPending",
                          )}
                        </p>
                      </div>
                    )}
                  </div>
                </div>
              </>
            )}
            {!workspace.taskId && (
              <div className="empty-state">
                <span className="empty-mark" aria-hidden="true">
                  <i />
                  <i />
                  <i />
                </span>
                <h2>{text(lang, "empty")}</h2>
                <p>{text(lang, "emptyNote")}</p>
              </div>
            )}
          </>
        ) : (
          <>
            <section className="intro logs-intro">
              <p className="eyebrow">
                <span className="tiny-rule" />
                {text(lang, "records")}
              </p>
              <h1>{text(lang, "logsHeading")}</h1>
              <p>{text(lang, "logsIntro")}</p>
            </section>
            <section className="query-section">
              <form
                onSubmit={(event) => {
                  event.preventDefault();
                  void lookup();
                }}
              >
                <label className="sr-only" htmlFor="log-query">
                  {text(lang, "findTask")}
                </label>
                <div className="query-bar">
                  <Icon name="search" />
                  <input
                    id="log-query"
                    placeholder={text(lang, "findTask")}
                    value={logInput}
                    onChange={(event) => setLogInput(event.target.value)}
                    required
                    autoComplete="off"
                  />
                  <button
                    className="button primary"
                    disabled={looking || !logInput.trim()}
                  >
                    {text(lang, "find")}
                    <Icon name="arrow" />
                  </button>
                </div>
              </form>
              <div className="query-foot">
                <span>{text(lang, "readOnly")}</span>
                {workspace.taskId && (
                  <button
                    className="text-button"
                    onClick={() => navigate("investigate")}
                  >
                    ← {text(lang, "back")}
                  </button>
                )}
              </div>
              {lookupError && (
                <p role="alert" className="form-error">
                  {text(lang, lookupError)}
                </p>
              )}
            </section>
            {matches !== null && (
              <section className="history-list">
                <h2>{text(lang, "history")}</h2>
                {!matches.length && (
                  <p className="subtle">{text(lang, "noHistory")}</p>
                )}
                {matches.map((match) => (
                  <button
                    className="history-row"
                    key={match.task_id}
                    onClick={() => {
                      setLogTaskId(match.task_id);
                      if (match.task_id === logTaskId) logs.refresh();
                    }}
                  >
                    <code>{match.task_id}</code>
                    <span>
                      {match.response_language === "zh" ? "中文" : "English"}
                    </span>
                    <time>{date(lang, match.created_at)}</time>
                    <StatusBadge status={match.status} lang={lang} />
                  </button>
                ))}
                {lookupCursor && (
                  <button
                    className="text-button"
                    disabled={looking}
                    onClick={() => void lookup(true)}
                  >
                    {text(lang, "older")} ↓
                  </button>
                )}
              </section>
            )}
            {logs.error !== null && logTaskId && (
              <ErrorNotice
                status={logs.error}
                lang={lang}
                onRefresh={logs.refresh}
              />
            )}
            {logTask && (
              <>
                <TaskIdentity task={logTask} lang={lang} />
                <FailureNotice task={logTask} lang={lang} />
                <div className="logs-timeline">
                  <div className="logs-timeline-top">
                    <p>
                      {text(lang, "lastUpdated")}:{" "}
                      {date(lang, logs.events.at(-1)?.created_at)}
                    </p>
                    <button className="text-button" onClick={logs.refresh}>
                      {text(lang, "refresh")} ↻
                    </button>
                  </div>
                  <Timeline events={logs.events} lang={lang} task={logTask} />
                </div>
              </>
            )}
          </>
        )}
      </main>
      <footer>
        <span>MaintainerLens</span>
        <span>{text(lang, "footer")}</span>
        <a href="/docs" target="_blank" rel="noopener noreferrer">
          API ↗
        </a>
      </footer>
    </>
  );
}
