const form = document.getElementById("run-form");
const formError = document.getElementById("form-error");

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  formError.hidden = true;
  const button = form.querySelector("button");
  button.disabled = true;
  const data = Object.fromEntries(new FormData(form));
  if (!data.telegram_chat_id) delete data.telegram_chat_id;
  if (!data.repo) delete data.repo;
  try {
    const run = await api("/api/v1/runs", { method: "POST", body: JSON.stringify(data) });
    window.location.href = run.url;
  } catch (err) {
    formError.textContent = err.message;
    formError.hidden = false;
    button.disabled = false;
  }
});

async function loadRuns() {
  const box = document.getElementById("runs");
  try {
    const runs = await api("/api/v1/runs?limit=20");
    box.replaceChildren();
    if (!runs.length) {
      box.append(el("p", { class: "muted" }, "Запусков пока нет."));
      return;
    }
    for (const run of runs) {
      const link = el("a", { class: "run", href: `/runs/${run.id}` });
      const left = el("span");
      left.append(el("strong", {}, run.repo || "диагноз без репозитория"));
      const src = run.source === "issue" ? ` · issue #${run.issue_number}` : "";
      left.append(el("span", { class: "muted" }, ` · ${new Date(run.created_at).toLocaleString("ru-RU")}${src}`));
      link.append(left, el("span", { class: `badge ${run.status}` }, STATUS_LABELS[run.status] || run.status));
      box.append(link);
    }
  } catch (err) {
    box.replaceChildren(el("p", { class: "error" }, `Не удалось загрузить запуски: ${err.message}`));
  }
}

async function loadStatus() {
  try {
    const s = await api("/api/v1/status");
    const missing = [];
    if (!s.groq) missing.push("GROQ_API_KEY — агент не сможет работать");
    if (!s.github) missing.push("GITHUB_TOKEN — нет доступа к репозиториям");
    const box = document.getElementById("config-warning");
    box.replaceChildren(el("strong", {}, "Не заданы ключи в .env:"));
    const list = el("ul");
    missing.forEach((m) => list.append(el("li", {}, m)));
    box.append(list);
    box.hidden = missing.length === 0;
    const info = [`Модели: ${s.models.main} / ${s.models.fast}`];
    info.push(s.telegram ? "Telegram подключён" : "Telegram не подключён — одобрение только на сайте");
    if (s.telegram_bot) {
      const hint = document.getElementById("tg-hint");
      hint.replaceChildren(document.createTextNode("Чтобы получать запросы в Telegram, один раз напишите "));
      hint.append(el("a", { href: `https://t.me/${s.telegram_bot}`, target: "_blank", rel: "noopener" }, `@${s.telegram_bot}`));
      hint.append(document.createTextNode(" команду /start и укажите здесь свой @username."));
      hint.hidden = false;
    }
    if (s.local_projects && s.local_projects.length) {
      info.push(`Локальные проекты: ${s.local_projects.map((n) => "local:" + n).join(", ")}`);
    }
    if (s.watched_repos.length) info.push(`Слежу за issue: ${s.watched_repos.join(", ")}`);
    info.push(s.sandbox ? "Песочница для тестов включена" : "Песочница выключена — проверка только через GitHub Actions");
    if (s.langsmith) info.push("LangSmith-трейсинг включён");
    document.getElementById("service-info").textContent = info.join(" · ");
  } catch (err) {
    /* статус необязателен для работы формы */
  }
}

loadStatus();
loadRuns();
setInterval(loadRuns, 5000);
