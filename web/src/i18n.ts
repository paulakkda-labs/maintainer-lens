import type { Language } from "./types";

const en = {
  investigate: "Investigate",
  records: "Execution records",
  workspace: "PUBLIC REPOSITORIES",
  heading: "Understand the issue.",
  intro:
    "Follow the evidence, inspect the analysis, and find a useful next step.",
  url: "GitHub Issue URL",
  submit: "Investigate",
  submitting: "Checking…",
  example: "Public GitHub Issues only",
  advanced: "Preparation budget",
  profile: "Budget profile",
  budgetNote:
    "Knowledge preparation only. The investigation remains limited to three search rounds and a 120-second execution segment.",
  unlimited: "Unlimited preparation",
  unlimitedNote:
    "No task-level preparation limits. Downloads, embedding costs and waiting time may increase.",
  task: "Task ID",
  copy: "Copy",
  copied: "Copied",
  generation: "Generation",
  round: "Round",
  progress: "Execution",
  noEvents: "Waiting for the first recorded event…",
  refresh: "Refresh",
  pending: "Queued",
  running: "Investigating",
  succeeded: "Completed",
  failed: "Failed",
  waiting_review: "Awaiting review",
  gaps: "More evidence needed",
  report: "Investigation report",
  preview: "Preview",
  markdown: "Markdown",
  download: "Download .md",
  reportNote:
    "A preliminary assessment grounded in the saved investigation context.",
  reportPending:
    "The report will appear after analysis and result storage complete.",
  reportFailed:
    "This task did not produce a completed report. Inspect its recorded error.",
  empty: "Your investigation will appear here.",
  emptyNote:
    "Submit an Issue URL to prepare or reuse repository knowledge and investigate its evidence.",
  historical: "Previous report",
  historicalNote:
    "This report belongs to an earlier task. The current investigation has not produced a new report.",
  unavailable: "This historical task has no saved report.",
  languageNote:
    "The report keeps its original language. Generate a separate English report when you need one.",
  generate: "Generate English report",
  logsHeading: "Follow the execution.",
  logsIntro:
    "Look up a task to inspect its recorded phases, recovery cycles and safe failure details.",
  findTask: "Task ID or GitHub Issue URL",
  find: "Look up",
  back: "Back to your investigation",
  history: "Matching investigations",
  noHistory: "No saved investigations match this URL.",
  older: "Older investigations",
  detail: "Task details",
  lastError: "Last recorded error",
  retryable: "Eligible for bounded retry",
  permanent: "Not eligible for automatic retry",
  errorNote:
    "This error may belong to an earlier attempt. Compare its generation and event time.",
  readError:
    "Unable to read the current state. Retrying a read will not submit a new investigation.",
  invalid:
    "Enter a public GitHub Issue URL, for example https://github.com/owner/repo/issues/123.",
  notFound: "Task not found. Check the ID or search by Issue URL.",
  conflict:
    "The task changed. Refresh its current state before deciding again.",
  network:
    "The request could not be confirmed. Use a read-only lookup before submitting again.",
  viewRecords: "View execution records",
  lastUpdated: "Latest event",
  reportLanguage: "Report language",
  current: "Metadata checked",
  stale: "Inputs changed",
  unknown: "Freshness unknown",
  checking: "Checking latest state",
  not_checked: "Not checked separately",
  source: "Source",
  historyName: "History",
  files: "files",
  chunks: "chunks",
  items: "items",
  review: "A decision is needed",
  reviewNote:
    "The task is paused. Inspect the recorded limit before choosing how to continue.",
  increase: "Raise budget & resume",
  accept: "Accept valid stage analysis",
  partialPreview: "Stage analysis for this decision",
  findings: "Supported findings",
  hypotheses: "Unverified hypotheses",
  questions: "Open questions",
  suppliedEvidence: "Actual analysis context",
  reject: "End investigation",
  outputTokens: "Approved output token budget",
  observed: "Observed",
  approveProfile: "New preparation profile",
  acceptNote:
    "Accepts the last complete, validated analysis with its limitations. It does not accept truncated output or certify factual correctness.",
  deciding: "Saving decision…",
  outputHelp: "Choose a limit your configured model supports.",
  footer: "Public repositories. Read-only investigations.",
  readOnly: "Read-only history lookup; no investigation is submitted.",
  stageNote:
    "Recorded phases, not a percentage estimate. Knowledge may be reused or built.",
  open: "Open",
  closed: "Closed",
  budgetUnavailable: "Budget profiles are temporarily unavailable.",
  earlierGeneration: "Earlier generation",
  decisionSaved: "Decision saved. Refreshing the task…",
};
type Key = keyof typeof en;
const zh: Record<Key, string> = {
  investigate: "调查",
  records: "执行记录",
  workspace: "公开仓库",
  heading: "分析 Issue",
  intro: "沿着证据理解问题，查看分析与下一步建议。",
  url: "GitHub Issue 链接",
  submit: "开始调查",
  submitting: "正在检查…",
  example: "支持公开 GitHub 仓库的 Issue",
  advanced: "Token 消耗预算",
  profile: "预算档位",
  budgetNote:
    "档位控制知识准备的数据量与时限，会影响 Token 消耗，但不设置整次调查的总 Token 上限。调查仍最多三轮，每次执行段期限为 120 秒。",
  unlimited: "无限准备预算",
  unlimitedNote:
    "放开任务级知识准备上限，下载量、Embedding 费用和等待时间可能增加。",
  task: "任务 ID",
  copy: "复制",
  copied: "已复制",
  generation: "执行代次",
  round: "检索轮次",
  progress: "执行进度",
  noEvents: "正在等待首条执行记录…",
  refresh: "刷新",
  pending: "排队中",
  running: "调查中",
  succeeded: "已完成",
  failed: "调查失败",
  waiting_review: "等待人工审核",
  gaps: "仍需更多证据",
  report: "调查报告",
  preview: "预览",
  markdown: "Markdown 原文",
  download: "下载 .md",
  reportNote: "基于本次保存的分析上下文生成的初步判断。",
  reportPending: "分析和结果保存完成后，报告会显示在这里。",
  reportFailed: "本次任务没有生成完整报告，请查看已记录的错误原因。",
  empty: "调查将从这里开始。",
  emptyNote:
    "提交 Issue 链接后，系统会准备或复用仓库知识，再依据证据开展调查。",
  historical: "历史报告",
  historicalNote: "这份报告属于较早的任务，当前调查尚未产生新报告。",
  unavailable: "这个历史任务没有保存的报告。",
  languageNote: "已有报告保留原语言。需要中文版本时，可单独生成一份中文报告。",
  generate: "生成中文报告",
  logsHeading: "查看每一步执行。",
  logsIntro: "通过任务 ID 查阅已记录的阶段、恢复代次和安全错误信息。",
  findTask: "任务 ID 或 GitHub Issue 链接",
  find: "查询记录",
  back: "返回上次调查",
  history: "相关调查",
  noHistory: "没有找到这个 Issue 的调查记录。",
  older: "更早的调查",
  detail: "任务详情",
  lastError: "最近记录的错误",
  retryable: "可按有限重试策略恢复",
  permanent: "不支持自动重试",
  errorNote: "错误可能来自较早的尝试，请结合执行代次和事件时间查看。",
  readError: "暂时无法读取当前状态，重试查询不会重新提交调查。",
  invalid:
    "请输入公开 GitHub Issue 链接，例如 https://github.com/owner/repo/issues/123。",
  notFound: "没有找到任务，请检查 ID，或通过 Issue 链接查找。",
  conflict: "任务状态已变化，请刷新后再操作。",
  network: "暂时无法确认请求结果，请先只读查询记录，再决定是否重新提交。",
  viewRecords: "查看执行记录",
  lastUpdated: "最近事件",
  reportLanguage: "报告语言",
  current: "元数据已核对",
  stale: "调查输入已变化",
  unknown: "最新状态未知",
  checking: "正在核对最新状态",
  not_checked: "尚未单独核对",
  source: "源码",
  historyName: "历史讨论",
  files: "个文件",
  chunks: "个切片",
  items: "条记录",
  review: "需要一个人工决定",
  reviewNote: "调查已暂停，请查看触发的预算限制，再选择如何继续。",
  increase: "提高预算并续跑",
  accept: "接受有效阶段分析",
  partialPreview: "本次决定对应的阶段分析",
  findings: "有证据支持的发现",
  hypotheses: "待验证假设",
  questions: "未解决问题",
  suppliedEvidence: "最近实际分析上下文",
  reject: "结束调查",
  outputTokens: "批准的输出 Token 预算",
  observed: "实际观测值",
  approveProfile: "新的准备档位",
  acceptNote:
    "接受最近一次完整、通过校验的分析及其局限，不接受截断输出，也不代表事实已获独立验证。",
  deciding: "正在保存决定…",
  outputHelp: "请填写部署模型实际支持的输出上限。",
  footer: "公开仓库 · 只读调查",
  readOnly: "只读查找历史记录，不会提交新调查。",
  stageNote:
    "展示已记录阶段，不估算百分比。知识准备可能复用已有数据，也可能进行构建。",
  open: "打开中",
  closed: "已关闭",
  budgetUnavailable: "暂时无法读取预算档位。",
  earlierGeneration: "较早的执行代次",
  decisionSaved: "决定已保存，正在刷新任务…",
};
export const text = (lang: Language, key: Key) =>
  (lang === "zh" ? zh : en)[key];
