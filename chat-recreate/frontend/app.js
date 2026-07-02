const state = {
  cases: [],
  products: [],
  activeTab: "source",
  case: null,
  hero: null,
  product: null,
  candidates: [],
  selectedIds: new Set(),
  messages: [],
  plan: null,
  runId: null,
  sessionId: null,
  revision: 0,
  status: "draft",
  videoUrl: null,
  generationError: null,
  readOnly: false,
  history: [],
  filter: "all",
  editingId: null,
  inferring: false,
  inferStartedAt: null,
  inferStatus: "",
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const categoryNames = { scene: "场景", style: "风格", character: "人物", audio: "声音", text: "文案" };
let staticDemo = location.hostname.endsWith(".github.io") || location.protocol === "file:";
let syncQueue = Promise.resolve();
let generationPollTimer = null;

const STATIC_PRODUCTS = [
  {
    id: "ice-tea",
    label: "冰红茶",
    kind: "product",
    local_url: "assets/products/ice-tea.jpg",
    public_url: "https://down-tw.img.susercontent.com/file/tw-11134201-7ra0g-md4f43a5mw5c8c",
    description: "透明瓶身、琥珀色茶汤与清晰正面标签",
  },
  {
    id: "foundation-2an",
    label: "2aN 粉底液",
    kind: "product",
    local_url: "assets/products/foundation-2an.jpg",
    public_url: "https://down-tw.img.susercontent.com/file/tw-11134207-81zth-me82i45o701x67",
    description: "磨砂方瓶、白色泵头与裸色瓶身",
  },
  {
    id: "smart-band",
    label: "智能手环",
    kind: "product",
    local_url: "assets/products/smart-band.png",
    public_url: "https://s41.ax1x.com/2026/06/30/pmduYJe.png",
    description: "黑色腕带、方形 AMOLED 屏幕与运动数据 UI",
  },
  {
    id: "chocolate",
    label: "巧克力",
    kind: "product",
    local_url: "assets/products/chocolate.jpg",
    public_url: "https://down-tw.img.susercontent.com/file/tw-11134207-7rbke-m9tqod4n08uq56",
    description: "多口味独立包装巧克力条，适合食品、零食、拆封和质感特写广告",
  },
];

function publicPath(value = "") {
  const text = String(value || "");
  if (!text || /^(https?:|data:|blob:)/.test(text)) return text;
  return text.startsWith("/") ? text.slice(1) : text;
}

function normalizeCase(item) {
  return { ...item, sourceVideo: publicPath(item.sourceVideo) };
}

function normalizeProduct(item) {
  return { ...item, local_url: publicPath(item.local_url), public_url: item.public_url || item.local_url };
}

function persist() {
  if (staticDemo || !state.sessionId || state.readOnly) return syncQueue;
  syncQueue = syncQueue.then(async () => {
    const saved = await api(`/api/sessions/${encodeURIComponent(state.sessionId)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        expected_revision: state.revision,
        product: state.product,
        candidates: state.candidates,
        selected_ids: [...state.selectedIds],
        messages: state.messages.filter(message => !message.transient),
        plan: state.plan,
        run_id: state.runId,
        video_url: state.videoUrl,
        status: state.status,
      }),
    });
    state.revision = saved.revision;
  }).catch(error => {
    toast(`会话保存失败：${error.message}`);
  });
  return syncQueue;
}

async function flushSession() {
  await syncQueue;
}

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => node.classList.remove("show"), 2400);
}

function escapeHtml(value = "") {
  return String(value).replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
}

function makeId() {
  if (window.crypto?.randomUUID) return window.crypto.randomUUID();
  const random = window.crypto?.getRandomValues
    ? [...window.crypto.getRandomValues(new Uint32Array(2))].map(value => value.toString(16)).join("")
    : Math.random().toString(16).slice(2);
  return `id_${Date.now().toString(36)}_${random}`;
}

async function copyText(value) {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(value);
      return true;
    } catch {
      // Fall through to the legacy copy path for LAN HTTP pages.
    }
  }
  const field = document.createElement("textarea");
  field.value = value;
  field.setAttribute("readonly", "");
  field.style.position = "fixed";
  field.style.top = "-1000px";
  field.style.left = "-1000px";
  document.body.appendChild(field);
  field.select();
  try {
    return document.execCommand("copy");
  } finally {
    field.remove();
  }
}

function generationErrorMessage(error) {
  if (!error) return "未知错误";
  if (typeof error === "string") return error;
  return error.message || error.error || error.detail || JSON.stringify(error);
}

function resultVideoComparison(videoUrl, autoplay = false) {
  return `
    <div class="video-compare" data-video-compare>
      <section>
        <label>原视频</label>
        <video src="${escapeHtml(publicPath(state.case.sourceVideo))}" controls preload="metadata" playsinline></video>
      </section>
      <section>
        <label>复刻视频</label>
        <video src="${escapeHtml(publicPath(videoUrl))}" controls preload="metadata" ${autoplay ? "autoplay" : ""} playsinline></video>
      </section>
    </div>`;
}

function bindVideoComparison(root = document) {
  root.querySelectorAll("[data-video-compare]").forEach(group => {
    const videos = group.querySelectorAll("video");
    if (videos.length < 2) return;
    const pair = [videos[0], videos[1]];
    const syncing = [false, false];
    const sync = (target, action) => {
      const index = pair.indexOf(target);
      if (syncing[index]) return;
      syncing[index] = true;
      if (action === "play" && target.paused) {
        const playback = target.play();
        if (playback && typeof playback.catch === "function") playback.catch(() => {});
      } else if (action === "pause" && !target.paused) {
        target.pause();
      }
      setTimeout(() => { syncing[0] = false; syncing[1] = false; }, 0);
    };
    pair.forEach(video => {
      video.addEventListener("play", () => sync(pair[0] === video ? pair[1] : pair[0], "play"));
      video.addEventListener("pause", () => sync(pair[0] === video ? pair[1] : pair[0], "pause"));
    });
  });
}

async function api(path, options = {}, timeoutMs = 60000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(path, { ...options, signal: controller.signal });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = typeof payload.detail === "string" ? payload.detail : payload.detail?.message || JSON.stringify(payload.detail || payload);
      throw new Error(detail || `${response.status} ${response.statusText}`);
    }
    return payload;
  } catch (error) {
    if (error.name === "AbortError") throw new Error(`请求超过 ${Math.round(timeoutMs / 1000)} 秒，已自动停止。`);
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

function setSessionUrl(sessionId = null, view = null) {
  const url = new URL(window.location.href);
  if (sessionId) url.searchParams.set("session", sessionId);
  else url.searchParams.delete("session");
  if (view) url.searchParams.set("view", view);
  else url.searchParams.delete("view");
  history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`);
}

async function switchTab(tab) {
  state.activeTab = tab;
  $$(".tab").forEach(button => button.classList.toggle("active", button.dataset.tab === tab));
  $$(".history-link").forEach(button => button.classList.toggle("active", tab === "history"));
  $$(".panel").forEach(panel => panel.classList.toggle("active", panel.dataset.panel === tab));
  if (tab === "chat") renderChat();
  if (tab === "result") renderResult();
  if (tab === "history") await renderHistory();
  persist();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function renderSources() {
  const visible = state.cases.filter(item => state.filter === "all" || item.sourceAspect === state.filter);
  $("#sourceGrid").innerHTML = visible.map((item, index) => `
    <button class="video-card ${state.case?.id === item.id ? "selected" : ""}" type="button" data-case="${escapeHtml(item.id)}">
      <video src="${publicPath(item.sourceVideo)}#t=0.1" preload="metadata" muted playsinline></video>
      <div class="card-top"><span class="card-chip">${escapeHtml(item.tag)}</span><span class="card-duration">${item.sourceDuration}s · ${item.sourceAspect}</span></div>
      <div class="card-body">
        <strong>${escapeHtml(item.title)}</strong><span>${escapeHtml(item.subtitle)}</span><i class="card-arrow">↗</i>
      </div>
    </button>
  `).join("");
  $$(".video-card").forEach(card => {
    const video = $("video", card);
    card.addEventListener("mouseenter", () => video.play().catch(() => {}));
    card.addEventListener("mouseleave", () => { video.pause(); video.currentTime = 0.1; });
    card.addEventListener("click", () => selectCase(card.dataset.case));
  });
}

function staticIntentCandidates() {
  const caseStyle = state.case?.style || "原片广告语法";
  const productLabel = state.product?.label || "新商品";
  return [
    {
      id: "demo-product-fit",
      category: "scene",
      label: "商品替换",
      description: `把核心商品替换为${productLabel}，保留原片的展示窗口、镜头距离和节奏重心。`,
      default_value: true,
    },
    {
      id: "demo-style-keep",
      category: "style",
      label: "风格延续",
      description: `延续${caseStyle}，让新商品自然进入原有光线、材质和运动方式。`,
      default_value: true,
    },
    {
      id: "demo-ending",
      category: "text",
      label: "结尾强化",
      description: "在最后一段用更清晰的产品英雄定格收束，不添加虚构品牌字样。",
      default_value: false,
    },
  ];
}

function staticPromptText() {
  const selected = state.candidates.filter(item => state.selectedIds.has(item.id));
  const beats = (state.case?.rhythm || []).join("; ");
  const intentText = selected.map(item => `- ${item.label}: ${item.description}`).join("\n");
  return [
    `Create a complete ${state.case?.sourceDuration || 8}-second product advertising video inspired by the source case "${state.case?.title || "selected case"}".`,
    `Hero product: ${state.product?.label || "selected product"} (${state.product?.description || "public product image"}).`,
    `Keep the source rhythm: ${beats || "preserve the original beginning, middle, and final product reveal."}`,
    "Adaptation requirements:",
    intentText || "- Preserve the original visual grammar while replacing the hero product.",
    "Use clean commercial lighting, coherent product scale, and a final readable product hero frame. Do not include extra logos, captions, or watermarks.",
  ].join("\n");
}

async function selectCase(caseId) {
  const nextCase = state.cases.find(item => item.id === caseId);
  if (!nextCase) return;
  const changed = state.case?.id !== caseId;
  state.case = nextCase;
  if (changed) {
    await flushSession();
    state.product = null;
    state.candidates = [];
    state.selectedIds = new Set();
    state.messages = [];
    state.plan = null;
    state.runId = null;
    state.videoUrl = null;
    state.status = "draft";
    state.readOnly = false;
    if (staticDemo) {
      state.hero = { hero_product: nextCase.hero_product || {} };
      state.sessionId = null;
      state.revision = 0;
      setSessionUrl();
      $("#caseIndicator").textContent = nextCase.title;
      renderSources();
      await switchTab("chat");
      if (!state.messages.length) {
        addMessage("ai", `这是 GitHub Pages 静态展示。我会基于「<strong>${escapeHtml(nextCase.title)}</strong>」的案例元数据演示复刻流程；真实 Gemini/Compass 和 Seedance 调用需要本地后端运行。`);
      }
      return;
    }
    const session = await api("/api/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ case_id: caseId }),
    });
    state.sessionId = session.session_id;
    state.revision = session.revision;
    setSessionUrl(state.sessionId);
  }
  $("#caseIndicator").textContent = nextCase.title;
  renderSources();
  await switchTab("chat");
  const stopTyping = addTyping("正在通过 Gemini 理解源视频，首次分析可能需要几分钟");
  try {
    state.hero = await api(`/api/hero/${encodeURIComponent(caseId)}`, {}, 600000);
    stopTyping();
    if (!state.messages.length) {
      addMessage("ai", `我看完了「<strong>${escapeHtml(nextCase.title)}</strong>」。这条视频主要在卖<strong>${escapeHtml(state.hero.hero_product.neutral_label)}</strong>，商品约占 ${escapeHtml(state.hero.hero_product.screen_time_ratio)} 的关键画面。<br>它的核心是 ${escapeHtml(nextCase.style)}，我会保留节奏和展示窗口。`);
    }
    persist();
  } catch (error) {
    stopTyping();
    toast(`理解结果加载失败：${error.message}`);
  }
}

function addMessage(role, html, extra = {}) {
  state.messages.push({ id: makeId(), role, html, createdAt: Date.now(), ...extra });
  renderMessages();
  persist();
}

function renderMessages() {
  $("#conversation").innerHTML = state.messages.map(message => `
    <div class="message ${message.role}">
      <div class="avatar">${message.role === "ai" ? "AI" : "你"}</div>
      <div class="bubble">${message.html}<span class="message-meta">${new Date(message.createdAt).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}</span></div>
    </div>
  `).join("");
  requestAnimationFrame(() => $("#conversation").lastElementChild?.scrollIntoView({ behavior: "smooth", block: "nearest" }));
}

function renderProducts() {
  $("#productLibrary").innerHTML = state.products.map(product => `
    <button class="product-option ${state.product?.id === product.id ? "selected" : ""} ${state.inferring && state.product?.id === product.id ? "loading" : ""}" type="button" data-product="${escapeHtml(product.id)}" ${state.inferring ? "disabled" : ""}>
      <img src="${publicPath(product.local_url)}" alt="${escapeHtml(product.label)}">
      <span>${state.inferring && state.product?.id === product.id ? "处理中…" : escapeHtml(product.label)}</span>
    </button>
  `).join("");
  const status = $("#productInferenceStatus");
  if (status) {
    status.classList.toggle("hidden", !state.inferring);
    const elapsed = state.inferStartedAt ? Math.max(1, Math.floor((Date.now() - state.inferStartedAt) / 1000)) : 0;
    status.innerHTML = state.inferring
      ? `<span class="inline-spinner" aria-hidden="true"></span><strong>${escapeHtml(state.inferStatus || "请求已发出，正在生成改编方向")}</strong><small>已等待 ${elapsed}s，结果返回后会自动展开到下方。</small>`
      : "";
  }
  $$(".product-option").forEach(button => button.addEventListener("click", () => {
    const product = state.products.find(item => item.id === button.dataset.product);
    chooseProduct(product);
  }));
}

function renderChat() {
  const hasCase = Boolean(state.case);
  $("#chatEmpty").classList.toggle("hidden", hasCase);
  $("#chatWorkspace").classList.toggle("hidden", !hasCase);
  if (!hasCase) return;
  renderMessages();
  renderProducts();
  const hasIntents = state.candidates.length > 0;
  $("#productPicker").classList.toggle("hidden", state.readOnly);
  $("#intentSection").classList.toggle("hidden", !hasIntents || state.readOnly);
  $("#actionDock").classList.toggle("hidden", !state.product || !hasIntents || state.readOnly);
  $("#progressStep").textContent = state.plan ? "3" : state.product ? "2" : "1";
  renderIntents();
}

async function chooseProduct(product) {
  if (!product || !state.case || state.readOnly || state.inferring) return;
  state.product = product;
  state.candidates = [];
  state.selectedIds = new Set();
  state.plan = null;
  state.runId = null;
  toast(`已选择 ${product.label}，正在生成改编方向…`);
  addMessage("user", `我选了「<strong>${escapeHtml(product.label)}</strong>」作为宣传商品。`);
  renderChat();
  await inferIntents();
}

function addTyping(text) {
  const id = makeId();
  state.messages.push({ id, role: "ai", html: `${escapeHtml(text)} <span class="typing"><i></i><i></i><i></i></span>`, createdAt: Date.now(), transient: true });
  renderMessages();
  return () => {
    state.messages = state.messages.filter(message => message.id !== id);
    renderMessages();
  };
}

async function inferIntents() {
  if (state.inferring) return;
  if (staticDemo) {
    state.candidates = staticIntentCandidates();
    state.selectedIds = new Set(state.candidates.filter(item => item.default_value).map(item => item.id));
    addMessage("ai", `已基于公开案例元数据生成 <strong>${state.candidates.length} 个</strong>演示改编方向。`);
    renderChat();
    return;
  }
  state.inferring = true;
  state.inferStartedAt = Date.now();
  state.inferStatus = "请求已发出，正在连接 Gemini";
  renderProducts();
  const stopTyping = addTyping("正在结合这条视频和你的商品构思改编方向");
  const countdown = setInterval(() => {
    const elapsed = Math.floor((Date.now() - state.inferStartedAt) / 1000);
    state.inferStatus = elapsed < 8
      ? "请求已发出，正在加载源视频理解结果"
      : "Gemini 正在返回改编方向";
    const message = state.messages.find(item => item.transient);
    if (message) message.html = `正在构思改编方向 · 已等待 ${elapsed}s <span class="typing"><i></i><i></i><i></i></span>`;
    renderMessages();
    renderProducts();
  }, 1000);
  try {
    const payload = await api("/api/intent/infer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ case_id: state.case.id, product: state.product }),
    }, 600000);
    stopTyping();
    state.candidates = payload.intent_candidates;
    state.selectedIds = new Set(state.candidates.filter(item => item.default_value).map(item => item.id));
    addMessage("ai", `我准备了 <strong>${state.candidates.length} 个</strong>全局改编方向。先选中一个与你的视频反差明显的场景，你也可以自由组合；已选项的描述可以直接编辑。`);
    renderChat();
  } catch (error) {
    stopTyping();
    addMessage("ai", `意图推断失败：${escapeHtml(error.message)}<br><button class="retry-button" data-retry="infer" type="button">重试</button>`);
    bindRetryButtons();
  } finally {
    clearInterval(countdown);
    state.inferring = false;
    state.inferStartedAt = null;
    state.inferStatus = "";
    renderChat();
  }
}

function renderIntents() {
  const grid = $("#intentGrid");
  if (!grid) return;
  grid.innerHTML = state.candidates.map(intent => {
    const selected = state.selectedIds.has(intent.id);
    const editing = state.editingId === intent.id;
    return `
      <div class="intent-card ${selected ? "selected" : ""}" data-intent="${escapeHtml(intent.id)}">
        <div class="intent-top">
          <span class="intent-check">${selected ? "✓" : ""}</span>
          <span class="intent-category">${escapeHtml(categoryNames[intent.category] || intent.category)}</span>
          <strong>${escapeHtml(intent.label)}</strong>
        </div>
        ${editing ? `
          <textarea class="intent-edit" maxlength="240">${escapeHtml(intent.description)}</textarea>
          <div class="intent-edit-actions"><button class="cancel" type="button">取消</button><button class="save" type="button">保存改写</button></div>
        ` : `<p>${escapeHtml(intent.description)}</p>${selected ? '<button class="edit-trigger text-button" type="button">编辑描述</button>' : ""}`}
      </div>
    `;
  }).join("");
  $$(".intent-card", grid).forEach(card => {
    card.addEventListener("click", event => {
      if (event.target.closest("button, textarea")) return;
      const id = card.dataset.intent;
      if (state.selectedIds.has(id)) state.selectedIds.delete(id); else state.selectedIds.add(id);
      state.editingId = null;
      renderIntents();
      updateSelection();
      persist();
    });
    $(".edit-trigger", card)?.addEventListener("click", () => {
      state.editingId = card.dataset.intent;
      renderIntents();
      $(".intent-edit", $(`[data-intent="${CSS.escape(card.dataset.intent)}"]`, grid))?.focus();
    });
    $(".cancel", card)?.addEventListener("click", () => { state.editingId = null; renderIntents(); });
    $(".save", card)?.addEventListener("click", () => saveIntentEdit(card.dataset.intent, $(".intent-edit", card).value));
  });
  updateSelection();
}

function updateSelection() {
  $("#selectedCount").textContent = state.selectedIds.size;
}

async function saveIntentEdit(intentId, description) {
  if (staticDemo) {
    const intent = state.candidates.find(item => item.id === intentId);
    if (!intent) return;
    intent.description = description;
    state.editingId = null;
    addMessage("user", `把「${escapeHtml(intent.label)}」调整为：${escapeHtml(intent.description)}`);
    renderIntents();
    return;
  }
  try {
    const payload = await api("/api/intent/parse", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        case_id: state.case.id,
        intent_id: intentId,
        description,
        candidates: state.candidates,
        product: state.product,
      }),
    }, 120000);
    state.candidates = payload.intent_candidates;
    state.editingId = null;
    const intent = state.candidates.find(item => item.id === intentId);
    addMessage("user", `把「${escapeHtml(intent.label)}」调整为：${escapeHtml(intent.description)}`);
    renderIntents();
  } catch (error) {
    toast(error.message);
  }
}

