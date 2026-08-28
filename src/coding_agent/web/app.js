const elements = {
  form: document.querySelector("#run-form"),
  workspace: document.querySelector("#workspace-input"),
  prompt: document.querySelector("#prompt-input"),
  promptCount: document.querySelector("#prompt-count"),
  maxSteps: document.querySelector("#max-steps"),
  maxStepsValue: document.querySelector("#max-steps-value"),
  demo: document.querySelector("#demo-toggle"),
  runButton: document.querySelector("#run-button"),
  runButtonLabel: document.querySelector("#run-button .button-label"),
  stopButton: document.querySelector("#stop-button"),
  formMessage: document.querySelector("#form-message"),
  connectionDot: document.querySelector("#connection-dot"),
  connectionLabel: document.querySelector("#connection-label"),
  modelName: document.querySelector("#model-name"),
  runStatus: document.querySelector("#run-status"),
  statusLabel: document.querySelector("#run-status b"),
  metricSteps: document.querySelector("#metric-steps"),
  metricTools: document.querySelector("#metric-tools"),
  metricTime: document.querySelector("#metric-time"),
  metricTokens: document.querySelector("#metric-tokens"),
  emptyState: document.querySelector("#empty-state"),
  timeline: document.querySelector("#timeline"),
  traceViewport: document.querySelector("#trace-viewport"),
  finalTest: document.querySelector("#final-test"),
  finalTestStatus: document.querySelector("#final-test-status"),
  finalTestCommand: document.querySelector("#final-test-command"),
  runId: document.querySelector("#run-id"),
  sessionList: document.querySelector("#session-list"),
  sessionListEmpty: document.querySelector("#session-list-empty"),
  sessionCount: document.querySelector("#session-count"),
  newSession: document.querySelector("#new-session-button"),
  contextPercent: document.querySelector("#context-percent"),
  contextFill: document.querySelector("#context-fill"),
  contextDetail: document.querySelector("#context-detail"),
  chatMessages: document.querySelector("#chat-messages"),
  chatEmpty: document.querySelector("#chat-empty"),
  turnCount: document.querySelector("#turn-count"),
  imageInput: document.querySelector("#image-input"),
  pendingImages: document.querySelector("#pending-images"),
  visionState: document.querySelector("#vision-state"),
  runPlan: document.querySelector("#run-plan"),
  planProgress: document.querySelector("#plan-progress"),
  planExplanation: document.querySelector("#plan-explanation"),
  planItems: document.querySelector("#plan-items"),
  selectedTurnLabel: document.querySelector("#selected-turn-label"),
  approvalCard: document.querySelector("#approval-card"),
  approvalTitle: document.querySelector("#approval-title"),
  approvalDescription: document.querySelector("#approval-description"),
  approvalTool: document.querySelector("#approval-tool"),
  approvalDiffs: document.querySelector("#approval-diffs"),
  approveEdit: document.querySelector("#approve-edit"),
  rejectEdit: document.querySelector("#reject-edit"),
};

const state = {
  runId: null,
  sessionId: null,
  config: null,
  startedAt: null,
  pollTimer: null,
  clockTimer: null,
  lastEventSeq: 0,
  roundElements: new Map(),
  running: false,
  approvalId: null,
  selectedRunId: null,
  pendingImageFiles: [],
};

const statusLabels = {
  queued: "准备任务",
  running: "运行中",
  waiting_approval: "等待用户确认",
  completed: "已完成",
  error: "运行错误",
  cancelled: "已停止",
  idle: "等待任务",
};

const eventPresentation = {
  queued: ["TASK", "任务进入队列", "任务参数已经通过本地 API 校验。", "started"],
  started: ["BOOT", "Agent 开始执行", "正在准备模型、上下文和本地工具。", "started"],
  model_request: ["ROUND", "开始新一轮", "将当前上下文和可用工具发送给模型。", "round"],
  model_response: ["THOUGHT", "模型决策摘要", "模型已完成本轮决策。", "thought"],
  tool_start: ["ACTION", "执行工具动作", "Agent 请求在受限工作区执行操作。", "action"],
  tool_finish: ["OBSERVATION", "观察工具结果", "结果已写回对话上下文。", "observation"],
  model_error: ["MODEL ERROR", "模型请求失败", "模型请求未能完成。", "failure"],
  approval_required: ["REVIEW", "等待用户确认", "风险操作尚未执行。", "review"],
  approval_decision: ["REVIEW", "操作审批已处理", "Agent 将按照你的选择继续。", "review"],
  plan_updated: ["PLAN", "执行计划已更新", "复杂任务的计划状态已同步。", "thought"],
  completed: ["DONE", "模型宣布任务完成", "已生成最终回答并结束循环。", "success"],
  cancel_requested: ["STOP", "收到停止请求", "当前模型请求返回后将终止。", "failure"],
  cancelled: ["STOP", "任务已停止", "Agent 循环已安全终止。", "failure"],
  error: ["ERROR", "任务执行失败", "请查看错误详情并调整配置或 Prompt。", "failure"],
};

