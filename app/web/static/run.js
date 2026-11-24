const runId = document.getElementById("run").dataset.runId;
const OUTCOME_TITLES = {
  pr_ok: "Pull Request создан и проверен",
  pr_needs_work: "Pull Request создан, но требует внимания",
  rejected: "Исправление отклонено",
  gave_up: "Не удалось подготовить исправление",
  not_reproduced: "Ошибку не удалось воспроизвести",
  error: "Запуск завершился ошибкой",
};
let timer = null;
let deciding = false;

function renderDiff(target, diff) {
  target.replaceChildren();
  for (const line of diff.split("\n")) {
    let cls = "";
    if (line.startsWith("+") && !line.startsWith("+++")) cls = "add";
    else if (line.startsWith("-") && !line.startsWith("---")) cls = "del";
    else if (line.startsWith("@@")) cls = "hunk";
    target.append(el("span", cls ? { class: cls } : {}, line || " "), document.createTextNode("\n"));
  }
}

const CONFIDENCE = { high: "уверенность: высокая", medium: "уверенность: средняя", low: "уверенность: низкая" };

function frameLabel(f) {
  if (!f) return "не указано в тексте ошибки";
  return `${f.path}:${f.line}` + (f.function ? ` в ${f.function}` : "");
}

function renderDiagnosis(d) {
  const box = document.getElementById("diagnosis");
  if (!d) {
    box.hidden = true;
    return;
  }
  const { facts, hypothesis: h } = d;
  box.hidden = false;
  const conf = document.getElementById("dg-confidence");
  conf.textContent = CONFIDENCE[h.confidence] || h.confidence;
  conf.className = `badge confidence-${h.confidence}`;
  document.getElementById("dg-where").textContent = frameLabel(facts.raise_site);
  const err = [facts.error_type, facts.error_message].filter(Boolean).join(": ");
  document.getElementById("dg-error").textContent = err || "тип ошибки в тексте не найден";
  document.getElementById("dg-summary").textContent = h.summary || "—";
  document.getElementById("dg-cause").textContent = h.cause;
  const steps = document.getElementById("dg-steps");
  steps.replaceChildren(...(h.fix_steps.length ? h.fix_steps : ["—"]).map((s) => el("li", {}, s)));
  const code = h.code_fix || {};
  const hasCode = Boolean(code.before || code.after);
  document.getElementById("dg-code").hidden = !hasCode;
  if (hasCode) {
    const lines = [
      ...(code.before ? code.before.split("\n").map((l) => `- ${l}`) : []),
      ...(code.after ? code.after.split("\n").map((l) => `+ ${l}`) : []),
    ];
    renderDiff(document.getElementById("dg-diff"), lines.join("\n"));
  }
  document.getElementById("dg-verify").textContent = h.how_to_verify || "—";
  const frames = document.getElementById("dg-frames");
  frames.replaceChildren(...(facts.frames.length ? facts.frames.map((f) => el("li", {}, frameLabel(f))) : [el("li", {}, "в тексте нет стек-трейса")]));
  const more = h.need_more_info || "";
  document.getElementById("dg-more").textContent = more;
  document.getElementById("dg-more").hidden = !more;
  document.getElementById("dg-more-label").hidden = !more;
}

function render(run) {
  document.getElementById("run-title").textContent = run.repo || "Диагноз без репозитория";
  document.querySelector('[data-testid="approve"]').textContent = (run.repo || "").startsWith("local:")
    ? "Одобрить и создать ветку"
    : "Одобрить и создать PR";
  const badge = document.getElementById("run-status");
  badge.textContent = STATUS_LABELS[run.status] || run.status;
  badge.className = `badge ${run.status}`;
  const source = run.source === "issue" ? `GitHub Issue #${run.issue_number}` : "форма";
  const channel = run.channel === "telegram" ? "Telegram" : "сайт";
  const mode = run.repo ? `одобрение: ${channel}` : "режим: диагноз по тексту, без доступа к коду";
  document.getElementById("run-meta").textContent = `Источник: ${source} · ${mode} · токенов LLM: ${run.tokens}`;

  const last = run.events[run.events.length - 1];
  const live = document.getElementById("run-live");
  live.textContent = run.status === "running" || run.status === "queued" ? `${last ? last.message : "Запуск…"}` : "";

  const list = document.getElementById("events");
  list.replaceChildren();
  for (const ev of run.events) {
    const li = el("li", { class: ev.level });
    li.append(el("span", { class: "ts" }, fmtTime(ev.ts)), document.createTextNode(ev.message));
    list.append(li);
  }

  const approval = document.getElementById("approval");
  if (run.approval && run.status === "awaiting_approval") {
    const p = run.approval;
    approval.hidden = false;
    document.getElementById("ap-file").textContent = p.file_path;
    document.getElementById("ap-cause").textContent = p.root_cause;
    document.getElementById("ap-expl").textContent = p.explanation;
    document.getElementById("ap-repro").textContent = p.reproduction || "—";
    document.getElementById("ap-ci").textContent = `${p.ci_label} (попытка ${p.attempt})`;
    document.getElementById("ap-verdict").textContent =
      p.verdict === "APPROVE" ? "ревьюер-модель согласна" : "ревьюер-модель сомневается";
    document.getElementById("ap-security").textContent = p.security.length ? p.security.join("\n") : "замечаний нет";
    const compare = document.getElementById("ap-compare");
    compare.hidden = !p.compare_url;
    if (p.compare_url) compare.href = p.compare_url;
    renderDiff(document.getElementById("ap-diff"), p.diff);
    const web = run.channel === "web";
    document.getElementById("ap-actions").hidden = !web;
    document.getElementById("ap-telegram").hidden = web;
  } else {
    approval.hidden = true;
  }

  const result = document.getElementById("result");
  if (run.status === "completed" || run.status === "failed") {
    result.hidden = false;
    document.getElementById("res-title").textContent =
      (run.result && run.result.title) || OUTCOME_TITLES[run.outcome] || "Запуск завершён";
    const resMessage = document.getElementById("res-message");
    resMessage.textContent = (run.result && run.result.message) || run.error || "";
    resMessage.hidden = Boolean(run.result && run.result.diagnosis); // диагноз показан отдельной карточкой
    const pr = document.getElementById("res-pr");
    if (run.pr_url && run.pr_url.startsWith("local:")) {
      pr.hidden = true; // локальная ветка: команда применения — в тексте результата
    } else if (run.pr_url) {
      pr.href = run.pr_url;
      pr.hidden = false;
    }
    renderDiagnosis(run.result && run.result.diagnosis);
    clearInterval(timer);
  }
}

async function refresh() {
  try {
    render(await api(`/api/v1/runs/${runId}`));
  } catch (err) {
    document.getElementById("run-live").textContent = `Не удалось загрузить: ${err.message}`;
  }
}

document.getElementById("ap-actions").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-decision]");
  if (!button || deciding) return;
  deciding = true;
  document.querySelectorAll("#ap-actions button").forEach((b) => (b.disabled = true));
  try {
    await api(`/api/v1/runs/${runId}/decision`, {
      method: "POST",
      body: JSON.stringify({ approved: button.dataset.decision === "true" }),
    });
    await refresh();
  } catch (err) {
    alert(err.message);
    document.querySelectorAll("#ap-actions button").forEach((b) => (b.disabled = false));
  } finally {
    deciding = false;
  }
});

refresh();
timer = setInterval(refresh, 2000);
