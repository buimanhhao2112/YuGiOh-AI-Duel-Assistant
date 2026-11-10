(() => {
  "use strict";

  const state = {
    cards: [],
    cardByName: new Map(),
    fuse: null,
    deckIndex: [],
    deckMainCounts: new Map(),
    main: new Map(),
    extra: new Map(),
    activeZone: "main",
    detectedHand: [],
    visionTimer: null,
    toastTimer: null,
  };
  const $ = (selector) => document.querySelector(selector);
  const normalize = (value) => String(value || "").normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase();

  function showToast(message, error = false) {
    const toast = $("#toast");
    toast.textContent = message;
    toast.classList.toggle("error", error);
    toast.classList.add("show");
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => toast.classList.remove("show"), 3200);
  }

  async function api(url, options = {}) {
    const response = await fetch(url, {
      ...options,
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = typeof result.detail === "string" ? result.detail : JSON.stringify(result.detail || result);
      throw new Error(detail || `Request failed (${response.status})`);
    }
    return result;
  }

  function isExtra(card) {
    const frame = normalize(card.frame_type);
    return card.type === "Monster" && ["fusion", "synchro", "xyz", "link"].some((part) => frame.includes(part));
  }

  function copyLimit(card) {
    const status = normalize(card.ban_status);
    if (status.includes("forbidden")) return 0;
    if (status.includes("limited 2") || status.includes("semi")) return 2;
    if (status.includes("limited 1") || status === "limited") return 1;
    return 3;
  }

  function currentCount(map) {
    let count = 0;
    for (const copies of map.values()) count += copies;
    return count;
  }

  function countDeckCards(entries) {
    const counts = new Map();
    for (const entry of entries) {
      const name = String(entry.name || "").trim();
      if (!name) continue;
      counts.set(name, (counts.get(name) || 0) + (Number(entry.count) || 0));
    }
    return counts;
  }

  function addCard(card, requestedZone = null) {
    const zone = isExtra(card) ? "extra" : "main";
    if (requestedZone && requestedZone !== zone) {
      showToast(`${card.name} thuộc ${zone === "extra" ? "Extra Deck" : "Main Deck"}.`, true);
      return;
    }
    const deck = zone === "extra" ? state.extra : state.main;
    const maximum = zone === "extra" ? 15 : 60;
    if (currentCount(deck) >= maximum) {
      showToast(`${zone === "extra" ? "Extra Deck" : "Main Deck"} đã đủ ${maximum} lá.`, true);
      return;
    }
    const current = deck.get(card.name) || 0;
    if (current >= copyLimit(card)) {
      showToast(`${card.name} đã đạt giới hạn ${copyLimit(card)} bản sao theo catalog.`, true);
      return;
    }
    deck.set(card.name, current + 1);
    renderDeck();
  }

  function removeCard(zone, name) {
    const deck = zone === "extra" ? state.extra : state.main;
    const next = (deck.get(name) || 0) - 1;
    if (next <= 0) deck.delete(name);
    else deck.set(name, next);
    renderDeck();
  }

  function escapeText(value) {
    return String(value ?? "");
  }

  function renderResults(query = $("#card-search").value) {
    const started = performance.now();
    const type = $("#type-filter").value;
    const attribute = $("#attribute-filter").value;
    const filteredBySelect = (card) => (!type || card.type === type) && (!attribute || card.attribute === attribute);
    let matches;
    const trimmed = query.trim();
    if (trimmed && state.fuse) {
      matches = state.fuse.search(trimmed, { limit: 250 }).map((entry) => entry.item).filter(filteredBySelect);
    } else if (trimmed) {
      const needle = normalize(trimmed);
      matches = state.cards.filter((card) => filteredBySelect(card) &&
        `${normalize(card.name)} ${normalize(card.archetype)} ${normalize(card.race_type)}`.includes(needle));
    } else {
      matches = state.cards.filter(filteredBySelect);
    }
    const shown = matches.slice(0, 80);
    const root = $("#card-results");
    root.replaceChildren();
    if (!shown.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = "Không tìm thấy lá bài phù hợp.";
      root.append(empty);
    } else {
      const fragment = document.createDocumentFragment();
      shown.forEach((card) => {
        const row = document.createElement("div");
        row.className = "card-row";
        row.setAttribute("role", "option");
        row.draggable = true;
        row.addEventListener("dragstart", (event) => event.dataTransfer.setData("text/plain", card.name));
        row.addEventListener("click", (event) => {
          if (!event.target.closest("button")) addCard(card);
        });
        const text = document.createElement("div");
        const title = document.createElement("div");
        title.className = "card-name";
        title.textContent = escapeText(card.name);
        const meta = document.createElement("div");
        meta.className = "card-meta";
        const stats = card.type === "Monster"
          ? [card.attribute, card.race_type, card.level_rank_link ? `${card.frame_type} ${card.level_rank_link}` : card.frame_type, card.atk != null ? `ATK ${card.atk}` : ""].filter(Boolean).join(" · ")
          : [card.type, card.frame_type, card.rarity].filter(Boolean).join(" · ");
        meta.textContent = stats || card.archetype || "Card";
        text.append(title, meta);
        const button = document.createElement("button");
        button.className = "card-add";
        button.type = "button";
        button.textContent = "+";
        button.title = "Thêm vào deck";
        button.addEventListener("click", () => addCard(card));
        row.append(text, button);
        fragment.append(row);
      });
      root.append(fragment);
    }
    $("#visible-count").textContent = `${matches.length.toLocaleString()} cards`;
    $("#search-time").textContent = `${(performance.now() - started).toFixed(1)} ms`;
  }

  function renderDeck() {
    const mainCount = currentCount(state.main);
    const extraCount = currentCount(state.extra);
    $("#main-count").textContent = mainCount;
    $("#extra-count").textContent = extraCount;
    $("#main-tab-count").textContent = mainCount;
    $("#extra-tab-count").textContent = extraCount;
    renderDeckZone("main", state.main);
    renderDeckZone("extra", state.extra);
  }

  function renderDeckZone(zone, entries) {
    const root = zone === "main" ? $("#main-drop") : $("#extra-drop");
    root.replaceChildren();
    if (!entries.size) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = zone === "main"
        ? "Thả thẻ vào đây hoặc nhấn + trong kết quả tìm kiếm."
        : "Extra Deck — tối đa 15 lá.";
      root.append(empty);
      return;
    }
    const fragment = document.createDocumentFragment();
    let index = 0;
    for (const [name, count] of entries) {
      const row = document.createElement("div");
      row.className = "deck-entry";
      const number = document.createElement("span");
      number.className = "entry-index";
      number.textContent = String(++index).padStart(2, "0");
      const title = document.createElement("span");
      title.className = "card-name";
      title.textContent = name;
      const copies = document.createElement("span");
      copies.className = "copy-count";
      copies.textContent = `×${count}`;
      const remove = document.createElement("button");
      remove.className = "remove-card";
      remove.type = "button";
      remove.textContent = "−";
      remove.title = "Bỏ một bản sao";
      remove.addEventListener("click", () => removeCard(zone, name));
      row.append(number, title, copies, remove);
      fragment.append(row);
    }
    root.append(fragment);
  }

  function setActiveZone(zone) {
    state.activeZone = zone;
    document.querySelectorAll(".deck-tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.zone === zone));
    document.querySelectorAll(".deck-list").forEach((list) => list.classList.toggle("active", list.dataset.zone === zone));
  }

  async function loadDeckIndex(preferredFile = "") {
    const result = await api("/api/decks");
    state.deckIndex = result.decks || [];
    const selectedFile = state.deckIndex.some((deck) => deck.file === preferredFile)
      ? preferredFile
      : state.deckIndex[0]?.file || "";
    for (const selector of [$("#deck-select"), $("#deck_selector")]) {
      selector.replaceChildren();
      state.deckIndex.forEach((deck) => {
        const option = document.createElement("option");
        option.value = deck.file;
        option.textContent = `${deck.name} (${deck.main_count}/${deck.extra_count})`;
        selector.append(option);
      });
      if (!state.deckIndex.length) {
        const option = document.createElement("option");
        option.value = "";
        option.textContent = "Chưa có deck";
        selector.append(option);
      }
      selector.value = selectedFile;
    }
  }

  async function loadSelectedDeck(file = $("#deck_selector").value, { showLoading = true } = {}) {
    if (!file) {
      state.main.clear();
      state.extra.clear();
      state.deckMainCounts.clear();
      renderHandSlots([]);
      renderDeck();
      return;
    }
    $("#deck-select").value = file;
    $("#deck_selector").value = file;
    if (showLoading) renderHandSlots([], "Đang nạp bài từ deck…");
    const raw = await api(`/api/decks/${encodeURIComponent(file)}`);
    state.main.clear();
    state.extra.clear();
    const mainCards = raw.main_deck || [];
    state.deckMainCounts = countDeckCards(mainCards);
    for (const [name, count] of state.deckMainCounts) state.main.set(name, count);
    for (const [name, count] of countDeckCards(raw.extra_deck || [])) state.extra.set(name, count);
    const uniqueCardNames = [...new Set(mainCards.map((entry) => String(entry.name || "").trim()).filter(Boolean))];
    renderHandSlots(uniqueCardNames);
    $("#deck-name").value = raw.name || file.replace(/\.json$/i, "");
    renderDeck();
    $("#deck-message").textContent = `${uniqueCardNames.length} tên bài trong Main Deck`;
  }

  function renderHandSlots(uniqueCardNames, placeholderText = "Chọn lá bài…") {
    document.querySelectorAll(".hand-card-select").forEach((select, index) => {
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = placeholderText;
      select.replaceChildren(placeholder);
      uniqueCardNames.forEach((name) => {
        const option = document.createElement("option");
        option.value = name;
        option.textContent = name;
        select.append(option);
      });
      select.setAttribute("aria-label", `Slot ${index + 1} hand bài`);
    });
  }

  async function saveDeck() {
    const payload = {
      name: $("#deck-name").value.trim(),
      main_deck: [...state.main].map(([name, count]) => ({ name, count })),
      extra_deck: [...state.extra].map(([name, count]) => ({ name, count })),
    };
    const message = $("#deck-message");
    message.textContent = "Đang lưu…";
    try {
      const saved = await api("/api/decks", { method: "POST", body: JSON.stringify(payload) });
      message.textContent = `Lưu thành ${saved.file}`;
      await loadDeckIndex(saved.file);
      await loadSelectedDeck(saved.file);
      showToast(`Đã lưu deck vào examples/${saved.file}.`);
    } catch (error) {
      message.textContent = "Không lưu được";
      showToast(error.message, true);
    }
  }

  function splitHand(value) {
    return value.split(/[|\r\n]+/).map((name) => name.trim()).filter(Boolean);
  }

  function renderRouteResult(resultBox, result, { human = false } = {}) {
    resultBox.replaceChildren();
    resultBox.classList.remove("hidden", "error");
    const summary = document.createElement("div");
    const outcome = human
      ? `${result.is_gold_route ? "🏆 GOLD ROUTE" : "Route được mô phỏng"} · score ${result.score} · MCTS ${result.baseline_score}`
      : `${result.cache_hit ? "⚡ Route từ cache" : "⚡ MCTS đã tìm route"} · score ${result.score} · ${result.simulations} lượt · ${result.expanded_nodes} node`;
    summary.className = "route-result-summary";
    summary.textContent = `${outcome} · ${result.mode} · End-board: ${result.end_board.join(" + ") || "field trống"}`;
    resultBox.append(summary);
    const steps = result.steps || [];
    if (!steps.length) {
      const empty = document.createElement("div");
      empty.className = "route-result-empty";
      empty.textContent = "Không tìm thấy nước đi trong các operator hiện được mô hình hóa.";
      resultBox.append(empty);
      return;
    }
    const list = document.createElement("ol");
    list.className = "route-result-list";
    steps.forEach((step, index) => {
      const item = document.createElement("li");
      item.textContent = `Bước ${index + 1}: ${step}`;
      list.append(item);
    });
    resultBox.append(list);
  }

  async function generateAiRoute() {
    const slots = Array.from(document.querySelectorAll(".hand-card-select"));
    const hand = slots.map((slot) => slot.value);
    const resultBox = $("#ai-route-result");
    const button = $("#generate-ai-route");
    if (hand.length !== 5 || hand.some((name) => !name)) {
      showToast("Vui lòng chọn đủ 5 lá bài đầu tay.", true);
      slots.find((slot) => !slot.value)?.focus();
      return;
    }
    const copies = new Map();
    for (const name of hand) copies.set(name, (copies.get(name) || 0) + 1);
    const exceedsDeck = [...copies].find(([name, count]) => count > (state.deckMainCounts.get(name) || 0));
    if (exceedsDeck) {
      showToast(`${exceedsDeck[0]} chỉ có ${state.deckMainCounts.get(exceedsDeck[0]) || 0} bản sao trong Main Deck.`, true);
      return;
    }
    button.disabled = true;
    button.textContent = "Đang tìm route bằng MCTS…";
    resultBox.classList.remove("hidden", "error");
    resultBox.textContent = "Đang tra cache; nếu chưa có route, AI sẽ tự tìm bằng MCTS…";
    try {
      const result = await api("/api/generate-route", {
        method: "POST",
        body: JSON.stringify({
          deck_file: $("#deck_selector").value || "vs_k9_deck.json",
          hand,
          mode: $("#ai-route-mode").value,
        }),
      });
      renderRouteResult(resultBox, result);
    } catch (error) {
      resultBox.textContent = error.message;
      resultBox.classList.add("error");
    } finally {
      button.disabled = false;
      button.textContent = "⚡ GENERATE AI ROUTE";
    }
  }

  async function benchmarkRoute() {
    const hand = splitHand($("#human-route-hand").value);
    const routeSteps = $("#human-route-steps").value.split(/\r?\n/).map((step) => step.trim()).filter(Boolean);
    const resultBox = $("#human-route-result");
    if (routeSteps.length === 0) {
      showToast("Vui lòng nhập các bước đi để AI chấm điểm");
      $("#human-route-steps").focus();
      return;
    }
    if (hand.length === 0) {
      showToast("Vui lòng nhập tay bài bắt đầu cho route.");
      $("#human-route-hand").focus();
      return;
    }
    const button = $("#benchmark-route");
    button.disabled = true;
    resultBox.classList.remove("hidden", "error");
    resultBox.textContent = "Đang kiểm tra operator và chạy baseline MCTS…";
    try {
      const result = await api("/api/benchmark-route", {
        method: "POST",
        body: JSON.stringify({
          deck_file: $("#deck_selector").value || "vs_k9_deck.json",
          hand,
          route_steps: routeSteps,
          mode: $("#human-route-mode").value,
          baseline_iterations: 400,
        }),
      });
      renderRouteResult(resultBox, result, { human: true });
    } catch (error) {
      resultBox.textContent = error.message;
      resultBox.classList.add("error");
    } finally {
      button.disabled = false;
    }
  }

  function renderVisionResult(result) {
    state.detectedHand = result.hand || [];
    const handRoot = $("#vision-hand");
    handRoot.textContent = state.detectedHand.length ? state.detectedHand.join(" · ") : "Không khớp template thẻ nào.";
    $("#use-detected-hand").disabled = state.detectedHand.length === 0;
    const archetypes = (result.opponent_archetypes || []).join(", ");
    const warnings = [result.template_warning, result.ocr_warning].filter(Boolean).join(" · ");
    const lines = [archetypes ? `Đối thủ có thể chơi: ${archetypes}` : "Chưa nhận ra archetype.", result.ocr_text || "Không đọc được text combat log.", warnings].filter(Boolean);
    $("#vision-log").textContent = lines.join("\n");
  }

  async function checkVisionStatus() {
    try {
      const status = await api("/api/vision/status");
      const missing = Object.entries(status.modules || {}).filter(([, ready]) => !ready).map(([name]) => name);
      const parts = [`${status.templates} templates`, status.tesseract_binary ? "Tesseract sẵn sàng" : "Tesseract chưa cài"];
      if (missing.length) parts.push(`Thiếu package: ${missing.join(", ")}`);
      $("#vision-capabilities").textContent = parts.join(" · ");
      $("#vision-state").textContent = status.available ? "Sẵn sàng" : "Thiếu dependency";
    } catch (error) {
      $("#vision-state").textContent = "Không kết nối";
      $("#vision-capabilities").textContent = error.message;
    }
  }

  async function scanOnce() {
    $("#vision-state").textContent = "Đang chụp…";
    try {
      const result = await api("/api/vision/scan", { method: "POST", body: JSON.stringify({ monitor: 1 }) });
      renderVisionResult(result);
      $("#vision-state").textContent = "Đã quét";
    } catch (error) {
      $("#vision-state").textContent = "Vision chưa sẵn sàng";
      $("#vision-log").textContent = error.message;
    }
  }

  async function toggleVision() {
    const button = $("#toggle-vision");
    if (state.visionTimer) {
      await api("/api/vision/worker/stop", { method: "POST", body: "{}" }).catch(() => {});
      clearInterval(state.visionTimer);
      state.visionTimer = null;
      button.textContent = "Bắt đầu quét 0,5s";
      $("#vision-state").textContent = "Đã dừng";
      return;
    }
    try {
      await api("/api/vision/worker/start", { method: "POST", body: JSON.stringify({ monitor: 1 }) });
      button.textContent = "Dừng quét";
      $("#vision-state").textContent = "Đang quét";
      state.visionTimer = setInterval(async () => {
        try {
          const data = await api("/api/vision/latest");
          if (data.latest?.hand || data.latest?.error) renderVisionResult(data.latest);
          if (data.latest?.error) {
            $("#vision-state").textContent = "Vision chưa sẵn sàng";
            $("#vision-log").textContent = data.latest.error;
            clearInterval(state.visionTimer);
            state.visionTimer = null;
            button.textContent = "Bắt đầu quét 0,5s";
          }
        } catch (_) { /* A transient poll failure must not stop the UI. */ }
      }, 500);
    } catch (error) {
      showToast(error.message, true);
      $("#vision-state").textContent = "Không thể bắt đầu";
    }
  }

  function setupDropZones() {
    document.querySelectorAll(".drop-zone").forEach((zone) => {
      zone.addEventListener("dragover", (event) => { event.preventDefault(); zone.classList.add("drag-over"); });
      zone.addEventListener("dragleave", () => zone.classList.remove("drag-over"));
      zone.addEventListener("drop", (event) => {
        event.preventDefault();
        zone.classList.remove("drag-over");
        const card = state.cardByName.get(event.dataTransfer.getData("text/plain"));
        if (card) addCard(card, zone.dataset.zone);
      });
    });
  }

  function setupEvents() {
    let searchFrame = 0;
    const scheduleSearch = () => {
      cancelAnimationFrame(searchFrame);
      searchFrame = requestAnimationFrame(() => renderResults());
    };
    $("#card-search").addEventListener("input", scheduleSearch);
    $("#type-filter").addEventListener("change", scheduleSearch);
    $("#attribute-filter").addEventListener("change", scheduleSearch);
    document.querySelectorAll(".deck-tab").forEach((tab) => tab.addEventListener("click", () => setActiveZone(tab.dataset.zone)));
    const changeDeck = (event) => loadSelectedDeck(event.target.value).catch((error) => {
      renderHandSlots([], "Không tải được deck");
      showToast(error.message, true);
    });
    $("#load-deck").addEventListener("click", () => changeDeck({ target: $("#deck-select") }));
    $("#deck-select").addEventListener("change", changeDeck);
    $("#deck_selector").addEventListener("change", changeDeck);
    $("#save-deck").addEventListener("click", saveDeck);
    $("#clear-deck").addEventListener("click", () => {
      state.main.clear(); state.extra.clear(); renderDeck(); showToast("Deck hiện tại đã được xóa.");
    });
    $("#generate-ai-route").addEventListener("click", generateAiRoute);
    $("#benchmark-route").addEventListener("click", benchmarkRoute);
    $("#scan-once").addEventListener("click", scanOnce);
    $("#toggle-vision").addEventListener("click", toggleVision);
    $("#use-detected-hand").addEventListener("click", () => {
      const hand = state.detectedHand.slice(0, 5);
      if (hand.length !== 5) {
        showToast("Nhận diện chưa đủ 5 lá bài.", true);
        return;
      }
      const slots = Array.from(document.querySelectorAll(".hand-card-select"));
      const missing = hand.find((name) => !Array.from(slots[0].options).some((option) => option.value === name));
      if (missing) {
        showToast(`${missing} không nằm trong Main Deck đang chọn.`, true);
        return;
      }
      hand.forEach((name, index) => { slots[index].value = name; });
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "/" && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) {
        event.preventDefault(); $("#card-search").focus();
      }
    });
    setupDropZones();
  }

  async function init() {
    setupEvents();
    await loadDeckIndex();
    try {
      await loadSelectedDeck($("#deck_selector").value, { showLoading: false });
    } catch (error) {
      renderHandSlots([], "Không tải được deck");
      showToast(error.message, true);
    }
    const health = await api("/api/health");
    $("#app-status").textContent = health.status === "ok" ? "ENGINE READY" : "CATALOG DEGRADED";
    $("#catalog-count").textContent = `${health.cards.toLocaleString()} cards`;
    const cards = await api("/api/cards");
    state.cards = cards;
    state.cardByName = new Map(cards.map((card) => [card.name, card]));
    if (typeof window.Fuse === "function") {
      state.fuse = new window.Fuse(cards, {
        keys: [{ name: "name", weight: 0.72 }, { name: "archetype", weight: 0.20 }, { name: "race_type", weight: 0.08 }],
        threshold: 0.32,
        ignoreLocation: true,
        minMatchCharLength: 2,
        shouldSort: true,
      });
      $("#search-engine").textContent = "Fuse.js 7.5 fuzzy search";
    } else {
      $("#search-engine").textContent = "Local fuzzy fallback (Fuse CDN unavailable)";
    }
    renderResults("");
    await checkVisionStatus();
  }

  function boot() {
    init().catch((error) => {
      $("#app-status").textContent = "STARTUP ERROR";
      $("#card-results").textContent = error.message;
      showToast(error.message, true);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }
})();
