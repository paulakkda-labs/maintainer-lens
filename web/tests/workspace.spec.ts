import { expect, test, type Page } from "@playwright/test";
import type { Detail, Event, Language } from "../src/types";

const EN = "11111111-1111-4111-8111-111111111111";
const ZH = "22222222-2222-4222-8222-222222222222";
const OTHER = "33333333-3333-4333-8333-333333333333";
const issueUrl = "https://github.com/example/project/issues/42";
const now = "2026-10-30T03:00:00Z";
const report =
  "# Findings\n\nA supported observation.\n\n| Evidence | Result |\n| --- | --- |\n| Source | Available |\n\n[Evidence](https://github.com/example/project/issues/42)\n\n<script>window.reportScriptExecuted=true</script>\n\n[Unsafe](javascript:alert(1))";

function event(eventId: number, phase: string, kind = "progress"): Event {
  return {
    event_id: eventId,
    phase,
    kind,
    generation: 0,
    created_at: now,
    search_round: 1,
    error: null,
  };
}

async function backend(page: Page) {
  const state = {
    done: false,
    failed: false,
    review: false,
    partialReview: false,
    reportMarkdown: null as string | null,
    decisionConflict: false,
    posts: [] as Record<string, unknown>[],
    decisions: [] as Record<string, unknown>[],
    reads: [] as string[],
  };
  await page.route("**/investigations**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (request.method() === "POST") {
      const body = request.postDataJSON() as Record<string, unknown>;
      if (path.endsWith("/from-url")) {
        state.posts.push(body);
        const lang = body.response_language as Language;
        return route.fulfill({
          status: 202,
          json: {
            task_id: lang === "zh" ? ZH : EN,
            response_language: lang,
            status: "pending",
            generation: 0,
            decision: "created",
            report_task_id: null,
            report_markdown: null,
            freshness: { status: "not_checked" },
          },
        });
      }
      if (path.endsWith("/decision")) {
        state.decisions.push(body);
        if (state.decisionConflict) {
          state.decisionConflict = false;
          return route.fulfill({
            status: 409,
            json: { detail: "private provider text" },
          });
        }
        state.review = false;
        return route.fulfill({ json: {} });
      }
      throw new Error(`Unexpected mutation: ${path}`);
    }
    state.reads.push(request.url());
    if (path.endsWith("/budget-profiles"))
      return route.fulfill({
        json: ["small", "standard", "large", "unlimited"].map((profile) => ({
          profile,
          source_max_files: profile === "unlimited" ? null : 500,
          source_max_bytes: 8388608,
          source_max_chunks: 2000,
          history_max_items: 3,
          preparation_timeout_seconds: 900,
        })),
      });
    if (path.endsWith("/lookup"))
      return route.fulfill({
        json: {
          repository: "example/project",
          issue_number: 42,
          tasks: [
            {
              task_id: EN,
              status: "succeeded",
              generation: 0,
              response_language: "en",
              created_at: now,
              updated_at: now,
            },
          ],
          next_before: null,
        },
      });
    if (path.endsWith("/reviews"))
      return route.fulfill({
        json: state.review
          ? [
              {
                review_id: "44444444-4444-4444-8444-444444444444",
                task_id: EN,
                generation: 0,
                context: {
                  reason: state.partialReview
                    ? "analysis_generation_limit"
                    : "source_files",
                  observed: state.partialReview ? 4096 : 501,
                  output_tokens: 4096,
                  knowledge_budget: { profile: "standard" },
                  base_execution: { response_language: "en" },
                  partial_result: state.partialReview
                    ? {
                        preliminary_analysis: {
                          summary: "A complete earlier analysis is available.",
                          findings: [
                            {
                              statement: "The excerpt declares cleanup.",
                              evidence_ids: ["source:sample:1"],
                            },
                          ],
                          hypotheses: [],
                          unanswered_questions: [
                            "Does this path reproduce the issue?",
                          ],
                        },
                        analysis_evidence: [
                          {
                            evidence_id: "source:sample:1",
                            source_url: issueUrl,
                            content: "async def cleanup(): pass",
                          },
                        ],
                      }
                    : null,
                },
                decision: null,
                can_increase_budget: true,
                allowed_knowledge_profiles: state.partialReview
                  ? []
                  : ["large", "unlimited"],
                can_accept_partial: state.partialReview,
                output_ceiling: null,
                budget_resumes_used: 0,
                user_recoveries_used: 0,
              },
            ]
          : [],
      });
    if (path.endsWith("/events")) {
      const events = [
        event(1, "queued", "queued"),
        event(2, "preparing_source"),
        event(3, "analyzing"),
      ];
      if (state.done) events.push(event(4, "completed", "completed"));
      return route.fulfill({
        json: {
          events: events.filter(
            (item) =>
              item.event_id > Number(url.searchParams.get("after") || 0),
          ),
          next_after: null,
        },
      });
    }
    const taskId = path.split("/").at(-1)!;
    if (![EN, ZH, OTHER].includes(taskId))
      return route.fulfill({
        status: 404,
        json: { detail: "Investigation not found" },
      });
    const lang: Language = taskId === ZH ? "zh" : "en";
    const detail: Detail = {
      task_id: taskId,
      response_language: lang,
      repository: "example/project",
      issue_number: 42,
      status: state.failed
        ? "failed"
        : state.review
          ? "waiting_review"
          : state.done
            ? "succeeded"
            : "running",
      generation: 0,
      progress: event(3, "analyzing"),
      last_error: state.failed
        ? {
            code: "invalid_data",
            message: "SECRET PROVIDER BODY",
            retryable: false,
          }
        : null,
      issue: {
        title: "Clarify cancellation cleanup",
        state: "open",
        source_url: issueUrl,
      },
      result: state.done
        ? {
            next_step: "needs_more_evidence",
            response_language: lang,
            search_round: 3,
          }
        : null,
      report_markdown: state.done
        ? (state.reportMarkdown ??
          (lang === "zh" ? "# 中文结论\n\n仍需进一步调查。" : report))
        : null,
      previous_report_task_id: null,
      previous_report_markdown: null,
      freshness: {
        status: "current",
        checked_at: now,
        observed_issue_state: "open",
      },
    };
    return route.fulfill({ json: detail });
  });
  return state;
}