async function handleUpload(file) {
  if (!file || !state.case) return;
  if (staticDemo) {
    toast("静态展示页不上传文件；请本地运行后端后再使用上传。");
    return;
  }
  const form = new FormData();
  form.append("case_id", state.case.id);
  form.append("image", file);
  try {
    const product = await api("/api/product/upload", { method: "POST", body: form });
    await chooseProduct(product);
  } catch (error) {
    toast(`上传失败：${error.message}`);
  }
}

async function commitAndPreview() {
  if (!state.product || !state.case || (!state.sessionId && !staticDemo) || state.readOnly) return;
  const button = $("#applyButton");
  const status = $("#promptCompileStatus");
  button.disabled = true;
  button.querySelector("span").textContent = "正在编译 Prompt…";
  status.classList.remove("hidden", "error");
  status.textContent = "正在提交创意调整并调用 Gemini 编译，通常需要几十秒。";
  state.plan = null;
  state.runId = null;
  state.status = "draft";
  state.videoUrl = null;
  addMessage("user", `应用 ${state.selectedIds.size} 项创意调整，并生成 Seedance Prompt。`);
  if (staticDemo) {
    state.plan = {
      run_id: `static-demo-${Date.now()}`,
      clip: {
        duration_sec: Math.max(4, Math.min(15, Number(state.case.sourceDuration) || 8)),
        narrative_beat: "Static GitHub Pages prompt preview",
        prompt: staticPromptText(),
      },
    };
    state.runId = state.plan.run_id;
    state.status = "validated";
    addMessage("ai", "静态展示已生成 Prompt 预览。真实结构校验和 Seedance 提交需要在本地配置 Compass 后运行。");
    status.classList.add("hidden");
    status.textContent = "";
    button.disabled = false;
    button.querySelector("span").textContent = "应用并生成 Prompt";
    switchTab("result");
    return;
  }
  try {
    await flushSession();
    const committed = await api("/api/intent/commit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        case_id: state.case.id,
        product: state.product,
        candidates: state.candidates,
        selected_ids: [...state.selectedIds],
        session_id: state.sessionId,
      }),
    });
    if (committed.session_revision) state.revision = committed.session_revision;
    const plan = await api("/api/validate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(committed.validate_input),
    }, 360000);
    if (plan._session_revision) {
      state.revision = plan._session_revision;
      delete plan._session_revision;
    }
    state.plan = plan;
    state.runId = plan.run_id;
    state.status = "validated";
    addMessage("ai", "Gemini 已完成参考图绑定、意图应用和 Seedance Prompt 结构校验。你可以在生成前做最后调整。");
    status.classList.add("hidden");
    status.textContent = "";
    switchTab("result");
  } catch (error) {
    const message = `Prompt 编译失败：${error.message}`;
    status.classList.add("error");
    status.textContent = `${message} 请重试；如果持续失败，请保留此错误信息。`;
    addMessage("ai", `${escapeHtml(message)}<br><button class="retry-button" data-retry="commit" type="button">重试</button>`);
    bindRetryButtons();
  } finally {
    button.disabled = false;
    button.querySelector("span").textContent = "应用并生成 Prompt";
  }
}

