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
  emptyState: document.querySelector("#empty-state"),
  timeline: document.querySelector("#timeline"),
  traceViewport: document.querySelector("#trace-viewport"),
  resultCard: document.querySelector("#result-card"),
  resultOutput: document.querySelector("#result-output"),
  copyResult: document.querySelector("#copy-result"),
  runId: document.querySelector("#run-id"),
};

const state = {
  runId: null,
  startedAt: null,
  pollTimer: null,
  clockTimer: null,
  lastEventSeq: 0,
  running: false,
};

const statusLabels = {
  queued: "准备任务",
  running: "运行中",
  completed: "已完成",
  error: "运行错误",
  cancelled: "已停止",
  idle: "等待任务",
};

const eventPresentation = {
  queued: ["TASK", "任务进入队列", "任务参数已经通过本地 API 校验。", "started"],
  started: ["BOOT", "Agent 开始执行", "正在准备模型、上下文和本地工具。", "started"],
  model_request: ["MODEL", "请求模型决策", "将当前上下文和可用工具发送给模型。", "model"],
  model_response: ["MODEL", "模型返回响应", "模型已完成本轮分析。", "model"],
  tool_start: ["TOOL", "调用本地工具", "Agent 请求在受限工作区执行操作。", "tool"],
  tool_finish: ["RETURN", "工具返回结果", "结果已写回对话上下文。", "success"],
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
  elements.runButtonLabel.textContent = running ? "Agent 执行中" : "启动 Agent";
  elements.stopButton.hidden = !running;
  elements.workspace.disabled = running;
  elements.prompt.disabled = running;
  elements.demo.disabled = running;
  elements.maxSteps.disabled = running;
}

function resetTrace() {
  state.lastEventSeq = 0;
  elements.emptyState.hidden = true;
  elements.timeline.replaceChildren();
  elements.resultCard.hidden = true;
  elements.resultOutput.textContent = "";
  elements.metricSteps.textContent = "00";
  elements.metricTools.textContent = "00";
  elements.metricTime.textContent = "00:00";
}

function eventDetails(event) {
  const payload = event.payload || {};
  if (event.type === "model_request") {
    return { summary: `第 ${payload.step || "—"} 次模型决策`, details: null };
  }
  if (event.type === "model_response") {
    const count = payload.tool_call_count || 0;
    return {
      summary: count ? `模型选择了 ${count} 个工具动作。` : "模型返回最终文本。",
      details: null,
    };
  }
  if (event.type === "tool_start") {
    return {
      summary: payload.name || "未知工具",
      details: prettyValue(payload.arguments || "{}"),
      detailLabel: "查看调用参数",
    };
  }
  if (event.type === "tool_finish") {
    return {
      summary: `${payload.name || "工具"} · ${payload.success ? "执行成功" : "执行失败"}`,
      details: prettyValue(payload.result || ""),
      detailLabel: "查看工具结果",
      className: payload.success ? "success" : "failure",
    };
  }
  return { summary: payload.message || eventPresentation[event.type]?.[2] || "状态更新", details: null };
}

function renderEvent(event) {
  const presentation = eventPresentation[event.type] || ["EVENT", event.type, "状态更新", "started"];
  const details = eventDetails(event);
  const item = document.createElement("li");
  item.className = `timeline-item ${details.className || presentation[3]}`;

  const header = document.createElement("div");
  header.className = "event-header";
  const title = document.createElement("div");
  title.className = "event-title";
  const index = document.createElement("span");
  index.className = "event-index";
  index.textContent = `${presentation[0]} / ${pad(event.seq)}`;
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
    const disclosureSummary = document.createElement("summary");
    disclosureSummary.textContent = details.detailLabel || "查看详情";
    const pre = document.createElement("pre");
    pre.textContent = details.details;
    disclosure.append(disclosureSummary, pre);
    item.append(disclosure);
  }
  elements.timeline.append(item);
}

function renderRun(run) {
  setRunStatus(run.status);
  elements.metricSteps.textContent = pad(run.steps || 0);
  elements.metricTools.textContent = pad(run.tool_calls || 0);
  elements.runId.textContent = `RUN / ${run.id.slice(0, 8).toUpperCase()}`;

  const events = run.events || [];
  const unseenEvents = events.filter((event) => event.seq > state.lastEventSeq);
  for (const event of unseenEvents) renderEvent(event);
  if (events.length) state.lastEventSeq = events.at(-1).seq;

  if (events.length) {
    elements.emptyState.hidden = true;
    elements.traceViewport.scrollTo({ top: elements.traceViewport.scrollHeight, behavior: "smooth" });
  }

  if (run.final_output) {
    elements.resultOutput.textContent = run.final_output;
    elements.resultCard.hidden = false;
  }
  if (run.error && !run.final_output) {
    elements.resultOutput.textContent = run.error;
    elements.resultCard.hidden = false;
  }

  const active = run.status === "queued" || run.status === "running";
  setRunning(active);
  if (!active) stopPolling();
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
    if (run.status === "queued" || run.status === "running") {
      state.pollTimer = window.setTimeout(pollRun, 650);
    }
  } catch (error) {
    elements.formMessage.textContent = error.message;
    setRunning(false);
    stopPolling();
  }
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
    const run = await api("/api/runs", {
      method: "POST",
      body: JSON.stringify({
        task,
        workspace: elements.workspace.value.trim(),
        max_steps: Number(elements.maxSteps.value),
        demo: elements.demo.checked,
      }),
    });
    state.runId = run.id;
    state.startedAt = Date.now();
    state.clockTimer = window.setInterval(updateClock, 500);
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
    setConnection(true, config.api_configured ? "本地服务在线 · API 已配置" : "本地服务在线 · API 未配置");
    elements.modelName.textContent = config.model;
    elements.workspace.value = config.default_workspace;
    elements.maxSteps.value = config.max_steps;
    elements.maxStepsValue.textContent = config.max_steps;
    if (!config.api_configured) elements.demo.checked = true;
  } catch (error) {
    setConnection(false, "无法连接本地服务");
    elements.formMessage.textContent = error.message;
    elements.runButton.disabled = true;
  }
}

elements.form.addEventListener("submit", startRun);
elements.stopButton.addEventListener("click", cancelRun);
elements.prompt.addEventListener("input", () => {
  elements.promptCount.textContent = `${elements.prompt.value.length.toLocaleString()} / 20,000`;
});
elements.prompt.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
    event.preventDefault();
    elements.form.requestSubmit();
  }
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
elements.copyResult.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(elements.resultOutput.textContent || "");
    elements.copyResult.textContent = "已复制";
    window.setTimeout(() => { elements.copyResult.textContent = "复制结果"; }, 1300);
  } catch {
    elements.copyResult.textContent = "复制失败";
  }
});

initialize();