function pad(value) {
  return String(value).padStart(2, "0");
}

function formatElapsed(milliseconds) {
  const totalSeconds = Math.max(0, Math.floor(milliseconds / 1000));
  return `${pad(Math.floor(totalSeconds / 60))}:${pad(totalSeconds % 60)}`;
}

function formatClock(timestamp) {
  if (!timestamp) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(timestamp));
}

function prettyValue(value) {
  if (typeof value !== "string") return JSON.stringify(value, null, 2);
  try {
    return JSON.stringify(JSON.parse(value), null, 2);
  } catch {
    return value;
  }
}

function appendInlineMarkdown(parent, text) {
  const pattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*|\*[^*\n]+\*|\[[^\]\n]+\]\(https?:\/\/[^)\s]+\))/g;
  let cursor = 0;
  for (const match of String(text).matchAll(pattern)) {
    if (match.index > cursor) parent.append(document.createTextNode(text.slice(cursor, match.index)));
    const token = match[0];
    let node;
    if (token.startsWith("`")) {
      node = document.createElement("code");
      node.textContent = token.slice(1, -1);
    } else if (token.startsWith("**")) {
      node = document.createElement("strong");
      node.textContent = token.slice(2, -2);
    } else if (token.startsWith("*")) {
      node = document.createElement("em");
      node.textContent = token.slice(1, -1);
    } else {
      const link = token.match(/^\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)$/);
      node = document.createElement("a");
      node.textContent = link?.[1] || token;
      node.href = link?.[2] || "#";
      node.target = "_blank";
      node.rel = "noopener noreferrer";
    }
    parent.append(node);
    cursor = match.index + token.length;
  }
  if (cursor < text.length) parent.append(document.createTextNode(text.slice(cursor)));
}

function appendMarkdownLines(parent, tagName, lines) {
  const node = document.createElement(tagName);
  lines.forEach((line, index) => {
    if (index) node.append(document.createElement("br"));
    appendInlineMarkdown(node, line);
  });
  parent.append(node);
}

function renderMarkdown(target, source) {
  target.replaceChildren();
  const lines = String(source || "").replaceAll("\r\n", "\n").split("\n");
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }
    if (line.startsWith("```")) {
      const language = line.slice(3).trim();
      const codeLines = [];
      index += 1;
      while (index < lines.length && !lines[index].startsWith("```")) {
        codeLines.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      const pre = document.createElement("pre");
      const code = document.createElement("code");
      if (language) code.dataset.language = language;
      code.textContent = codeLines.join("\n");
      pre.append(code);
      target.append(pre);
      continue;
    }
    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    if (heading) {
      const node = document.createElement(`h${heading[1].length + 2}`);
      appendInlineMarkdown(node, heading[2]);
      target.append(node);
      index += 1;
      continue;
    }
    const unordered = line.match(/^[-*]\s+(.+)$/);
    const ordered = line.match(/^\d+[.)]\s+(.+)$/);
    if (unordered || ordered) {
      const list = document.createElement(unordered ? "ul" : "ol");
      const matcher = unordered ? /^[-*]\s+(.+)$/ : /^\d+[.)]\s+(.+)$/;
      while (index < lines.length) {
        const itemMatch = lines[index].match(matcher);
        if (!itemMatch) break;
        const item = document.createElement("li");
        appendInlineMarkdown(item, itemMatch[1]);
        list.append(item);
        index += 1;
      }
      target.append(list);
      continue;
    }
    if (line.startsWith(">")) {
      const quoteLines = [];
      while (index < lines.length && lines[index].startsWith(">")) {
        quoteLines.push(lines[index].replace(/^>\s?/, ""));
        index += 1;
      }
      appendMarkdownLines(target, "blockquote", quoteLines);
      continue;
    }
    const paragraph = [];
    while (index < lines.length && lines[index].trim()) {
      if (paragraph.length && /^(#{1,3})\s+|^```|^[-*]\s+|^\d+[.)]\s+|^>/.test(lines[index])) break;
      paragraph.push(lines[index]);
      index += 1;
    }
    appendMarkdownLines(target, "p", paragraph);
  }
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      ...(options.body ? { "Content-Type": "application/json", "X-Agent-Client": "web-ui" } : {}),
      ...(options.headers || {}),
    },
  });
  let body;
  try {
    body = await response.json();
  } catch {
    body = { error: `本地服务返回了无法解析的响应（HTTP ${response.status}）` };
  }
  if (!response.ok) throw new Error(body.error || `请求失败（HTTP ${response.status}）`);
  return body;
}

