// Общие хелперы. Весь текст вставляется через textContent — HTML от агента никогда не рендерится.
const STATUS_LABELS = {
  queued: "в очереди",
  running: "агент работает",
  awaiting_approval: "ждёт решения",
  completed: "завершён",
  failed: "ошибка",
};

function el(tag, attrs = {}, text = "") {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (text) node.textContent = text;
  return node;
}

async function api(path, options = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const detail = Array.isArray(data.detail)
      ? data.detail.map((d) => d.msg).join("; ")
      : data.detail || resp.statusText;
    throw new Error(detail);
  }
  return data;
}

function fmtTime(iso) {
  return new Date(iso).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
