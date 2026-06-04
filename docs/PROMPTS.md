# ChatGPT Prompts / ChatGPT 提示词

This repository generates prompt files automatically in each output pack:

- `NEXT_MESSAGE_PHASE1.txt`
- `NEXT_MESSAGE_PHASE2.txt`

Use them in the ChatGPT message box after uploading `CHATGPT_HANDOFF_INPUT.md`.

---

## Phase 1 — Workspace history map

```text
I uploaded a codex-session-handoff evidence pack.

Please run Phase 1 only. Do not write the final report yet.

Task: build a research/code workspace history map from the uploaded pack.

Use the pack's WORKSPACE_MAP, DOCS_AUTHORITY, HISTORY_TIMELINE, SESSION_CLUSTERING, included docs, and selected session digests. Treat session digests as supporting evidence, not final truth.

Please output:
1. The main workstreams detected in the workspace.
2. The likely historical evolution across versions, branches, and experiments.
3. Which revised Markdown documents appear to be the most trustworthy current summaries.
4. Which branches, files, or claims appear superseded, historical, or uncertain.
5. Which session clusters are representative evidence and which look like duplicates/background.
6. A concise list of questions I should manually confirm before you write the final report.

Do not invent files, commands, results, or conclusions. Mark uncertain claims as "unconfirmed".
```

## Phase 2 — Final report

```text
Here are my confirmations and corrections for Phase 1:

[Paste your corrections here.]

Please run Phase 2 now.

Using the uploaded evidence pack, your Phase 1 analysis, and my confirmations above, generate the final Markdown report.

The report should include:
1. Overall workspace goal and scope.
2. Historical evolution map.
3. Main workstreams and their roles.
4. Current most trustworthy documents and results.
5. Superseded or historical materials that should not override current summaries.
6. Known problems, weak evidence, and unresolved questions.
7. Recommended cleanup / organization / next research steps.
8. Reading order for the next human or AI agent.
9. Evidence index mapping major claims back to files, docs, or session clusters.

Do not invent unsupported details. Preserve exact paths when available.
```

---

# 中文说明

每次生成证据包后，工具会自动生成：

- `NEXT_MESSAGE_PHASE1.txt`
- `NEXT_MESSAGE_PHASE2.txt`

使用方式：

1. 上传 `CHATGPT_HANDOFF_INPUT.md`
2. 将 `NEXT_MESSAGE_PHASE1.txt` 复制到 ChatGPT 对话框
3. 阅读第一阶段历史路线图，并人工确认/修正
4. 将 `NEXT_MESSAGE_PHASE2.txt` 复制到 ChatGPT 对话框
5. 获得最终报告