function renderResult() {
  const hasPlan = Boolean(state.plan);
  $("#resultEmpty").classList.toggle("hidden", hasPlan);
  $("#resultWorkspace").classList.toggle("hidden", !hasPlan);
  if (!hasPlan) return;
  $("#promptPreview").value = state.plan.clip.prompt;
  $("#promptPreview").readOnly = state.readOnly || state.status === "completed";
  $("#resultAspect").textContent = state.case.sourceAspect;
  $("#resultDuration").textContent = `${state.plan.clip.duration_sec}s`;
  $("#generateButton").classList.toggle("hidden", state.readOnly || state.status === "completed");
  $("#generateButton").disabled = state.status === "generating";
  $("#generateButton").textContent = staticDemo ? "静态展示" : "开始生成视频";
  if (staticDemo) $("#generateButton").disabled = true;
  $("#generationStatus").classList.add("hidden");
  $("#generationStatus").classList.remove("error");
  if (state.videoUrl) {
    $("#videoFrame").innerHTML = resultVideoComparison(state.videoUrl);
    bindVideoComparison($("#videoFrame"));
    $("#generationStatus").classList.remove("hidden", "error");
    $("#generationStatus").textContent = "生成完成，成片已保存到案例库。";
  } else if (state.status === "generating") {
    $("#videoFrame").innerHTML = `
      <div class="video-waiting">
        <span class="generation-orb"></span>
        <strong>Seedance 正在生成</strong>
        <small>页面会自动刷新生成状态</small>
      </div>`;
    $("#generationStatus").classList.remove("hidden", "error");
    $("#generationStatus").textContent = "Seedance 正在生成；页面可保持打开，稍后自动刷新…";
    pollGeneration();
  } else if (state.status === "failed") {
    $("#videoFrame").innerHTML = `
      <div class="video-waiting">
        <span class="generation-orb"></span>
        <strong>生成失败</strong>
        <small>调整 Prompt 后可以重试</small>
      </div>`;
    $("#generationStatus").classList.remove("hidden");
    $("#generationStatus").classList.add("error");
    $("#generationStatus").innerHTML = `生成失败：${escapeHtml(generationErrorMessage(state.generationError))} <button class="retry-button" data-retry="generate" type="button">重试</button>`;
    bindRetryButtons();
  } else {
    $("#videoFrame").innerHTML = `
      <div class="video-waiting">
        <span class="generation-orb"></span>
        <strong>Prompt 已就绪</strong>
        <small>确认后开始 Seedance 生成</small>
      </div>`;
  }
}