async function submit(page: Page, button = "Investigate") {
  await page
    .getByLabel(
      button === "开始调查" ? "GitHub Issue 链接" : "GitHub Issue URL",
    )
    .fill(issueUrl);
  await page.getByRole("button", { name: button, exact: true }).click();
}

test("English default, live progress, safe preview, download, and independent retained workspaces", async ({
  page,
}) => {
  const state = await backend(page);
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Understand the issue.", exact: true }),
  ).toHaveText("Understand the issue.");
  await submit(page);
  await expect(page.getByTestId("task-id")).toHaveText(EN);
  await expect(
    page.getByText("Analyzing evidence", { exact: true }),
  ).toBeVisible();
  expect(state.posts[0].response_language).toBe("en");
  state.done = true;
  await expect(
    page.getByRole("heading", { name: "Findings", exact: true }),
  ).toBeVisible();
  await expect(page.locator(".markdown table")).toHaveCount(1);
  expect(await page.evaluate(() => "reportScriptExecuted" in window)).toBe(
    false,
  );
  await expect(
    page.getByRole("link", { name: "Unsafe", exact: true }),
  ).toHaveCount(0);
  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download .md" }).click();
  expect((await downloadPromise).suggestedFilename()).toBe(
    "example-project-42-en.md",
  );
  await page
    .getByRole("link", { name: "Execution records", exact: true })
    .click();
  await page.getByLabel("Task ID or GitHub Issue URL").fill(OTHER);
  await page.getByRole("button", { name: "Look up", exact: true }).click();
  await expect(page.getByTestId("task-id")).toHaveText(OTHER);
  await page
    .getByRole("button", { name: "Back to your investigation" })
    .click();
  await expect(page.getByTestId("task-id")).toHaveText(EN);
  await expect(
    page.getByRole("heading", { name: "Findings", exact: true }),
  ).toBeVisible();
  await page.reload();
  await expect(page.getByTestId("task-id")).toHaveText(EN);
  await expect(
    page.getByRole("heading", { name: "Findings", exact: true }),
  ).toBeVisible();
  expect(state.posts).toHaveLength(1);
});

test("language toggle changes UI and phases without regenerating the existing report", async ({
  page,
}) => {
  const state = await backend(page);
  state.done = true;
  await page.goto("/");
  await page.getByRole("button", { name: "中文", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "分析 Issue", exact: true }),
  ).toBeVisible();
  await submit(page, "开始调查");
  await expect(page.getByRole("heading", { name: "中文结论" })).toBeVisible();
  expect(state.posts[0].response_language).toBe("zh");
  await page.getByRole("button", { name: "EN", exact: true }).click();
  await expect(
    page.getByText("Analyzing evidence", { exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("heading", { name: "中文结论" })).toBeVisible();
  expect(state.posts).toHaveLength(1);
  await page.getByRole("button", { name: "Generate English report" }).click();
  await expect(page.getByTestId("task-id")).toHaveText(EN);
  expect(state.posts[1].response_language).toBe("en");
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "Understand the issue.", exact: true }),
  ).toHaveText("Understand the issue.");
  expect(state.posts).toHaveLength(2);
});

