(() => {
  "use strict";

  const FALLBACK_CONFIG = {
    version: "1.0.0",
    clientVersion: "2.1.0",
    maxChars: 2500,
    maxChunk: 500,
    adjustmentMin: -50,
    adjustmentMax: 50,
    rateChunks: 40,
    rateWindow: 900,
  };

  const STORAGE_KEY = "murf-web-settings-v1";
  const SAMPLE_TEXT =
    "Welcome to the public voice lab. [pause 0.5s] " +
    "Pick a voice, shape the delivery, then press Generate. " +
    "Long scripts are split into 500-character requests and stitched " +
    "back into one MP3, so you can paste a whole scene and walk away.";

  const state = {
    config: { ...FALLBACK_CONFIG },
    voices: [],
    savedVoice: "",
    busy: false,
    controller: null,
    audioUrl: null,
  };

  const el = {};

  document.addEventListener("DOMContentLoaded", init);

  async function init() {
    cacheElements();
    bindEvents();
    updateCounts();

    const saved = loadSettings();
    applySavedSliders(saved);
    state.savedVoice = typeof saved.voice === "string" ? saved.voice : "";

    try {
      const [config, payload] = await Promise.all([
        fetchJson("/api/config"),
        fetchJson("/api/voices"),
      ]);

      state.config = { ...FALLBACK_CONFIG, ...config };
      state.voices = Array.isArray(payload.voices) ? payload.voices : [];
      populateLocales();
      renderVoiceList();
      updateCounts();
      syncGenerateButton();
      setStatus(
        `Loaded ${state.voices.length} voices. Ready when you are.`,
        "neutral",
      );
    } catch (error) {
      setStatus(error.message || "Could not load the voice catalog.", "error");
      syncGenerateButton();
    }
  }

  function cacheElements() {
    const ids = [
      "text",
      "charCount",
      "maxChars",
      "chunkEstimate",
      "charProgress",
      "sample",
      "clear",
      "pauseLength",
      "insertPause",
      "generate",
      "generateLabel",
      "spinner",
      "cancel",
      "status",
      "voiceSearch",
      "tierFilter",
      "localeFilter",
      "genderFilter",
      "voiceSelect",
      "voiceCount",
      "voiceInfo",
      "styleSelect",
      "localeInput",
      "rate",
      "rateValue",
      "pitch",
      "pitchValue",
      "resetControls",
      "result",
      "download",
      "audio",
      "resultVoice",
      "resultChunks",
      "resultSize",
      "resultTime",
    ];
    for (const id of ids) {
      el[id] = document.getElementById(id);
    }
  }

  function bindEvents() {
    el.text.addEventListener("input", updateCounts);

    for (const filter of [
      el.voiceSearch,
      el.tierFilter,
      el.localeFilter,
      el.genderFilter,
    ]) {
      filter.addEventListener("input", renderVoiceList);
      filter.addEventListener("change", renderVoiceList);
    }

    el.voiceSelect.addEventListener("change", () => {
      state.savedVoice = el.voiceSelect.value;
      updateVoiceInfo();
      persistSettings();
    });

    el.rate.addEventListener("input", () => {
      updateRange(el.rate, el.rateValue);
      persistSettings();
    });
    el.pitch.addEventListener("input", () => {
      updateRange(el.pitch, el.pitchValue);
      persistSettings();
    });

    el.sample.addEventListener("click", () => {
      el.text.value = SAMPLE_TEXT;
      updateCounts();
      el.text.focus();
    });
    el.clear.addEventListener("click", clearComposer);
    el.insertPause.addEventListener("click", insertPause);
    el.generate.addEventListener("click", generate);
    el.cancel.addEventListener("click", cancelGeneration);
    el.resetControls.addEventListener("click", resetControls);

    document.addEventListener("keydown", (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
        event.preventDefault();
        generate();
      }
    });

    window.addEventListener("beforeunload", releaseAudio);
  }

  async function fetchJson(url) {
    const response = await fetch(url, {
      headers: { Accept: "application/json" },
    });
    if (!response.ok) {
      const error = await readError(response);
      throw new Error(error.message);
    }
    return response.json();
  }

  function filteredVoices() {
    const query = el.voiceSearch.value.trim().toLowerCase();
    const tier = el.tierFilter.value;
    const locale = el.localeFilter.value;
    const gender = el.genderFilter.value;

    return state.voices.filter((voice) => {
      if (tier && voice.tier !== tier) {
        return false;
      }
      if (locale && voice.locale !== locale) {
        return false;
      }
      if (gender && voice.gender !== gender) {
        return false;
      }
      if (!query) {
        return true;
      }
      const haystack = [
        voice.name,
        voice.apiName,
        voice.locale,
        voice.language,
        voice.gender,
        ...(voice.styles || []),
      ]
        .join(" ")
        .toLowerCase();
      return haystack.includes(query);
    });
  }

  function populateLocales() {
    const locales = new Map();
    for (const voice of state.voices) {
      if (!voice.locale || locales.has(voice.locale)) {
        continue;
      }
      locales.set(voice.locale, voice.language || "");
    }

    el.localeFilter.replaceChildren(
      new Option("All locales", ""),
      ...Array.from(locales.entries())
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([locale, language]) => {
          const label = language ? `${locale} — ${language}` : locale;
          return new Option(label, locale);
        }),
    );
  }

  function renderVoiceList() {
    const previous = el.voiceSelect.value;
    const voices = filteredVoices();
    const groups = new Map();

    for (const voice of voices) {
      const locale = voice.locale || "Other";
      if (!groups.has(locale)) {
        groups.set(locale, []);
      }
      groups.get(locale).push(voice);
    }

    const fragment = document.createDocumentFragment();
    for (const locale of Array.from(groups.keys()).sort((a, b) => a.localeCompare(b))) {
      const groupVoices = groups.get(locale);
      const language = groupVoices[0]?.language || "";
      const group = document.createElement("optgroup");
      group.label = language ? `${locale} · ${language}` : locale;

      for (const voice of groupVoices) {
        const tier = voice.tier === "PREMIUM" ? " · premium" : "";
        const option = new Option(
          `${voice.name} — ${voice.gender || "voice"}${tier}`,
          voiceKey(voice),
        );
        group.append(option);
      }
      fragment.append(group);
    }

    el.voiceSelect.replaceChildren(fragment);
    if (!voices.length) {
      el.voiceSelect.append(new Option("No voices match these filters", ""));
      el.voiceCount.textContent = `0 of ${state.voices.length}`;
      el.voiceInfo.replaceChildren(
        placeholder("Try a different search or clear the filters."),
      );
      el.styleSelect.replaceChildren(new Option("Default", ""));
      el.styleSelect.disabled = true;
      return;
    }

    const wanted = [previous, state.savedVoice].find(
      (value) => value && voices.some((voice) => voiceKey(voice) === value),
    );
    const preferred =
      voices.find((voice) => voice.apiName === "en-US-natalie") || voices[0];
    const selected = wanted || voiceKey(preferred);
    el.voiceSelect.value = selected;
    state.savedVoice = selected;
    el.voiceCount.textContent = `${voices.length} of ${state.voices.length}`;
    updateVoiceInfo();
  }

  function updateVoiceInfo() {
    const voice = selectedVoice();
    if (!voice) {
      el.voiceInfo.replaceChildren(placeholder("Choose a voice."));
      el.styleSelect.replaceChildren(new Option("Default", ""));
      el.styleSelect.disabled = true;
      return;
    }

    const row = document.createElement("div");
    row.className = "voice-title-row";

    const copy = document.createElement("div");
    const name = document.createElement("p");
    name.className = "voice-name";
    name.textContent = voice.name;

    const meta = document.createElement("p");
    meta.className = "voice-meta";
    meta.textContent = [voice.locale, voice.language, voice.gender]
      .filter(Boolean)
      .join(" · ");

    copy.append(name, meta);

    const badge = document.createElement("span");
    badge.className =
      voice.tier === "PREMIUM" ? "tier-badge premium" : "tier-badge";
    badge.textContent = voice.tier === "PREMIUM" ? "PREMIUM" : "FREE";

    row.append(copy, badge);

    const styles = document.createElement("p");
    styles.className = "voice-styles";
    styles.textContent = voice.styles?.length
      ? `Styles: ${voice.styles.join(", ")}`
      : "No named styles for this voice.";

    el.voiceInfo.replaceChildren(row, styles);
    el.localeInput.placeholder = voice.locale || "en-US";

    const previousStyle = el.styleSelect.value;
    el.styleSelect.replaceChildren(new Option("Default", ""));
    for (const style of voice.styles || []) {
      el.styleSelect.append(new Option(style, style));
    }
    const hasPrevious = Array.from(el.styleSelect.options).some(
      (option) => option.value === previousStyle,
    );
    el.styleSelect.value = hasPrevious ? previousStyle : "";
    el.styleSelect.disabled = !(voice.styles || []).length;
  }

  function selectedVoice() {
    return state.voices.find((voice) => voiceKey(voice) === el.voiceSelect.value);
  }

  function voiceKey(voice) {
    return voice.apiName || voice.voiceId || voice.name || "";
  }

  function placeholder(message) {
    const p = document.createElement("p");
    p.className = "voice-info-placeholder";
    p.textContent = message;
    return p;
  }

  function updateCounts() {
    const units = el.text.value.length;
    const max = state.config.maxChars;
    const chunks = units ? Math.ceil(units / state.config.maxChunk) : 0;
    const over = units > max;

    el.charCount.textContent = units.toLocaleString();
    el.maxChars.textContent = max.toLocaleString();
    el.charProgress.max = max;
    el.charProgress.value = Math.min(units, max);
    el.charCount.classList.toggle("over-limit", over);
    el.charProgress.classList.toggle("over-limit", over);
    el.chunkEstimate.textContent = `${chunks} request${chunks === 1 ? "" : "s"}`;
    if (over) {
      el.chunkEstimate.textContent = `over by ${(units - max).toLocaleString()}`;
    }
    syncGenerateButton();
  }

  function syncGenerateButton() {
    const hasText = el.text.value.trim().length > 0;
    const over = el.text.value.length > state.config.maxChars;
    el.generate.disabled =
      state.busy || !state.voices.length || !hasText || over;
  }

  function updateRange(input, output) {
    const value = Number(input.value);
    output.value = value > 0 ? `+${value}` : String(value);
    output.textContent = output.value;
  }

  function applySavedSliders(saved) {
    const clamp = (value, fallback) => {
      if (!Number.isInteger(value)) {
        return fallback;
      }
      return Math.min(
        state.config.adjustmentMax,
        Math.max(state.config.adjustmentMin, value),
      );
    };
    el.rate.value = String(clamp(saved.rate, 0));
    el.pitch.value = String(clamp(saved.pitch, 0));
    updateRange(el.rate, el.rateValue);
    updateRange(el.pitch, el.pitchValue);
  }

  function loadSettings() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) {
        return {};
      }
      const parsed = JSON.parse(raw);
      return parsed && typeof parsed === "object" ? parsed : {};
    } catch {
      return {};
    }
  }

  function persistSettings() {
    try {
      localStorage.setItem(
        STORAGE_KEY,
        JSON.stringify({
          voice: el.voiceSelect.value,
          rate: Number(el.rate.value),
          pitch: Number(el.pitch.value),
        }),
      );
    } catch {
      /* Storage can be unavailable in private modes. */
    }
  }

  function insertPause() {
    const tag = `[pause ${el.pauseLength.value}s]`;
    const start = el.text.selectionStart ?? el.text.value.length;
    const end = el.text.selectionEnd ?? start;
    el.text.setRangeText(tag, start, end, "end");
    el.text.focus();
    updateCounts();
  }

  function resetControls() {
    el.voiceSearch.value = "";
    el.tierFilter.value = "";
    el.localeFilter.value = "";
    el.genderFilter.value = "";
    el.styleSelect.value = "";
    el.localeInput.value = "";
    el.rate.value = "0";
    el.pitch.value = "0";
    updateRange(el.rate, el.rateValue);
    updateRange(el.pitch, el.pitchValue);

    renderVoiceList();
    const preferred =
      state.voices.find((voice) => voice.apiName === "en-US-natalie") ||
      state.voices[0];
    if (preferred) {
      el.voiceSelect.value = voiceKey(preferred);
      state.savedVoice = el.voiceSelect.value;
      updateVoiceInfo();
    }
    persistSettings();
    setStatus("Controls reset. Ready.", "neutral");
  }

  function clearComposer() {
    el.text.value = "";
    updateCounts();
    clearResult();
    setStatus("Cleared. Ready when you are.", "neutral");
    el.text.focus();
  }

  function cancelGeneration() {
    if (!state.controller) {
      return;
    }
    state.controller.abort();
    setStatus("Cancelling…", "neutral");
  }

  async function generate() {
    if (state.busy) {
      return;
    }

    const text = el.text.value.trim();
    if (!text) {
      setStatus("Add some text first.", "error");
      return;
    }
    if (el.text.value.length > state.config.maxChars) {
      setStatus(
        `Shorten the script to ${state.config.maxChars.toLocaleString()} characters.`,
        "error",
      );
      return;
    }

    const voice = selectedVoice();
    if (!voice) {
      setStatus("Choose a voice first.", "error");
      return;
    }

    const controller = new AbortController();
    state.controller = controller;
    state.busy = true;
    setBusy(true);

    const chunks = Math.max(1, Math.ceil(el.text.value.length / state.config.maxChunk));
    setStatus(
      `Sending ${chunks} request${chunks === 1 ? "" : "s"} to Murf…`,
      "working",
    );

    const started = performance.now();

    try {
      const response = await fetch("/api/synthesize", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "audio/mpeg",
        },
        body: JSON.stringify({
          text: el.text.value,
          voice: voiceKey(voice),
          style: el.styleSelect.value || undefined,
          rate: Number(el.rate.value),
          pitch: Number(el.pitch.value),
          locale: el.localeInput.value.trim() || undefined,
        }),
        signal: controller.signal,
      });

      if (!response.ok) {
        const error = await readError(response);
        const wait = error.retryAfter
          ? ` Try again in ${formatWait(error.retryAfter)}.`
          : "";
        throw new Error(`${error.message}${wait}`);
      }

      const blob = await response.blob();
      if (!blob.size) {
        throw new Error("The server returned an empty clip.");
      }

      const returnedChunks = Number(response.headers.get("X-Murf-Chunks")) || chunks;
      const serverBytes = Number(response.headers.get("X-Murf-Bytes")) || blob.size;
      const total = (performance.now() - started) / 1000;

      showResult(blob, voice, returnedChunks, serverBytes, total);
      setStatus(
        `Generated ${returnedChunks} request${returnedChunks === 1 ? "" : "s"} in ${total.toFixed(1)}s.`,
        "success",
      );
    } catch (error) {
      if (error.name === "AbortError") {
        setStatus("Cancelled. Ready when you are.", "neutral");
      } else {
        setStatus(error.message || "Generation failed.", "error");
      }
    } finally {
      if (state.controller === controller) {
        state.controller = null;
        state.busy = false;
        setBusy(false);
      }
      syncGenerateButton();
    }
  }

  function setBusy(busy) {
    el.cancel.hidden = !busy;
    el.spinner.hidden = !busy;
    el.generateLabel.textContent = busy ? "Generating…" : "Generate voice";
    syncGenerateButton();
  }

  function showResult(blob, voice, chunks, bytes, elapsed) {
    releaseAudio();
    state.audioUrl = URL.createObjectURL(blob);

    el.audio.src = state.audioUrl;
    el.download.href = state.audioUrl;
    el.download.download = `murf-${safeName(voice.apiName || voice.voiceId)}.mp3`;
    el.resultVoice.textContent = `${voice.name} · ${voice.locale}`;
    el.resultChunks.textContent = `${chunks} request${chunks === 1 ? "" : "s"}`;
    el.resultSize.textContent = formatBytes(bytes || blob.size);
    el.resultTime.textContent = `${elapsed.toFixed(1)}s`;
    el.result.hidden = false;
    el.audio.play().catch(() => {
      /* Autoplay can be blocked; the controls are still there. */
    });
    el.result.scrollIntoView({ block: "nearest" });
  }

  function clearResult() {
    releaseAudio();
    el.audio.removeAttribute("src");
    el.audio.load();
    el.download.href = "#";
    el.download.download = "murf-speech.mp3";
    el.result.hidden = true;
    el.resultVoice.textContent = "—";
    el.resultChunks.textContent = "—";
    el.resultSize.textContent = "—";
    el.resultTime.textContent = "—";
  }

  function releaseAudio() {
    if (state.audioUrl) {
      URL.revokeObjectURL(state.audioUrl);
      state.audioUrl = null;
    }
  }

  function setStatus(message, tone) {
    el.status.textContent = message;
    el.status.dataset.tone = tone || "neutral";
  }

  async function readError(response) {
    const text = await response.text();
    try {
      const payload = JSON.parse(text);
      return {
        message: payload.message || payload.error || `Request failed (${response.status}).`,
        retryAfter: Number(payload.retry_after) || 0,
      };
    } catch {
      return {
        message: text || `Request failed (${response.status}).`,
        retryAfter: 0,
      };
    }
  }

  function formatWait(seconds) {
    const rounded = Math.max(1, Math.round(seconds));
    if (rounded < 60) {
      return `${rounded}s`;
    }
    return `${Math.ceil(rounded / 60)} min`;
  }

  function formatBytes(bytes) {
    if (bytes < 1024) {
      return `${bytes} B`;
    }
    if (bytes < 1024 * 1024) {
      return `${(bytes / 1024).toFixed(1)} KB`;
    }
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  function safeName(value) {
    return String(value || "speech")
      .replace(/[^A-Za-z0-9_-]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 60) || "speech";
  }
})();