async function generateVideo() {
  if (staticDemo) {
    const status = $("#generationStatus");
    status.classList.remove("hidden", "error");
    status.textContent = "GitHub Pages 只展示前端和 Prompt 预览；真实生成需要本地后端和 Compass 环境变量。";
    return;
  }
  if (state.readOnly || state.status === "completed") return;
  const button = $("#generateButton");
  const status = $("#generationStatus");
  button.disabled = true;
  status.classList.remove("hidden", "error");
  status.textContent = "正在提交 Seedance 2.0 任务…";
  const form = new FormData();
  form.append("case_id", state.case.id);
  form.append("run_id", state.runId);
  state.plan.clip.prompt = $("#promptPreview").value;
  await persist();
  await flushSession();
  form.append("prompt_text", state.plan.clip.prompt);
  try {
    const payload = await api("/api/generate", { method: "POST", body: form }, 60000);
    if (payload.session_revision) state.revision = payload.session_revision;
    state.status = "generating";
    if (payload.status === "ready") {
      status.textContent = payload.message;
      button.disabled = false;
      return;
    }
    status.textContent = "任务已提交，Seedance 正在生成完整视频…";
    pollGeneration();
  } catch (error) {
    status.classList.add("error");
    status.innerHTML = `提交失败：${escapeHtml(error.message)} <button class="retry-button" data-retry="generate" type="button">重试</button>`;
    bindRetryButtons();
    button.disabled = false;
  }
}