function setConnection(online, label) {
  elements.connectionDot.classList.toggle("online", online);
  elements.connectionDot.classList.toggle("offline", !online);
  elements.connectionLabel.textContent = label;
}

function setRunStatus(status) {
  elements.runStatus.className = `status-pill ${status}`;
  elements.statusLabel.textContent = statusLabels[status] || status;
}

function setRunning(running) {
  state.running = running;
  elements.runButton.disabled = running;
  elements.runButtonLabel.textContent = running
    ? "Agent 执行中"
    : state.sessionId
      ? "继续对话"
      : "开始会话";
  elements.stopButton.hidden = !running;
  elements.prompt.disabled = running;
  elements.imageInput.disabled = running;
  const sessionLocked = Boolean(state.sessionId);
  elements.workspace.disabled = running || sessionLocked;
  elements.demo.disabled = running || sessionLocked;
  elements.maxSteps.disabled = running || sessionLocked;
}

function resetTrace() {
  state.lastEventSeq = 0;
  state.roundElements.clear();
  elements.emptyState.hidden = true;
  elements.timeline.replaceChildren();
  elements.finalTest.hidden = true;
  elements.metricSteps.textContent = "00";
  elements.metricTools.textContent = "00";
  elements.metricTime.textContent = "00:00";
  elements.metricTokens.textContent = "00";
  renderApproval(null);
  renderPlan([], "");
  elements.selectedTurnLabel.textContent = "尚未选择对话轮次";
}

function resetConversationView() {
  elements.chatMessages.replaceChildren();
  const empty = document.createElement("p");
  empty.className = "chat-empty";
  empty.id = "chat-empty";
  empty.textContent = "新建会话后，你和 Agent 的每轮消息都会保存在本地。";
  elements.chatMessages.append(empty);
  elements.chatEmpty = empty;
  elements.turnCount.textContent = "0 TURNS";
  elements.contextPercent.textContent = "0%";
  elements.contextFill.style.width = "0%";
  elements.contextDetail.textContent = "尚未开始会话";
}

function renderConversation(session) {
  state.sessionId = session.id;
  window.localStorage.setItem("loopcoder.session", session.id);
  markActiveSession(session.id);
  elements.workspace.value = session.workspace;
  elements.demo.checked = Boolean(session.demo);
  elements.maxSteps.value = session.max_steps;
  elements.maxStepsValue.textContent = session.max_steps;

  elements.chatMessages.replaceChildren();
  const messages = session.messages || [];
  for (const message of messages) {
    const article = document.createElement("article");
    article.className = `chat-message ${message.role}`;
    article.dataset.runId = message.run_id || "";
    article.classList.toggle("selected", Boolean(message.run_id) && message.run_id === state.selectedRunId);
    if (message.run_id) {
      article.tabIndex = 0;
      article.setAttribute("role", "button");
      article.title = `查看第 ${message.turn || 0} 轮执行计划和轨迹`;
      article.addEventListener("click", () => selectTurn(message.run_id, message.turn));
      article.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          selectTurn(message.run_id, message.turn);
        }
      });
    }
    if (message.status === "error" || message.status === "cancelled") {
      article.classList.add("error");
    }
    const label = document.createElement("div");
    label.className = "chat-message-label";
    const role = message.role === "user" ? "YOU" : "AGENT";
    label.textContent = `${role}\nTURN ${pad(message.turn || 0)}\n${formatClock(message.created_at)}`;
    const content = document.createElement("div");
    content.className = "chat-message-content markdown-content";
    renderMarkdown(content, message.content || "");
    article.append(label, content);
    if (Array.isArray(message.attachments) && message.attachments.length) {
      const attachments = document.createElement("div");
      attachments.className = "chat-attachments";
      for (const path of message.attachments) {
        const badge = document.createElement("span");
        badge.textContent = `▧ ${String(path).split("/").at(-1)}`;
        badge.title = path;
        attachments.append(badge);
      }
      article.append(attachments);
    }
    elements.chatMessages.append(article);
  }
  if (!messages.length) {
    const empty = document.createElement("p");
    empty.className = "chat-empty";
    empty.textContent = "会话已建立，发送第一条 Prompt 后开始记忆。";
    elements.chatMessages.append(empty);
  }

  const context = session.context || {};
  const percent = context.percent || 0;
  elements.contextPercent.textContent = `${percent}%`;
  elements.contextFill.style.width = `${percent}%`;
  const dropped = context.dropped_exchanges || 0;
  elements.contextDetail.textContent = dropped
    ? `保留 ${context.retained_exchanges} 轮 · 已压缩 ${dropped} 轮`
    : `已记住 ${context.total_exchanges || 0} 轮 · 本地持久化`;
  const turnCount = session.turn_count || 0;
  elements.turnCount.textContent = `${turnCount} ${turnCount === 1 ? "TURN" : "TURNS"}`;
  elements.chatMessages.scrollTop = elements.chatMessages.scrollHeight;
  setRunning(state.running);
}