test("budget control keeps its scope and selected profile across interface languages", async ({
  page,
}) => {
  const state = await backend(page);
  await page.goto("/");
  const budget = page.locator(".budget-options summary");
  await expect(budget).toContainText("Preparation budget");
  await expect(budget.locator(".budget-emphasis")).toHaveText("*");
  await budget.click();
  await expect(
    page.getByText("Knowledge preparation only.", { exact: false }),
  ).toBeVisible();
  await page
    .getByRole("combobox", { name: "Budget profile" })
    .selectOption("small");
  await page.getByRole("button", { name: "中文", exact: true }).click();
  await expect(budget).toContainText("Token 消耗预算");
  await expect(page.getByRole("combobox", { name: "预算档位" })).toHaveValue(
    "small",
  );
  await expect(
    page.getByText("但不设置整次调查的总 Token 上限", { exact: false }),
  ).toBeVisible();
  expect(state.posts).toHaveLength(0);
  await budget.click();
  await submit(page, "开始调查");
  expect(state.posts[0].knowledge_budget_profile).toBe("small");
});

test("URL history lookup and browser back navigation never submit work", async ({
  page,
}) => {
  const state = await backend(page);
  state.done = true;
  await page.goto("/logs");
  await page.getByLabel("Task ID or GitHub Issue URL").fill(issueUrl);
  await page.getByRole("button", { name: "Look up", exact: true }).click();
  await page.locator(".history-row").click();
  await expect(page.getByTestId("task-id")).toHaveText(EN);
  await page.getByRole("link", { name: "Investigate", exact: true }).click();
  await page.goBack();
  await expect(page.getByTestId("task-id")).toHaveText(EN);
  expect(state.posts).toHaveLength(0);
});