const phases: Record<string, [string, string]> = {
  queued: ["等待领取", "Waiting for a worker"],
  starting: ["开始执行", "Starting execution"],
  snapshot: ["获取 GitHub Issue", "Fetching the GitHub Issue"],
  preparing_source: [
    "准备或复用 Source 知识",
    "Preparing or reusing Source knowledge",
  ],
  preparing_history: [
    "准备或同步 History 知识",
    "Preparing or syncing History knowledge",
  ],
  preparing_query: ["准备检索查询", "Preparing the search query"],
  searching_history: ["检索 History", "Searching History"],
  searching_source: ["检索 Source", "Searching Source"],
  analyzing: ["分析证据", "Analyzing evidence"],
  planning: ["规划后续检索", "Planning a follow-up search"],
  tool_search: ["执行后续检索", "Running a follow-up search"],
  finalizing: ["校验并保存结果", "Validating and saving the result"],
  completed: ["调查已完成", "Investigation completed"],
  waiting_review: ["等待人工审核", "Awaiting a human decision"],
};
const kinds: Record<string, [string, string]> = {
  failed: ["本次执行失败", "Execution failed"],
  retry_scheduled: ["已安排重试", "Retry scheduled"],
  review_requested: ["请求人工审核", "Human review requested"],
  review_approved: ["已批准续跑", "Resume approved"],
  review_rejected: ["人工结束调查", "Investigation ended by review"],
};
export function phase(lang: Language, code: string, kind?: string) {
  const entry = (kind && kinds[kind]) || phases[code];
  return entry
    ? entry[lang === "zh" ? 0 : 1]
    : lang === "zh"
      ? "执行阶段"
      : "Execution phase";
}
const failures: Record<string, [string, string]> = {
  invalid_data: [
    "输入或上游数据未通过校验。",
    "Input or upstream data failed validation.",
  ],
  invalid_result: [
    "分析结果或证据引用未通过校验。",
    "Analysis or evidence references failed validation.",
  ],
  invalid_checkpoint: [
    "已保存进度无效，或与当前配置不兼容。",
    "Saved progress is invalid or incompatible.",
  ],
  graph_deadline: [
    "本次调查执行段超过期限。",
    "The investigation execution deadline expired.",
  ],
  preparation_deadline: [
    "知识准备超过期限。",
    "Knowledge preparation exceeded its deadline.",
  ],
  preparation_budget: [
    "知识准备达到所选预算上限。",
    "Knowledge preparation reached its budget.",
  ],
  github_rate_limited: [
    "GitHub 请求达到速率限制。",
    "GitHub rate limit reached.",
  ],
  upstream_timeout: ["上游服务请求超时。", "An upstream service timed out."],
  upstream_unavailable: [
    "暂时无法访问上游服务。",
    "An upstream service is unavailable.",
  ],
  source_not_ready: ["Source 索引尚不可用。", "The Source index is not ready."],
  analysis_output_limit: [
    "模型输出达到预算上限。",
    "Model generation reached its output allowance.",
  ],
  analysis_incomplete: [
    "模型未返回完整、有效的分析。",
    "Model generation did not complete a valid analysis.",
  ],
  review_rejected: [
    "人工决定结束此次调查。",
    "A human ended this investigation.",
  ],
};
export function failure(lang: Language, code: string) {
  const known = failures[code];
  if (known) return known[lang === "zh" ? 0 : 1];
  const http = /^(github|upstream)_http_(\d{3})$/.exec(code);
  if (http)
    return lang === "zh"
      ? `上游服务返回 HTTP ${http[2]}。`
      : `Upstream service returned HTTP ${http[2]}.`;
  return lang === "zh"
    ? "执行出现问题，请保留任务 ID 供开发者排查。"
    : "Processing failed. Keep the task ID for diagnosis.";
}
export function date(lang: Language, value?: string | null) {
  return value
    ? new Date(value).toLocaleString(lang === "zh" ? "zh-CN" : "en-GB", {
        hour12: false,
      })
    : "—";
}
export function profile(lang: Language, value: string) {
  return (
    (
      {
        small: ["小型", "Small"],
        standard: ["标准", "Standard"],
        large: ["大型", "Large"],
        unlimited: ["无限", "Unlimited"],
      } as Record<string, string[]>
    )[value]?.[lang === "zh" ? 0 : 1] ?? value
  );
}