function latestRunId(session) {
  const messages = session.messages || [];
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index].run_id) return messages[index].run_id;
  }
  return null;
}

async function selectTurn(runId, turn) {
  if (!runId) return;
  try {
    if (state.running && runId !== state.runId) return;
    state.selectedRunId = runId;
    document.querySelectorAll(".chat-message").forEach((message) => {
      message.classList.toggle("selected", message.dataset.runId === runId);
    });
    resetTrace();
    elements.selectedTurnLabel.textContent = `正在载入第 ${turn || "—"} 轮的会话记忆与执行轨迹…`;
    const run = await api(`/api/runs/${runId}`);
    state.runId = runId;
    renderRun(run);
    elements.selectedTurnLabel.textContent = `当前展示：第 ${run.turn || turn || "—"} 轮 · ${runId.slice(0, 8).toUpperCase()}`;
  } catch (error) {
    elements.formMessage.textContent = error.message;
  }
}

async function refreshSession() {
  if (!state.sessionId) return null;
  const session = await api(`/api/sessions/${state.sessionId}`);
  renderConversation(session);
  return session;
}

async function loadSessionList() {
  const response = await api("/api/sessions");
  const previous = state.sessionId || window.localStorage.getItem("loopcoder.session") || "";
  const sessions = response.sessions || [];
  elements.sessionList.replaceChildren();
  elements.sessionCount.textContent = pad(sessions.length);
  if (!sessions.length) {
    const empty = document.createElement("p");
    empty.className = "session-list-empty";
    empty.textContent = "还没有本地会话";
    elements.sessionList.append(empty);
  }
  for (const session of sessions) {
    const item = document.createElement("div");
    item.className = "session-item";
    item.dataset.sessionId = session.id;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "session-open";
    button.title = session.workspace;

    const title = document.createElement("strong");
    title.textContent = session.title || "新会话";
    const workspace = document.createElement("span");
    const parts = String(session.workspace || "").split(/[\\/]/).filter(Boolean);
    workspace.textContent = `⌂ ${parts.at(-1) || session.workspace}`;
    const meta = document.createElement("small");
    meta.textContent = `${pad(session.turn_count || 0)} TURNS${session.active_run_id ? " · RUNNING" : ""}`;
    button.append(title, workspace, meta);
    button.addEventListener("click", () => selectSession(session.id));
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "session-delete";
    remove.textContent = "×";
    remove.setAttribute("aria-label", `删除会话：${session.title || "新会话"}`);
    remove.title = session.active_run_id ? "请先停止运行中的任务" : "删除整个会话";
    remove.disabled = Boolean(session.active_run_id);
    remove.addEventListener("click", () => deleteSession(session));
    item.append(button, remove);
    elements.sessionList.append(item);
  }
  if (previous && sessions.some((session) => session.id === previous)) {
    markActiveSession(previous);
    return previous;
  }
  return "";
}

function markActiveSession(sessionId) {
  document.querySelectorAll(".session-item").forEach((item) => {
    item.classList.toggle("active", item.dataset.sessionId === sessionId);
  });
}

async function deleteSession(session) {
  if (session.active_run_id) {
    elements.formMessage.textContent = "请先停止这个会话正在运行或等待确认的任务。";
    return;
  }
  const title = session.title || "新会话";
  const confirmed = window.confirm(
    `确定永久删除会话“${title}”吗？\n\n消息、上下文、执行轨迹和该会话上传的图片都会被删除，此操作无法撤销。`,
  );
  if (!confirmed) return;
  try {
    await api(`/api/sessions/${session.id}`, {
      method: "DELETE",
      body: JSON.stringify({}),
    });
    const wasSelected = state.sessionId === session.id;
    if (wasSelected) {
      stopPolling();
      state.sessionId = null;
      state.runId = null;
      state.selectedRunId = null;
      window.localStorage.removeItem("loopcoder.session");
      resetConversationView();
      resetTrace();
      setRunStatus("idle");
      setRunning(false);
    }
    const nextSession = await loadSessionList();
    if (wasSelected && nextSession) await selectSession(nextSession);
    elements.formMessage.textContent = `已删除会话“${title}”。`;
  } catch (error) {
    elements.formMessage.textContent = error.message;
  }
}