test("safe failure text is localized and unknown task queries do not create investigations", async ({
  page,
}) => {
  const state = await backend(page);
  state.failed = true;
  await page.goto("/logs");
  await page.getByLabel("Task ID or GitHub Issue URL").fill(EN);
  await page.getByRole("button", { name: "Look up", exact: true }).click();
  await expect(
    page.getByText("Input or upstream data failed validation.", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(page.getByText("SECRET PROVIDER BODY")).toHaveCount(0);
  await page.getByRole("button", { name: "中文", exact: true }).click();
  await expect(
    page.getByText("输入或上游数据未通过校验。", { exact: true }),
  ).toBeVisible();
  await page.getByLabel("任务 ID 或 GitHub Issue 链接").fill("missing-task");
  await page.getByRole("button", { name: "查询记录", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("没有找到任务");
  expect(state.posts).toHaveLength(0);
});

test("review is explicit, uses server eligibility, and retries a decision with the same id", async ({
  page,
}) => {
  const state = await backend(page);
  state.review = true;
  await page.goto("/");
  await submit(page);
  await expect(
    page.getByRole("heading", { name: "A decision is needed" }),
  ).toBeVisible();
  expect(state.decisions).toHaveLength(0);
  await expect(
    page.getByRole("button", { name: "Accept valid stage analysis" }),
  ).toHaveCount(0);
  state.decisionConflict = true;
  await page.getByRole("button", { name: "Raise budget & resume" }).click();
  await expect(page.getByRole("alert")).toContainText("The task changed");
  await page.getByRole("button", { name: "Raise budget & resume" }).click();
  await expect.poll(() => state.decisions.length).toBe(2);
  expect(state.decisions[0].decision_id).toBe(state.decisions[1].decision_id);
  expect(state.decisions[1].knowledge_budget_profile).toBe("large");
});

test("responsive layouts remain usable and produce local review screenshots", async ({
  page,
}) => {
  const state = await backend(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/");
  await page.screenshot({
    path: "../.local/web-review/desktop-empty.png",
    fullPage: true,
  });
  state.done = true;
  await submit(page);
  await expect(
    page.getByRole("heading", { name: "Findings", exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: "../.local/web-review/desktop-report.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "../.local/web-review/mobile-report.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "中文", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "分析 Issue", exact: true }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "../.local/web-review/mobile-report-zh.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.screenshot({
    path: "../.local/web-review/desktop-report-zh.png",
    fullPage: true,
  });
});

for (const viewport of [
  { width: 1440, height: 1000 },
  { width: 390, height: 844 },
]) {
  test(`report and page scroll independently at ${viewport.width}px`, async ({
    page,
  }) => {
    const state = await backend(page);
    state.done = true;
    state.reportMarkdown = `# Long report\n\n${Array.from(
      { length: 80 },
      (_, i) =>
        `## Finding ${i + 1}\n\nEvidence and limitations remain available throughout this long investigation report.`,
    ).join("\n\n")}`;
    await page.setViewportSize(viewport);
    await page.goto("/");
    await submit(page);
    const paper = page.getByTestId("report-content");
    await expect(paper).toBeVisible();

    for (const mode of ["Preview", "Markdown"]) {
      await page.getByRole("button", { name: mode, exact: true }).click();
      await expect
        .poll(() => paper.evaluate((el) => el.scrollHeight > el.clientHeight))
        .toBe(true);
      expect(
        await paper.evaluate((el) => el.getBoundingClientRect().height),
      ).toBeGreaterThanOrEqual(viewport.height);
      await paper.evaluate((el) => {
        el.scrollTop = 0;
      });
      await paper.scrollIntoViewIfNeeded();
      await paper.hover();
      const pageBefore = await page.evaluate(() => window.scrollY);
      await page.mouse.wheel(0, 300);
      await expect
        .poll(() => paper.evaluate((el) => el.scrollTop))
        .toBeGreaterThan(0);
      expect(await page.evaluate(() => window.scrollY)).toBe(pageBefore);

      await paper.focus();
      const beforeKey = await paper.evaluate((el) => el.scrollTop);
      const pageBeforeKey = await page.evaluate(() => window.scrollY);
      await page.keyboard.press("PageDown");
      await expect
        .poll(() => paper.evaluate((el) => el.scrollTop))
        .toBeGreaterThan(beforeKey);
      // Let keyboard scrolling finish before testing a wheel gesture elsewhere.
      await page.waitForTimeout(150);
      expect(await page.evaluate(() => window.scrollY)).toBe(pageBeforeKey);

      const reportBefore = await paper.evaluate((el) => el.scrollTop);
      expect(
        await paper.evaluate(
          (el) => el.scrollTop + el.clientHeight < el.scrollHeight,
        ),
      ).toBe(true);
      const outerBefore = await page.evaluate(() => window.scrollY);
      await page.mouse.move(8, viewport.height / 2);
      await page.mouse.wheel(0, -120);
      await expect
        .poll(() => page.evaluate(() => window.scrollY))
        .toBeLessThan(outerBefore);
      expect(await paper.evaluate((el) => el.scrollTop)).toBe(reportBefore);
      if (mode === "Preview") {
        await page.screenshot({
          path: `../.local/web-review/report-scroll-${viewport.width}.png`,
        });
      }

      for (const boundary of ["top", "bottom"]) {
        await paper.evaluate((el, edge) => {
          el.scrollTop = edge === "top" ? 0 : el.scrollHeight;
        }, boundary);
        await paper.hover();
        const pageAtBoundary = await page.evaluate(() => window.scrollY);
        await page.mouse.wheel(0, boundary === "top" ? -300 : 300);
        // Browser wheel scrolling settles asynchronously, including boundary chaining.
        await page.waitForTimeout(150);
        expect(await page.evaluate(() => window.scrollY)).toBe(pageAtBoundary);
      }
    }
  });
}

test("partial acceptance exposes the validated analysis and its actual context before decision", async ({
  page,
}) => {
  const state = await backend(page);
  state.review = true;
  state.partialReview = true;
  await page.goto("/");
  await submit(page);
  await expect(
    page.getByText("A complete earlier analysis is available.", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    page.getByText("The excerpt declares cleanup.", { exact: false }),
  ).toBeVisible();
  await page.locator(".partial-preview details summary").click();
  await expect(
    page.getByText("async def cleanup(): pass", { exact: true }),
  ).toBeVisible();
  expect(state.decisions).toHaveLength(0);
  await page.screenshot({
    path: "../.local/web-review/desktop-review.png",
    fullPage: true,
  });
  await page
    .getByRole("button", { name: "Accept valid stage analysis" })
    .click();
  await expect.poll(() => state.decisions.length).toBe(1);
  expect(state.decisions[0].action).toBe("accept_partial");
});