async function pollGeneration() {
  if (generationPollTimer) {
    clearTimeout(generationPollTimer);
    generationPollTimer = null;
  }
  const status = $("#generationStatus");
  try {
    const payload = await api(`/api/generate/${encodeURIComponent(state.case.id)}/status?run_id=${encodeURIComponent(state.runId)}`);
    if (payload.status === "succeeded") {
      if (payload.session_revision) state.revision = payload.session_revision;
      state.status = "completed";
      state.videoUrl = payload.video_url;
      state.generationError = null;
      $("#videoFrame").innerHTML = resultVideoComparison(payload.video_url, true);
      bindVideoComparison($("#videoFrame"));
      status.classList.remove("hidden", "error");
      status.textContent = "生成完成，成片已保存到独立 run 目录。";
      $("#generateButton").disabled = false;
      return;
    }
    if (payload.status === "failed") {
      if (payload.session_revision) state.revision = payload.session_revision;
      state.status = "failed";
      state.generationError = payload.error;
      $("#videoFrame").innerHTML = `
        <div class="video-waiting">
          <span class="generation-orb"></span>
          <strong>生成失败</strong>
          <small>调整 Prompt 后可以重试</small>
        </div>`;
      status.classList.remove("hidden");
      status.classList.add("error");
      status.innerHTML = `生成失败：${escapeHtml(generationErrorMessage(payload.error))} <button class="retry-button" data-retry="generate" type="button">重试</button>`;
      bindRetryButtons();
      $("#generateButton").disabled = false;
      return;
    }
    status.classList.remove("hidden", "error");
    status.textContent = "Seedance 正在生成；页面可保持打开，稍后自动刷新…";
    generationPollTimer = setTimeout(pollGeneration, 5000);
  } catch (error) {
    state.status = "failed";
    state.generationError = error.message;
    $("#videoFrame").innerHTML = `
      <div class="video-waiting">
        <span class="generation-orb"></span>
        <strong>生成失败</strong>
        <small>调整 Prompt 后可以重试</small>
      </div>`;
    status.classList.add("error");
    status.innerHTML = `生成失败：${escapeHtml(error.message)} <button class="retry-button" data-retry="generate" type="button">重试</button>`;
    bindRetryButtons();
    $("#generateButton").disabled = false;
  }
}