async function createSession() {
  const session = await api("/api/sessions", {
    method: "POST",
    body: JSON.stringify({
      workspace: elements.workspace.value.trim(),
      max_steps: Number(elements.maxSteps.value),
      demo: elements.demo.checked,
    }),
  });
  state.sessionId = session.id;
  await loadSessionList();
  renderConversation(session);
  return session;
}

function startFreshSession() {
  stopPolling();
  setRunning(false);
  state.sessionId = null;
  state.runId = null;
  state.selectedRunId = null;
  state.pendingImageFiles = [];
  renderPendingImages();
  window.localStorage.removeItem("loopcoder.session");
  markActiveSession("");
  resetConversationView();
  resetTrace();
  elements.emptyState.hidden = false;
  setRunStatus("idle");
  elements.runId.textContent = "RUN / NOT STARTED";
  setRunning(false);
  elements.prompt.focus();
}

async function selectSession(sessionId) {
  if (!sessionId || sessionId === state.sessionId) return;
  try {
    stopPolling();
    setRunning(false);
    state.sessionId = sessionId;
    state.runId = null;
    state.selectedRunId = null;
    resetTrace();
    elements.emptyState.hidden = false;
    setRunStatus("idle");
    const session = await refreshSession();
    const targetRunId = session.active_run_id || latestRunId(session);
    if (targetRunId) {
      state.runId = targetRunId;
      state.selectedRunId = targetRunId;
      const run = await api(`/api/runs/${targetRunId}`);
      state.startedAt = run.started_at ? new Date(run.started_at).getTime() : Date.now();
      state.clockTimer = window.setInterval(updateClock, 500);
      renderRun(run);
      if (isActiveStatus(run.status)) pollRun();
    }
  } catch (error) {
    elements.formMessage.textContent = error.message;
  }
}

function eventDetails(event) {
  const payload = event.payload || {};
  if (event.type === "model_request") {
    return { summary: `第 ${payload.step || "—"} 次模型决策`, details: null };
  }
  if (event.type === "model_response") {
    const count = payload.tool_call_count || 0;
    const timing = payload.duration_ms == null ? "" : ` · ${payload.duration_ms} ms`;
    return {
      summary: `${payload.thought || (count ? `模型选择了 ${count} 个工具动作。` : "模型返回最终文本。")}${timing}`,
      details: null,
    };
  }
  if (event.type === "tool_start") {
    return {
      summary: payload.name || "未知工具",
      details: prettyValue(payload.arguments || "{}"),
      detailLabel: "查看调用参数",
      defaultOpen: true,
      className: "action",
    };
  }
  if (event.type === "tool_finish") {
    const timing = payload.duration_ms == null ? "" : ` · ${payload.duration_ms} ms`;
    return {
      summary: `${payload.name || "工具"} · ${payload.success ? "执行成功" : "执行失败"}${timing}`,
      details: prettyValue(payload.result || ""),
      detailLabel: "查看工具结果",
      defaultOpen: true,
      className: payload.success ? "observation" : "failure",
    };
  }
  return { summary: payload.message || eventPresentation[event.type]?.[2] || "状态更新", details: null };
}

function isActiveStatus(status) {
  return status === "queued" || status === "running" || status === "waiting_approval";
}

function renderApproval(approval) {
  if (!approval) {
    state.approvalId = null;
    elements.approvalCard.hidden = true;
    elements.approvalDiffs.replaceChildren();
    return;
  }
  if (state.approvalId === approval.id) {
    elements.approvalCard.hidden = false;
    return;
  }
  state.approvalId = approval.id;
  const proposal = approval.proposal || {};
  const external = proposal.kind === "external";
  elements.approvalTool.textContent = String(proposal.tool || "ACTION").toUpperCase();
  elements.approvalTitle.textContent = proposal.title || (external ? "确认外部数据传输" : "确认本轮代码修改");
  elements.approvalDescription.textContent = proposal.summary || (
    external
      ? "此操作会把下列信息发送给外部模型服务，请确认后继续。"
      : "代码尚未写入工作区。请检查下面的红色删除行与绿色新增行。"
  );
  elements.approvalDiffs.replaceChildren();

  if (external) {
    const article = document.createElement("article");
    article.className = "approval-file external-review";
    const details = proposal.details || {};
    for (const [name, value] of Object.entries(details)) {
      const row = document.createElement("div");
      const key = document.createElement("strong");
      key.textContent = name.replaceAll("_", " ").toUpperCase();
      const content = document.createElement("span");
      content.textContent = typeof value === "string" ? value : JSON.stringify(value);
      row.append(key, content);
      article.append(row);
    }
    elements.approvalDiffs.append(article);
  }

  const files = proposal.files || [];
  for (const file of files) {
    const article = document.createElement("article");
    article.className = "approval-file";
    const header = document.createElement("header");
    const path = document.createElement("strong");
    path.textContent = file.path || "unknown file";
    const note = document.createElement("span");
    note.textContent = file.truncated ? "DIFF TRUNCATED" : "FULL DIFF";
    header.append(path, note);

    const pre = document.createElement("pre");
    for (const line of String(file.diff || "").split("\n")) {
      const row = document.createElement("span");
      if (line.startsWith("+") && !line.startsWith("+++")) row.className = "diff-add";
      else if (line.startsWith("-") && !line.startsWith("---")) row.className = "diff-delete";
      else if (line.startsWith("@@")) row.className = "diff-hunk";
      else row.className = "diff-context";
      row.textContent = line || " ";
      pre.append(row);
    }
    article.append(header, pre);
    elements.approvalDiffs.append(article);
  }
  elements.approveEdit.disabled = false;
  elements.rejectEdit.disabled = false;
  elements.approvalCard.hidden = false;
  elements.approvalCard.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function renderPlan(plan, explanation = "") {
  const items = Array.isArray(plan) ? plan : [];
  elements.planItems.replaceChildren();
  elements.runPlan.hidden = !items.length;
  if (!items.length) return;
  const completed = items.filter((item) => item.status === "completed").length;
  elements.planProgress.textContent = `${completed} / ${items.length}`;
  elements.planExplanation.textContent = explanation || "";
  elements.planExplanation.hidden = !explanation;
  for (const item of items) {
    const row = document.createElement("li");
    row.className = `plan-item ${item.status || "pending"}`;
    const mark = document.createElement("span");
    mark.textContent = item.status === "completed" ? "✓" : item.status === "in_progress" ? "→" : "○";
    const text = document.createElement("p");
    text.textContent = item.step || "";
    row.append(mark, text);
    elements.planItems.append(row);
  }
}

async function submitApproval(approved) {
  if (!state.runId || !state.approvalId) return;
  elements.approveEdit.disabled = true;
  elements.rejectEdit.disabled = true;
  try {
    const run = await api(
      `/api/runs/${state.runId}/approvals/${state.approvalId}`,
      { method: "POST", body: JSON.stringify({ approved }) },
    );
    renderRun(run);
    state.pollTimer = window.setTimeout(pollRun, 200);
  } catch (error) {
    elements.formMessage.textContent = error.message;
    elements.approveEdit.disabled = false;
    elements.rejectEdit.disabled = false;
  }
}

function ensureRound(step, timestamp) {
  const roundNumber = Number(step);
  if (state.roundElements.has(roundNumber)) {
    return state.roundElements.get(roundNumber);
  }

  const group = document.createElement("li");
  group.className = "round-group";
  group.dataset.round = String(roundNumber);

  const header = document.createElement("header");
  header.className = "round-header";
  const identity = document.createElement("div");
  identity.className = "round-identity";
  const badge = document.createElement("span");
  badge.className = "round-badge";
  badge.textContent = `ROUND ${pad(roundNumber)}`;
  const label = document.createElement("strong");
  label.textContent = "模型 → 工具 → 结果";
  identity.append(badge, label);

  const time = document.createElement("time");
  time.dateTime = timestamp || "";
  time.textContent = formatClock(timestamp);
  header.append(identity, time);

  const body = document.createElement("div");
  body.className = "round-events";
  group.append(header, body);
  elements.timeline.append(group);
  state.roundElements.set(roundNumber, body);
  return body;
}

function renderEvent(event) {
  const step = event.payload?.step;
  if (event.type === "model_request" && step) {
    ensureRound(step, event.timestamp);
    return;
  }

  const presentation = eventPresentation[event.type] || ["EVENT", event.type, "状态更新", "started"];
  const details = eventDetails(event);
  const isRoundEvent = Boolean(step) && ["model_response", "tool_start", "tool_finish"].includes(event.type);
  const item = document.createElement(isRoundEvent ? "article" : "li");
  item.className = `timeline-item ${details.className || presentation[3]}`;

  const header = document.createElement("div");
  header.className = "event-header";
  const title = document.createElement("div");
  title.className = "event-title";
  const index = document.createElement("span");
  index.className = "event-index";
  index.textContent = isRoundEvent ? presentation[0] : `${presentation[0]} / ${pad(event.seq)}`;
  const strong = document.createElement("strong");
  strong.textContent = presentation[1];
  const time = document.createElement("time");
  time.dateTime = event.timestamp;
  time.textContent = formatClock(event.timestamp);
  title.append(index, strong);
  header.append(title, time);

  const summary = document.createElement("p");
  summary.className = "event-summary";
  summary.textContent = details.summary;
  item.append(header, summary);

  if (details.details) {
    const disclosure = document.createElement("details");
    disclosure.className = "event-details";
    disclosure.open = Boolean(details.defaultOpen);
    const disclosureSummary = document.createElement("summary");
    disclosureSummary.textContent = details.detailLabel || "查看详情";
    const pre = document.createElement("pre");
    pre.textContent = details.details;
    disclosure.append(disclosureSummary, pre);
    item.append(disclosure);
  }
  const target = isRoundEvent ? ensureRound(step, event.timestamp) : elements.timeline;
  target.append(item);
}

function renderRun(run) {
  setRunStatus(run.status);
  elements.metricSteps.textContent = pad(run.steps || 0);
  elements.metricTools.textContent = pad(run.tool_calls || 0);
  const usage = run.model_usage || {};
  const tokens = usage.total_tokens || ((usage.prompt_tokens || 0) + (usage.completion_tokens || 0));
  elements.metricTokens.textContent = Number(tokens || 0).toLocaleString("zh-CN");
  elements.runId.textContent = `RUN / ${run.id.slice(0, 8).toUpperCase()}`;
  elements.selectedTurnLabel.textContent = `当前展示：第 ${run.turn || "—"} 轮 · ${run.id.slice(0, 8).toUpperCase()}`;
  renderPlan(run.plan, run.plan_explanation);
  renderApproval(run.pending_approval);

  const events = run.events || [];
  const unseenEvents = events.filter((event) => event.seq > state.lastEventSeq);
  for (const event of unseenEvents) renderEvent(event);
  if (events.length) state.lastEventSeq = events.at(-1).seq;

  if (events.length) {
    elements.emptyState.hidden = true;
    elements.traceViewport.scrollTo({ top: elements.traceViewport.scrollHeight, behavior: "smooth" });
  }

  if (run.final_test_result) {
    const test = run.final_test_result;
    elements.finalTest.classList.toggle("failed", !test.success);
    elements.finalTestStatus.textContent = test.success
      ? `PASSED · ${test.exit_code ?? 0}`
      : `FAILED · ${test.exit_code ?? "?"}`;
    elements.finalTestCommand.textContent = (test.command || []).join(" ");
    elements.finalTest.hidden = false;
  }

  if (!isActiveStatus(run.status) && run.duration_ms != null) {
    elements.metricTime.textContent = formatElapsed(run.duration_ms);
  }

  const active = isActiveStatus(run.status);
  const wasRunning = state.running;
  setRunning(active);
  if (!active) {
    stopPolling();
    if (wasRunning) {
      refreshSession().catch((error) => {
        elements.formMessage.textContent = error.message;
      });
      loadSessionList().catch(() => {});
    }
  }
}

function updateClock() {
  if (!state.startedAt) return;
  elements.metricTime.textContent = formatElapsed(Date.now() - state.startedAt);
}

function stopPolling() {
  if (state.pollTimer) window.clearTimeout(state.pollTimer);
  if (state.clockTimer) window.clearInterval(state.clockTimer);
  state.pollTimer = null;
  state.clockTimer = null;
}

async function pollRun() {
  if (!state.runId) return;
  try {
    const run = await api(`/api/runs/${state.runId}`);
    renderRun(run);
    if (isActiveStatus(run.status)) {
      state.pollTimer = window.setTimeout(pollRun, 650);
    }
  } catch (error) {
    elements.formMessage.textContent = error.message;
    setRunning(false);
    stopPolling();
  }
}

function renderPendingImages() {
  elements.pendingImages.replaceChildren();
  for (const [index, file] of state.pendingImageFiles.entries()) {
    const chip = document.createElement("span");
    chip.className = "pending-image";
    const name = document.createElement("b");
    name.textContent = `▧ ${file.name}`;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "×";
    remove.title = "移除图片";
    remove.addEventListener("click", () => {
      state.pendingImageFiles.splice(index, 1);
      renderPendingImages();
    });
    chip.append(name, remove);
    elements.pendingImages.append(chip);
  }
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || "");
    reader.onerror = () => reject(new Error(`无法读取图片：${file.name}`));
    reader.readAsDataURL(file);
  });
}