function bindRetryButtons() {
  $$("[data-retry]").forEach(button => button.onclick = () => {
    if (button.dataset.retry === "infer") inferIntents();
    if (button.dataset.retry === "commit") commitAndPreview();
    if (button.dataset.retry === "generate") generateVideo();
  });
}

function historyStatusLabel(status) {
  return {
    draft: "编辑中",
    validated: "Prompt 已就绪",
    generating: "生成中",
    completed: "已完成",
    failed: "失败",
    pending_validation: "待编译",
  }[status] || status;
}

async function renderStaticHistory() {
  const list = $("#historyList");
  const detail = $("#historyDetail");
  detail.classList.add("hidden");
  list.classList.remove("hidden");
  list.innerHTML = '<div class="history-loading">正在读取生成结果…</div>';
  try {
    const response = await fetch("generated-results.json");
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    const results = await response.json();
    if (!Array.isArray(results) || !results.length) {
      list.innerHTML = '<div class="history-empty"><h2>还没有公开结果</h2><p>生成视频发布后会出现在这里。</p></div>';
      return;
    }
    state.history = results;
    list.innerHTML = results.map(item => `
      <article class="history-card result-card">
        <video class="history-result-video" src="${escapeHtml(publicPath(item.video_url))}#t=0.1" controls preload="metadata" playsinline></video>
        <div class="history-card-body">
          <div class="history-card-top">
            <span class="history-status completed">生成结果</span>
            <time>${escapeHtml(item.generated_at_label || "")}</time>
          </div>
          <h2>${escapeHtml(item.case_title)}</h2>
          <p>${escapeHtml(item.subtitle || item.style || "")}</p>
          <div class="history-result-meta">
            <span>${escapeHtml(item.tag || "案例")}</span>
            <span>${escapeHtml(item.source_aspect || "")}</span>
            <span>${escapeHtml(item.duration_sec ? `${item.duration_sec}s` : "")}</span>
          </div>
          <a class="history-source-link" href="${escapeHtml(publicPath(item.source_video))}" target="_blank" rel="noreferrer">查看源片</a>
        </div>
      </article>
    `).join("");
  } catch (error) {
    list.innerHTML = `<div class="history-empty"><h2>生成结果读取失败</h2><p>${escapeHtml(error.message)}</p></div>`;
  }
}