async function uploadPendingImages() {
  const attachments = [];
  for (const file of state.pendingImageFiles) {
    const data = await fileToBase64(file);
    const uploaded = await api(`/api/sessions/${state.sessionId}/images`, {
      method: "POST",
      body: JSON.stringify({ filename: file.name, data_base64: data }),
    });
    attachments.push(uploaded.path);
  }
  return attachments;
}

async function startRun(event) {
  event.preventDefault();
  elements.formMessage.textContent = "";
  const task = elements.prompt.value.trim();
  if (!task) {
    elements.formMessage.textContent = "请输入一个编程任务 Prompt。";
    elements.prompt.focus();
    return;
  }

  resetTrace();
  setRunning(true);
  setRunStatus("queued");
  try {
    if (!state.sessionId) await createSession();
    const attachments = await uploadPendingImages();
    const run = await api(`/api/sessions/${state.sessionId}/messages`, {
      method: "POST",
      body: JSON.stringify({
        content: task,
        attachments,
      }),
    });
    state.runId = run.id;
    state.selectedRunId = run.id;
    state.startedAt = Date.now();
    state.pendingImageFiles = [];
    renderPendingImages();
    elements.prompt.value = "";
    elements.prompt.dispatchEvent(new Event("input"));
    state.clockTimer = window.setInterval(updateClock, 500);
    await refreshSession();
    renderRun(run);
    pollRun();
  } catch (error) {
    elements.formMessage.textContent = error.message;
    setRunStatus("error");
    setRunning(false);
  }
}