async function renderHistory() {
  const list = $("#historyList");
  const detail = $("#historyDetail");
  if (!list || !detail) return;
  detail.classList.add("hidden");
  list.classList.remove("hidden");
  if (staticDemo) {
    await renderStaticHistory();
    return;
  }
  list.innerHTML = '<div class="history-loading">正在读取案例文件…</div>';
  try {
    state.history = await api("/api/history");
    if (!state.history.length) {
      list.innerHTML = '<div class="history-empty"><h2>还没有历史案例</h2><p>从灵感库创建的会话会自动保存在这里。</p></div>';
      return;
    }
    list.innerHTML = state.history.map(item => `
      <article class="history-card">
        <video src="${escapeHtml(item.video_url || item.source_video || "")}#t=0.1" preload="metadata" muted playsinline></video>
        <div class="history-card-body">
          <div class="history-card-top">
            <span class="history-status ${escapeHtml(item.status)}">${escapeHtml(historyStatusLabel(item.status))}</span>
            <time>${new Date(item.updated_at).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })}</time>
          </div>
          <h2>${escapeHtml(item.case_title)}</h2>
          <p>${escapeHtml(item.product_label || "尚未选择商品")} · ${item.message_count} 条对话</p>
          <div class="history-card-actions">
            <button class="secondary-button" type="button" data-history-kind="${escapeHtml(item.kind)}" data-history-case="${escapeHtml(item.case_id)}" data-history-id="${escapeHtml(item.kind === "session" ? item.id : item.run_id)}" data-history-action="${item.can_continue ? "continue" : "view"}">
              ${item.can_continue ? "继续编辑" : "查看案例"}
            </button>
            ${item.kind === "session" ? `<button class="danger-button" type="button" data-delete-session="${escapeHtml(item.id)}" data-delete-title="${escapeHtml(item.case_title)}">删除</button>` : ""}
          </div>
        </div>
      </article>
    `).join("");
    $$("[data-history-id]", list).forEach(button => button.addEventListener("click", () => {
      openHistoryEntry(button.dataset.historyKind, button.dataset.historyCase, button.dataset.historyId, button.dataset.historyAction);
    }));
    $$("[data-delete-session]", list).forEach(button => button.addEventListener("click", () => {
      deleteHistorySession(button.dataset.deleteSession, button.dataset.deleteTitle);
    }));
  } catch (error) {
    list.innerHTML = `<div class="history-empty"><h2>案例库读取失败</h2><p>${escapeHtml(error.message)}</p></div>`;
  }
}

async function deleteHistorySession(sessionId, title) {
  if (!confirm(`删除「${title || "这个案例"}」？删除后会同时清理关联生成文件。`)) return;
  try {
    await api(`/api/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
    if (state.sessionId === sessionId) await resetStateOnly();
    toast("案例已删除");
    await renderHistory();
  } catch (error) {
    toast(`删除失败：${error.message}`);
  }
}

async function ensureRunSession(caseId, runId, action) {
  const endpoint = action === "continue" ? "continue" : "session";
  return api(`/api/runs/${encodeURIComponent(caseId)}/${encodeURIComponent(runId)}/${endpoint}`, { method: "POST" });
}

async function openHistoryEntry(kind, caseId, id, action = "view") {
  try {
    const session = kind === "session"
      ? await api(`/api/sessions/${encodeURIComponent(id)}`)
      : await ensureRunSession(caseId, id, action);
    await loadSession(session);
  } catch (error) {
    toast(`历史案例读取失败：${error.message}`);
  }
}

async function openHistory(kind, caseId, id, options = {}) {
  const list = $("#historyList");
  const detail = $("#historyDetail");
  try {
    const payload = options.payload || (kind === "session"
      ? await api(`/api/sessions/${encodeURIComponent(id)}`)
      : await api(`/api/runs/${encodeURIComponent(caseId)}/${encodeURIComponent(id)}`));
    if (payload.session_id && options.updateUrl !== false) setSessionUrl(payload.session_id, "history");
    const caseInfo = state.cases.find(item => item.id === payload.case_id);
    const messages = payload.messages || [];
    const videoUrl = payload.video_url;
    const canContinue = payload.status !== "completed";
    list.classList.add("hidden");
    detail.classList.remove("hidden");
    detail.innerHTML = `
      <div class="history-detail-head">
        <button class="text-button" id="historyBack" type="button">← 返回案例库</button>
        ${canContinue ? '<button class="primary-button" id="historyContinue" type="button">继续编辑</button>' : ""}
      </div>
      <div class="history-detail-title">
        <div><span class="eyebrow">CASE HISTORY</span><h1>${escapeHtml(caseInfo?.title || payload.case_id)}</h1></div>
        <span class="history-status ${escapeHtml(payload.status)}">${escapeHtml(historyStatusLabel(payload.status))}</span>
      </div>
      <div class="history-detail-grid ${videoUrl ? "" : "without-video"}">
        <section class="history-conversation">
          <h2>对话历史</h2>
          <div class="conversation">${messages.map(message => `
            <div class="message ${escapeHtml(message.role)}">
              <div class="avatar">${message.role === "ai" ? "AI" : "你"}</div>
              <div class="bubble">${message.html || escapeHtml(message.content || "")}<span class="message-meta">${new Date(message.createdAt || payload.updated_at || Date.now()).toLocaleString("zh-CN")}</span></div>
            </div>
          `).join("") || '<p class="history-muted">该案例没有对话记录。</p>'}</div>
        </section>
        ${videoUrl ? `
          <section class="history-video">
            <h2>成片视频</h2>
            <video src="${escapeHtml(videoUrl)}" controls playsinline></video>
          </section>
        ` : ""}
      </div>
    `;
    $("#historyBack").addEventListener("click", () => {
      setSessionUrl(state.sessionId || payload.session_id || null);
      renderHistory();
    });
    $("#historyContinue")?.addEventListener("click", async () => {
      if (kind === "session") {
        await loadSession(payload);
        return;
      }
      const session = await api(`/api/runs/${encodeURIComponent(caseId)}/${encodeURIComponent(id)}/continue`, { method: "POST" });
      await loadSession(session);
    });
  } catch (error) {
    toast(`历史案例读取失败：${error.message}`);
  }
}

function applySession(payload, readOnly = false, options = {}) {
  const match = state.cases.find(item => item.id === payload.case_id);
  if (!match) throw new Error("源案例已不存在");
  state.case = match;
  state.product = payload.product || null;
  state.candidates = payload.candidates || [];
  state.selectedIds = new Set(payload.selected_ids || []);
  state.messages = payload.messages || payload.chat_history || [];
  state.plan = payload.plan || payload.plan_meta || null;
  state.runId = payload.run_id || state.plan?.run_id || null;
  state.sessionId = payload.session_id || null;
  state.revision = payload.revision || 0;
  state.status = payload.status || "draft";
  state.videoUrl = payload.video_url || null;
  state.generationError = payload.generation_error || null;
  state.readOnly = readOnly || Boolean(payload.read_only);
  $("#caseIndicator").textContent = match.title;
  if (state.sessionId) setSessionUrl(state.sessionId, options.view || null);
  renderSources();
}

async function loadSession(payloadOrId) {
  await flushSession();
  const payload = typeof payloadOrId === "string"
    ? await api(`/api/sessions/${encodeURIComponent(payloadOrId)}`)
    : payloadOrId;
  applySession(payload, false);
  await switchTab(["completed", "generating", "failed"].includes(payload.status) ? "result" : "chat");
  resumePendingIntentInference();
}

function resumePendingIntentInference() {
  const shouldResume = state.product
    && state.case
    && !state.readOnly
    && state.status === "draft"
    && !state.plan
    && state.candidates.length === 0
    && !state.inferring;
  if (!shouldResume) return;
  if (!state.messages.length) {
    addMessage("ai", `继续为「<strong>${escapeHtml(state.product.label)}</strong>」构思改编方向。`);
  }
  inferIntents();
}

async function restore() {
  if (staticDemo) return;
  const params = new URL(window.location.href).searchParams;
  const sessionId = params.get("session");
  if (!sessionId) return;
  await loadSession(sessionId);
}

async function reset() {
  if (!confirm("重置当前对话、商品和意图选择？")) return;
  await flushSession();
  Object.assign(state, {
    activeTab: "source",
    case: null,
    hero: null,
    product: null,
    candidates: [],
    messages: [],
    plan: null,
    runId: null,
    sessionId: null,
    revision: 0,
    status: "draft",
    videoUrl: null,
    generationError: null,
    readOnly: false,
    editingId: null,
    inferStartedAt: null,
    inferStatus: "",
  });
  state.selectedIds = new Set();
  setSessionUrl();
  $("#caseIndicator").textContent = "尚未选择创意";
  renderSources();
  switchTab("source");
}

async function resetStateOnly() {
  Object.assign(state, {
    activeTab: "source",
    case: null,
    hero: null,
    product: null,
    candidates: [],
    messages: [],
    plan: null,
    runId: null,
    sessionId: null,
    revision: 0,
    status: "draft",
    videoUrl: null,
    generationError: null,
    readOnly: false,
    editingId: null,
    inferring: false,
    inferStartedAt: null,
    inferStatus: "",
  });
  state.selectedIds = new Set();
  setSessionUrl();
  $("#caseIndicator").textContent = "尚未选择创意";
  renderSources();
}

async function init() {
  try {
    if (staticDemo) {
      const cases = await fetch("cases.json").then(response => {
        if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
        return response.json();
      });
      state.cases = cases.map(normalizeCase);
      state.products = STATIC_PRODUCTS.map(normalizeProduct);
      $("#runtimeText").textContent = "GitHub Pages Demo · 后端需本地运行";
    } else {
      [state.cases, state.products] = await Promise.all([api("/api/cases"), api("/api/products")]);
      state.cases = state.cases.map(normalizeCase);
      state.products = state.products.map(normalizeProduct);
      const health = await api("/api/health");
      $("#runtimeText").textContent = health.compass_configured
        ? `Seedance 2.0 · ${health.llm_model} 真实 API`
        : "真实 API · 等待 Compass 配置";
    }
    renderSources();
    renderProducts();
    await restore();
  } catch (error) {
    if (!staticDemo) {
      staticDemo = true;
      try {
        const cases = await fetch("cases.json").then(response => {
          if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
          return response.json();
        });
        state.cases = cases.map(normalizeCase);
        state.products = STATIC_PRODUCTS.map(normalizeProduct);
        $("#runtimeText").textContent = "静态展示 · 后端不可用";
        renderSources();
        renderProducts();
      } catch (fallbackError) {
        toast(`初始化失败：${fallbackError.message}`);
      }
    } else {
      toast(`初始化失败：${error.message}`);
    }
  }

  $$(".tab").forEach(button => button.addEventListener("click", () => switchTab(button.dataset.tab)));
  $$(".history-link").forEach(button => button.addEventListener("click", () => switchTab("history")));
  $$("[data-jump]").forEach(button => button.addEventListener("click", () => switchTab(button.dataset.jump)));
  $$("#filterPills button").forEach(button => button.addEventListener("click", () => {
    state.filter = button.dataset.filter;
    $$("#filterPills button").forEach(item => item.classList.toggle("active", item === button));
    renderSources();
  }));
  $("#productUpload").addEventListener("change", event => handleUpload(event.target.files[0]));
  const dropzone = $("#dropzone");
  ["dragenter", "dragover"].forEach(name => dropzone.addEventListener(name, event => { event.preventDefault(); dropzone.classList.add("dragging"); }));
  ["dragleave", "drop"].forEach(name => dropzone.addEventListener(name, event => { event.preventDefault(); dropzone.classList.remove("dragging"); }));
  dropzone.addEventListener("drop", event => handleUpload(event.dataTransfer.files[0]));
  $("#applyButton").addEventListener("click", commitAndPreview);
  $("#generateButton").addEventListener("click", generateVideo);
  $("#copyPrompt").addEventListener("click", async () => {
    try {
      const copied = await copyText($("#promptPreview").value);
      toast(copied ? "Prompt 已复制" : "复制失败，请手动全选复制");
    } catch (error) {
      toast(`复制失败：${error.message}`);
    }
  });
  $("#resetButton").addEventListener("click", reset);
}

init();