async function cancelRun() {
  if (!state.runId) return;
  elements.stopButton.disabled = true;
  try {
    const run = await api(`/api/runs/${state.runId}/cancel`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    renderRun(run);
  } catch (error) {
    elements.formMessage.textContent = error.message;
  } finally {
    elements.stopButton.disabled = false;
  }
}

async function initialize() {
  try {
    const config = await api("/api/config");
    state.config = config;
    setConnection(true, config.api_configured ? "本地服务在线 · API 已配置" : "本地服务在线 · API 未配置");
    elements.modelName.textContent = config.model;
    elements.workspace.value = config.default_workspace;
    elements.maxSteps.value = config.max_steps;
    elements.maxStepsValue.textContent = config.max_steps;
    elements.visionState.textContent = config.vision_configured
      ? `图片理解：${config.vision_model}`
      : "图片理解：尚未配置 QWEN_API_KEY";
    elements.visionState.classList.toggle("configured", Boolean(config.vision_configured));
    if (!config.api_configured) elements.demo.checked = true;
    const recentSession = await loadSessionList();
    if (recentSession) {
      state.sessionId = recentSession;
      const session = await refreshSession();
      const targetRunId = session?.active_run_id || latestRunId(session || {});
      if (targetRunId) {
        state.runId = targetRunId;
        state.selectedRunId = targetRunId;
        resetTrace();
        const run = await api(`/api/runs/${targetRunId}`);
        state.startedAt = run.started_at ? new Date(run.started_at).getTime() : Date.now();
        state.clockTimer = window.setInterval(updateClock, 500);
        renderRun(run);
        if (isActiveStatus(run.status)) pollRun();
      }
    } else {
      resetConversationView();
      setRunning(false);
    }
  } catch (error) {
    setConnection(false, "无法连接本地服务");
    elements.formMessage.textContent = error.message;
    elements.runButton.disabled = true;
  }
}

elements.form.addEventListener("submit", startRun);
elements.stopButton.addEventListener("click", cancelRun);
elements.newSession.addEventListener("click", startFreshSession);
elements.approveEdit.addEventListener("click", () => submitApproval(true));
elements.rejectEdit.addEventListener("click", () => submitApproval(false));
elements.prompt.addEventListener("input", () => {
  elements.promptCount.textContent = `${elements.prompt.value.length.toLocaleString()} / 20,000`;
});
elements.prompt.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
    event.preventDefault();
    elements.form.requestSubmit();
  }
});
elements.imageInput.addEventListener("change", () => {
  const selected = Array.from(elements.imageInput.files || []);
  const allowed = new Set(["image/png", "image/jpeg", "image/webp", "image/gif"]);
  for (const file of selected) {
    if (!allowed.has(file.type)) {
      elements.formMessage.textContent = `${file.name} 不是支持的 PNG/JPEG/WebP/GIF 图片。`;
      continue;
    }
    if (file.size > 10_000_000) {
      elements.formMessage.textContent = `${file.name} 超过 10 MB。`;
      continue;
    }
    if (state.pendingImageFiles.length >= 5) {
      elements.formMessage.textContent = "每轮最多添加 5 张图片。";
      break;
    }
    state.pendingImageFiles.push(file);
  }
  elements.imageInput.value = "";
  renderPendingImages();
});
elements.maxSteps.addEventListener("input", () => {
  elements.maxStepsValue.textContent = elements.maxSteps.value;
});
document.querySelectorAll(".preset").forEach((button) => {
  button.addEventListener("click", () => {
    elements.prompt.value = button.dataset.prompt || "";
    elements.prompt.dispatchEvent(new Event("input"));
    elements.prompt.focus();
  });
});
initialize();
