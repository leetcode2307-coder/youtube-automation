/* Shorts Studio Frontend Controller */

const AUTO_ASSEMBLE = Boolean(window.__STUDIO_CONFIG__?.autoAssemble);
const AUTO_OPEN_REVIEW = Boolean(window.__STUDIO_CONFIG__?.autoOpenReview);

function getPreferredTheme() {
  try {
    const saved = localStorage.getItem("studio-theme");
    if (saved === "dark" || saved === "light") return saved;
  } catch (_e) {}
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function applyTheme(theme) {
  const active = theme === "dark" ? "dark" : "light";
  document.documentElement.setAttribute("data-theme", active);
  try {
    localStorage.setItem("studio-theme", active);
  } catch (_e) {}
  const label = document.getElementById("themeToggleLabel");
  const btn = document.getElementById("themeToggleBtn");
  if (label) label.textContent = active === "dark" ? "Light Mode" : "Dark Mode";
  if (btn) btn.setAttribute("aria-pressed", String(active === "dark"));
}

function toggleTheme() {
  const current = document.documentElement.getAttribute("data-theme") || getPreferredTheme();
  applyTheme(current === "dark" ? "light" : "dark");
}

function copyText(text, label = "Text", triggerBtn = null) {
  navigator.clipboard?.writeText(text || "");
  showToast(label + " copied to clipboard.", "success");
  if (triggerBtn) {
    const prev = triggerBtn.dataset.origLabel || triggerBtn.textContent;
    triggerBtn.dataset.origLabel = prev;
    triggerBtn.textContent = "Copied!";
    clearTimeout(triggerBtn.__copyTimer);
    triggerBtn.__copyTimer = setTimeout(() => {
      triggerBtn.textContent = prev;
    }, 1500);
  }
}

const STYLE_MEMORY_PRESETS = {
  suspense:
    "Open with an unexpected 2-second paradox or visual contrast hook, moody dramatic rim lighting, slow push-in shot to character reactions, escalating curiosity beat in scene 2, and a crisp twist revelation in the final scene.",
  warm:
    "Warm golden-hour storytelling tone, grounded everyday character emotion, gentle pacing, clear relatable moral takeaway without preaching, ending with a heartfelt single-line reflection.",
  tech:
    "High-energy visual explainer: hook viewers with a concrete pain point in 2 seconds, kinetic close-up-to-wide transitions, clear visual analogy in middle scenes, and a sharp futuristic payoff frame.",
};

function applyStylePreset(presetKey) {
  const box = document.getElementById("quickStyleExamples");
  if (!box || !STYLE_MEMORY_PRESETS[presetKey]) return;
  box.value = STYLE_MEMORY_PRESETS[presetKey];
  box.focus();
  showToast("Applied '" + presetKey + "' style example (optional).", "info");
}

function clearQuickStyleMemory() {
  const box = document.getElementById("quickStyleExamples");
  if (box) {
    box.value = "";
    box.focus();
  }
  showToast("Cleared optional style examples for this run.", "info");
}

function syncTempControl(rawVal) {
  const parsed = parseFloat(rawVal);
  if (Number.isNaN(parsed)) return;
  const clamped = Math.max(0, Math.min(1.5, parsed));
  const numInput = document.getElementById("cfg-llm_temperature");
  const rangeInput = document.getElementById("cfg-llm_temperature_range");
  if (numInput) numInput.value = clamped.toFixed(2);
  if (rangeInput) rangeInput.value = Math.max(0.2, Math.min(1.3, clamped)).toFixed(2);
}

const SECTION_TITLES = {
  overview: "Overview",
  videos: "Videos & Pipeline",
  ledger: "Publishing Ledger",
  schedule: "Schedule & Automation",
  activity: "Activity & Logs",
  settings: "Settings & Health",
};

function showToast(message, type = "info") {
  const el = document.getElementById("toast");
  if (!el) return;
  el.textContent = message;
  el.className = "toast show" + (type === "error" ? " error" : type === "success" ? " success" : "");
  clearTimeout(window.__toastTimer);
  window.__toastTimer = setTimeout(() => {
    el.className = "toast";
  }, 4800);
}

function toggleSidebar(forceState) {
  const sidebar = document.getElementById("sidebar");
  const scrim = document.getElementById("sidebarScrim");
  const toggleBtn = document.getElementById("menuToggleBtn");
  if (!sidebar || !scrim) return;
  const open = typeof forceState === "boolean" ? forceState : !sidebar.classList.contains("open");
  sidebar.classList.toggle("open", open);
  scrim.classList.toggle("open", open);
  if (toggleBtn) toggleBtn.setAttribute("aria-expanded", String(open));
}

function navigateTo(sectionId, updateHash = true) {
  if (!SECTION_TITLES[sectionId]) sectionId = "overview";
  document.querySelectorAll(".section-panel").forEach((panel) => {
    panel.classList.toggle("active", panel.id === "section-" + sectionId);
  });
  document.querySelectorAll(".nav-item[data-section]").forEach((btn) => {
    const active = btn.getAttribute("data-section") === sectionId;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-current", active ? "page" : "false");
  });
  const titleEl = document.getElementById("topbarTitle");
  if (titleEl) titleEl.textContent = SECTION_TITLES[sectionId];
  if (updateHash && window.location.hash !== "#" + sectionId) {
    history.replaceState(null, "", "#" + sectionId);
  }
  if (window.innerWidth <= 860) toggleSidebar(false);
}

function filterRuns(filterKey, btnEl) {
  document.querySelectorAll(".filter-btn[data-filter]").forEach((b) => {
    b.setAttribute("aria-pressed", String(b === btnEl));
  });
  document.querySelectorAll(".run-card[data-filter-group]").forEach((card) => {
    const groups = (card.getAttribute("data-filter-group") || "").split(",");
    const show = filterKey === "all" || groups.includes(filterKey);
    card.style.display = show ? "" : "none";
  });
}

function setBtnBusy(btn, busy, busyLabel) {
  if (!btn) return;
  if (busy) {
    if (!btn.dataset.origHtml) btn.dataset.origHtml = btn.innerHTML;
    btn.disabled = true;
    btn.classList.add("is-busy");
    btn.textContent = "";
    const svgNS = "http://www.w3.org/2000/svg";
    const spinner = document.createElementNS(svgNS, "svg");
    spinner.setAttribute("class", "btn-spinner");
    spinner.setAttribute("viewBox", "0 0 24 24");
    spinner.setAttribute("fill", "none");
    spinner.setAttribute("stroke", "currentColor");
    spinner.setAttribute("stroke-width", "2.4");
    spinner.setAttribute("aria-hidden", "true");
    const arc = document.createElementNS(svgNS, "path");
    arc.setAttribute("d", "M21 12a9 9 0 1 1-6.219-8.56");
    spinner.appendChild(arc);
    btn.appendChild(spinner);
    btn.appendChild(document.createTextNode(" " + busyLabel));
  } else {
    btn.disabled = false;
    btn.classList.remove("is-busy");
    if (btn.dataset.origHtml) {
      btn.innerHTML = btn.dataset.origHtml;
      delete btn.dataset.origHtml;
    }
  }
}

async function generateShort(slot, btnEl) {
  const nicheInput = document.getElementById("quickNiche");
  const langSelect = document.getElementById("quickLanguage");
  const topicInput = document.getElementById("quickTopic");
  const styleInput = document.getElementById("quickStyleExamples");
  const rememberCheck = document.getElementById("quickRememberStyle");
  const autoFlowCheck = document.getElementById("quickAutoFlow");

  const payload = {
    slot,
    niche: nicheInput ? nicheInput.value.trim() || null : null,
    language: langSelect ? langSelect.value || null : null,
    topic: topicInput ? topicInput.value.trim() || null : null,
    style_examples: styleInput ? styleInput.value.trim() : null,
    remember_style: Boolean(rememberCheck && rememberCheck.checked),
    auto_run_flow: Boolean(autoFlowCheck && autoFlowCheck.checked),
  };

  setBtnBusy(btnEl, true, "Creating script...");
  showToast("Generating your " + slot + " video script and scene prompts...");
  try {
    const res = await fetch("/api/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (res.ok) {
      const flowNote = data.flow_job_status ? " + queued Google Flow Playwright bot!" : "";
      showToast("Created new video brief: " + data.title + flowNote, "success");
      setTimeout(() => {
        window.location.hash = "#videos";
        window.location.reload();
      }, 900);
    } else {
      showToast("Generation failed: " + (data.detail || "Please check Groq settings"), "error");
    }
  } catch (err) {
    showToast("Network error: " + err, "error");
  } finally {
    setBtnBusy(btnEl, false);
  }
}

function openClipPicker(runId) {
  const input = document.getElementById("clips-input-" + runId);
  if (input) input.click();
}

function openVoiceoverPicker(runId) {
  const input = document.getElementById("vo-input-" + runId);
  if (input) input.click();
}

function triggerDropzonePick(ev, runId) {
  if (ev.target && ev.target.closest("button")) return;
  openClipPicker(runId);
}

function handleDragEnter(ev) {
  ev.preventDefault();
  const zone = ev.currentTarget;
  const depth = (parseInt(zone.dataset.dragDepth || "0", 10) || 0) + 1;
  zone.dataset.dragDepth = String(depth);
  zone.classList.add("drag-over");
}

function handleDragLeave(ev) {
  ev.preventDefault();
  const zone = ev.currentTarget;
  const depth = Math.max(0, (parseInt(zone.dataset.dragDepth || "1", 10) || 1) - 1);
  zone.dataset.dragDepth = String(depth);
  if (depth === 0) zone.classList.remove("drag-over");
}

function handleDrop(ev, runId) {
  ev.preventDefault();
  const zone = ev.currentTarget;
  zone.dataset.dragDepth = "0";
  zone.classList.remove("drag-over");
  if (ev.dataTransfer && ev.dataTransfer.files.length) {
    uploadFileList(runId, ev.dataTransfer.files);
  }
}

function uploadClips(runId, inputEl) {
  if (inputEl.files && inputEl.files.length) {
    uploadFileList(runId, inputEl.files);
    inputEl.value = "";
  }
}

function uploadVoiceoverFile(runId, inputEl) {
  if (!inputEl.files || !inputEl.files.length) return;
  const raw = inputEl.files[0];
  inputEl.value = "";
  if (!raw.name.toLowerCase().endsWith(".mp3") || raw.size < 1024) {
    showToast("Please select a valid .mp3 voiceover file (>= 1 KB).", "error");
    return;
  }
  const renamed = new File([raw], "voiceover.mp3", { type: raw.type || "audio/mpeg" });
  uploadFileList(runId, [renamed]);
}

function formatBytesMB(bytes) {
  return (bytes / (1024 * 1024)).toFixed(1) + " MB";
}

function renderUploadProgress(banner, text, percent = null, stateTone = "info") {
  if (!banner) return;
  banner.style.display = "block";
  banner.className = "progress-banner" + (stateTone === "error" ? " is-error" : stateTone === "success" ? " is-success" : "");
  banner.textContent = "";
  const label = document.createElement("div");
  label.textContent = text;
  banner.appendChild(label);
  if (typeof percent === "number") {
    const track = document.createElement("div");
    track.className = "progress-track";
    const fill = document.createElement("div");
    fill.className = "progress-fill";
    fill.style.width = Math.max(0, Math.min(100, percent)) + "%";
    track.appendChild(fill);
    banner.appendChild(track);
  }
}

function createUploadQueueUI(runId, fileArr) {
  const container = document.getElementById("upload-queue-" + runId);
  if (!container) return [];
  container.style.display = "flex";
  container.textContent = "";
  return fileArr.map((file) => {
    const row = document.createElement("div");
    row.className = "upload-queue-item";

    const nameSpan = document.createElement("span");
    nameSpan.className = "mono";
    nameSpan.textContent = file.name + " (" + formatBytesMB(file.size || 0) + ")";

    const statusBadge = document.createElement("span");
    statusBadge.className = "badge badge-neutral";
    statusBadge.textContent = "Queued";

    row.appendChild(nameSpan);
    row.appendChild(statusBadge);
    container.appendChild(row);
    return statusBadge;
  });
}

function setQueueItemState(badgeEl, text, tone) {
  if (!badgeEl) return;
  badgeEl.className = "badge badge-" + (tone || "neutral");
  badgeEl.textContent = text;
}

function refreshRunClipsUi(runId, clipsData) {
  const station = document.getElementById("upload-station-" + runId);
  const clipList = document.getElementById("clip-list-" + runId);
  const countEl = document.getElementById("clips-count-" + runId);
  if (!station || !clipList || !clipsData) return;

  const readySet = new Set(
    (clipsData.clips || [])
      .map((name) => {
        const m = String(name).match(/scene_?0*(\d+)/i);
        return m ? parseInt(m[1], 10) : 0;
      })
      .filter((n) => n > 0)
  );

  const sceneChips = clipList.querySelectorAll(".clip-chip[data-scene]");
  sceneChips.forEach((chip) => {
    const num = parseInt(chip.getAttribute("data-scene") || "0", 10);
    const isReady = readySet.has(num);
    chip.classList.toggle("ready", isReady);
    chip.textContent = chip.textContent.replace(/^(Ready|Missing):\s*/i, isReady ? "Ready: " : "Missing: ");
  });

  if (countEl) {
    const totalScenes = parseInt(station.getAttribute("data-total-scenes") || String(sceneChips.length), 10);
    const readyCount = Array.from(sceneChips).filter((c) => c.classList.contains("ready")).length;
    countEl.textContent = readyCount + "/" + totalScenes + " ready";
  }

  if (clipsData.has_custom_voiceover) {
    const voChip = clipList.querySelector(".clip-chip[data-vo]");
    if (voChip) {
      voChip.classList.add("ready");
      voChip.setAttribute("data-vo", "custom");
      voChip.textContent = "Ready: voiceover.mp3 (Custom)";
    }
  }
}

function uploadSingleFileXhr(runId, file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/upload-clips/" + encodeURIComponent(runId), true);
    xhr.timeout = 180000;

    xhr.upload.onprogress = (ev) => {
      if (ev.lengthComputable && typeof onProgress === "function") {
        onProgress(ev.loaded, ev.total);
      }
    };

    xhr.onload = () => {
      let data = {};
      try {
        data = JSON.parse(xhr.responseText || "{}");
      } catch (_e) {
        data = {};
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        if (typeof onProgress === "function") onProgress(file.size, file.size);
        resolve(data);
      } else {
        reject(new Error(data.detail || "Upload failed (" + xhr.status + ")"));
      }
    };

    xhr.onerror = () => reject(new Error("Network error while uploading " + file.name));
    xhr.ontimeout = () => reject(new Error("Upload timed out for " + file.name));

    const formData = new FormData();
    formData.append("files", file);
    xhr.send(formData);
  });
}

async function uploadFileList(runId, files) {
  const fileArr = Array.from(files || []);
  if (!fileArr.length) return;

  const banner = document.getElementById("progress-" + runId);
  const setProgress = (msg, pct = null, tone = "info") => {
    renderUploadProgress(banner, msg, pct, tone);
  };

  // Client-side preflight validation so invalid files show clear inline feedback without uploading bytes
  for (const f of fileArr) {
    const lower = f.name.toLowerCase();
    const isZip = lower.endsWith(".zip");
    const isMp4 = lower.endsWith(".mp4");
    const isVo = lower === "voiceover.mp3";
    if (!isMp4 && !isVo && !isZip) {
      const msg = "Unsupported file '" + f.name + "'. Upload scene_01.mp4..scene_0N.mp4, a Google Flow .zip export, or voiceover.mp3.";
      setProgress(msg, null, "error");
      showToast(msg, "error");
      return;
    }
    if (isMp4 && !/scene_?0*\d+/i.test(f.name)) {
      const msg = "MP4 filename '" + f.name + "' must follow scene_01.mp4 naming convention (or upload the Google Flow .zip directly).";
      setProgress(msg, null, "error");
      showToast(msg, "error");
      return;
    }
    if (isZip && f.size < 22) {
      const msg = "ZIP archive '" + f.name + "' is empty or invalid.";
      setProgress(msg, null, "error");
      showToast(msg, "error");
      return;
    }
    if (!isZip && f.size < 1024) {
      const msg = "File '" + f.name + "' is too small or empty.";
      setProgress(msg, null, "error");
      showToast(msg, "error");
      return;
    }
  }

  const queueBadges = createUploadQueueUI(runId, fileArr);
  const totalBytes = fileArr.reduce((sum, f) => sum + (f.size || 0), 0);
  const loadedByFile = new Array(fileArr.length).fill(0);
  let completedCount = 0;

  const updateUploadUi = () => {
    const loadedTotal = loadedByFile.reduce((a, b) => a + b, 0);
    const pct = totalBytes > 0 ? Math.min(100, Math.round((loadedTotal / totalBytes) * 100)) : 0;
    setProgress(
      "Step 1 of 3: Uploading " +
        fileArr.length +
        " item(s) — " +
        pct +
        "% (" +
        formatBytesMB(loadedTotal) +
        " / " +
        formatBytesMB(totalBytes) +
        ") • " +
        completedCount +
        "/" +
        fileArr.length +
        " complete",
      pct,
      "info"
    );
  };

  updateUploadUi();
  showToast("Uploading " + fileArr.length + " item(s)...");

  try {
    const concurrency = Math.min(3, fileArr.length);
    let nextIdx = 0;

    async function worker() {
      while (nextIdx < fileArr.length) {
        const idx = nextIdx++;
        const file = fileArr[idx];
        const badge = queueBadges[idx];
        setQueueItemState(badge, "Uploading...", "info");
        try {
          const uploadRes = await uploadSingleFileXhr(runId, file, (loaded, total) => {
            const scaled = total > 0 ? Math.round((loaded / total) * (file.size || total)) : loaded;
            loadedByFile[idx] = Math.min(file.size || scaled, scaled);
            const filePct = total > 0 ? Math.min(100, Math.round((loaded / total) * 100)) : 0;
            setQueueItemState(badge, file.name.toLowerCase().endsWith(".zip") && filePct >= 100 ? "Extracting ZIP..." : filePct + "%", "info");
            updateUploadUi();
          });
          loadedByFile[idx] = file.size || loadedByFile[idx];
          completedCount += 1;
          const countInfo = Array.isArray(uploadRes.uploaded) && file.name.toLowerCase().endsWith(".zip")
            ? "Extracted " + uploadRes.uploaded.length + " file(s)"
            : "Uploaded";
          setQueueItemState(badge, countInfo, "success");
          updateUploadUi();
        } catch (fileErr) {
          setQueueItemState(badge, "Error", "danger");
          throw fileErr;
        }
      }
    }

    await Promise.all(Array.from({ length: concurrency }, () => worker()));

    const clipsRes = await fetch("/api/clips/" + encodeURIComponent(runId));
    const upData = await clipsRes.json();
    if (!clipsRes.ok) {
      const msg = "Upload verification failed: " + (upData.detail || "Unable to verify clips");
      setProgress(msg, null, "error");
      showToast(msg, "error");
      return;
    }

    refreshRunClipsUi(runId, upData);

    if (AUTO_ASSEMBLE && upData.missing_scenes && upData.missing_scenes.length === 0) {
      setProgress("Step 2 of 3: All clips uploaded. Preparing your 9:16 video and voiceover...", null, "info");
      showToast("All clips uploaded. Assembling video...", "info");
      const asmRes = await fetch("/api/assemble/" + encodeURIComponent(runId), { method: "POST" });
      const asmData = await asmRes.json();
      if (!asmRes.ok) {
        const msg = "Video assembly failed: " + (asmData.detail || "Check uploaded scene clips");
        setProgress(msg, null, "error");
        showToast(msg, "error");
        return;
      }

      if (AUTO_OPEN_REVIEW) {
        setProgress("Step 3 of 3: Video ready! Opening your review screen...", null, "success");
        const notRes = await fetch("/api/notify/" + encodeURIComponent(runId), { method: "POST" });
        const notData = await notRes.json();
        if (notRes.ok && notData.review_url) {
          window.location.assign(notData.review_url);
          return;
        }
      }
      setProgress("Video assembled and ready for review!", null, "success");
      showToast("Your video is ready for approval!", "success");
      setTimeout(() => window.location.reload(), 900);
    } else {
      const missing = (upData.missing_scenes || []).map((n) => "scene_" + String(n).padStart(2, "0") + ".mp4").join(", ");
      const msg = missing
        ? "Uploaded! Still missing: " + missing
        : "All scene clips ready! Click Assemble Video when ready.";
      setProgress(msg, null, missing ? "info" : "success");
      showToast(msg, "success");
    }
  } catch (err) {
    const msg = "Upload failed: " + (err?.message || err) + ". Check filenames and try again.";
    setProgress(msg, null, "error");
    showToast(msg, "error");
  }
}

async function assembleRun(runId, btnEl) {
  setBtnBusy(btnEl, true, "Preparing video...");
  showToast("Combining clips and voiceover with FFmpeg...");
  try {
    const res = await fetch("/api/assemble/" + encodeURIComponent(runId), { method: "POST" });
    const data = await res.json();
    if (res.ok) {
      if (AUTO_OPEN_REVIEW) {
        showToast("Video assembled. Opening review screen...", "success");
        await openReview(runId, true);
      } else {
        showToast("Video assembled and ready for review.", "success");
        setTimeout(() => window.location.reload(), 900);
      }
    } else {
      showToast("Assembly failed: " + (data.detail || "Check uploaded clips"), "error");
    }
  } catch (err) {
    showToast("Assembly error: " + err, "error");
  } finally {
    setBtnBusy(btnEl, false);
  }
}

async function openReview(runId, sameTab = false, btnEl = null) {
  if (btnEl) setBtnBusy(btnEl, true, "Opening...");
  try {
    const res = await fetch("/api/notify/" + encodeURIComponent(runId), { method: "POST" });
    const data = await res.json();
    if (res.ok && data.review_url) {
      if (sameTab) {
        window.location.assign(data.review_url);
      } else {
        const win = window.open(data.review_url, "_blank");
        if (!win) window.location.assign(data.review_url);
        else {
          showToast("Review screen opened in a new tab.", "success");
          setTimeout(() => window.location.reload(), 900);
        }
      }
    } else {
      showToast("Could not open review: " + (data.detail || "Assemble video first"), "error");
    }
  } catch (err) {
    showToast("Error: " + err, "error");
  } finally {
    if (btnEl) setBtnBusy(btnEl, false);
  }
}

async function saveRunEdits(runId, btnEl) {
  const splitCsv = (val) => val.split(",").map((s) => s.trim()).filter(Boolean);
  const payload = {
    title: document.getElementById("edit-title-" + runId).value.trim(),
    description: document.getElementById("edit-desc-" + runId).value.trim(),
    tags: splitCsv(document.getElementById("edit-tags-" + runId).value),
    hashtags: splitCsv(document.getElementById("edit-hashtags-" + runId).value),
    narration_full: document.getElementById("edit-narration-" + runId).value.trim(),
    privacy_status: document.getElementById("edit-privacy-" + runId).value,
    category_id: document.getElementById("edit-cat-" + runId).value,
    synthetic_media_disclosure: document.getElementById("edit-ai-" + runId).checked,
  };
  setBtnBusy(btnEl, true, "Saving...");
  try {
    const res = await fetch("/api/package/" + encodeURIComponent(runId), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (res.ok) {
      showToast("Video details saved.", "success");
      setTimeout(() => window.location.reload(), 800);
    } else {
      showToast("Save failed: " + (data.detail || "Check required fields"), "error");
    }
  } catch (err) {
    showToast("Network error: " + err, "error");
  } finally {
    setBtnBusy(btnEl, false);
  }
}

async function retryUpload(runId, btnEl) {
  setBtnBusy(btnEl, true, "Uploading...");
  showToast("Uploading video to YouTube...");
  try {
    const res = await fetch("/api/retry-upload/" + encodeURIComponent(runId), { method: "POST" });
    const data = await res.json();
    if (res.ok) {
      showToast("Uploaded (" + data.privacy_status + "): " + data.youtube_video_id, "success");
      setTimeout(() => window.location.reload(), 1000);
    } else {
      showToast("Upload failed: " + (data.detail || "Error"), "error");
    }
  } catch (err) {
    showToast("Upload error: " + err, "error");
  } finally {
    setBtnBusy(btnEl, false);
  }
}

async function deleteRun(runId) {
  if (!window.confirm("Delete run " + runId + " and its local video clips?")) return;
  try {
    const res = await fetch("/api/package/" + encodeURIComponent(runId), { method: "DELETE" });
    if (res.ok) {
      showToast("Run deleted.", "success");
      setTimeout(() => window.location.reload(), 600);
    } else {
      const data = await res.json();
      showToast("Delete failed: " + (data.detail || "Error"), "error");
    }
  } catch (err) {
    showToast("Error: " + err, "error");
  }
}

async function viewBrief(runId) {
  try {
    const res = await fetch("/api/brief/" + encodeURIComponent(runId));
    const text = await res.text();
    const modal = document.getElementById("briefModal");
    document.getElementById("modalTitle").textContent = "Visual Prompts — " + runId;
    document.getElementById("modalBody").textContent = text;
    modal.classList.add("open");
    document.getElementById("modalCloseBtn")?.focus();
  } catch (err) {
    showToast("Could not load prompts: " + err, "error");
  }
}

function closeModal() {
  document.getElementById("briefModal")?.classList.remove("open");
}

function copyBrief() {
  const text = document.getElementById("modalBody")?.textContent || "";
  navigator.clipboard.writeText(text);
  showToast("Prompts copied to clipboard.", "success");
}

function syncCronFromTime(slot) {
  const timeInput = document.getElementById("cfg-" + slot + "_time");
  const cronInput = document.getElementById("cfg-" + slot + "_cron");
  if (!timeInput || !cronInput || !timeInput.value.includes(":")) return;
  const [hh, mm] = timeInput.value.split(":");
  cronInput.value = parseInt(mm, 10) + " " + parseInt(hh, 10) + " * * *";
}

function syncTimeInputsFromScheduleForm() {
  const mTime = document.getElementById("sched-morning_time")?.value;
  const eTime = document.getElementById("sched-evening_time")?.value;
  const tz = document.getElementById("sched-timezone")?.value.trim();
  if (mTime && mTime.includes(":")) {
    const [hh, mm] = mTime.split(":");
    document.getElementById("cfg-morning_cron").value = parseInt(mm, 10) + " " + parseInt(hh, 10) + " * * *";
    document.getElementById("cfg-morning_time").value = mTime;
  }
  if (eTime && eTime.includes(":")) {
    const [hh, mm] = eTime.split(":");
    document.getElementById("cfg-evening_cron").value = parseInt(mm, 10) + " " + parseInt(hh, 10) + " * * *";
    document.getElementById("cfg-evening_time").value = eTime;
  }
  if (tz) document.getElementById("cfg-timezone").value = tz;
}

async function saveAllSettings(ev, fromScheduleForm = false) {
  if (ev) ev.preventDefault();
  if (fromScheduleForm) syncTimeInputsFromScheduleForm();

  const errBox = document.getElementById("settingsErrorSummary");
  if (errBox) errBox.style.display = "none";

  const payload = {
    studio_name: document.getElementById("cfg-studio_name").value.trim(),
    language: document.getElementById("cfg-language")?.value || "english",
    niche: document.getElementById("cfg-niche").value.trim(),
    niche_domains: document.getElementById("cfg-niche_domains").value.trim(),
    audience_tone: document.getElementById("cfg-audience_tone").value.trim(),
    default_cta: document.getElementById("cfg-default_cta").value.trim(),
    category_id: document.getElementById("cfg-category_id").value,
    min_duration_seconds: parseInt(document.getElementById("cfg-min_duration_seconds").value, 10),
    max_duration_seconds: parseInt(document.getElementById("cfg-max_duration_seconds").value, 10),
    groq_model: document.getElementById("cfg-groq_model").value.trim(),
    llm_temperature: parseFloat(document.getElementById("cfg-llm_temperature")?.value ?? "0.9"),
    prompt_style_examples: document.getElementById("cfg-prompt_style_examples")?.value.trim() || "",
    google_pro_email: document.getElementById("cfg-google_pro_email")?.value.trim() || "",
    flow_auto_automate: document.getElementById("cfg-flow_auto_automate")?.checked ?? false,
    tts_voice: document.getElementById("cfg-tts_voice").value.trim(),
    use_ai_voiceover: document.getElementById("cfg-use_ai_voiceover")?.checked ?? true,
    audio_mode: document.getElementById("cfg-audio_mode")?.value || "ai_voiceover",
    default_privacy_status: document.getElementById("cfg-default_privacy_status").value,
    timezone: document.getElementById("cfg-timezone").value.trim(),
    morning_cron: document.getElementById("cfg-morning_cron").value.trim(),
    evening_cron: document.getElementById("cfg-evening_cron").value.trim(),
    synthetic_media_default: document.getElementById("cfg-synthetic_media_default").checked,
    auto_assemble_on_upload: document.getElementById("cfg-auto_assemble_on_upload").checked,
    auto_open_review_on_assemble: document.getElementById("cfg-auto_open_review_on_assemble").checked,
    send_email_on_ready: document.getElementById("cfg-send_email_on_ready").checked,
  };

  try {
    const res = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (res.ok) {
      showToast("Settings saved successfully.", "success");
      setTimeout(() => window.location.reload(), 800);
    } else {
      const msg = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
      if (errBox) {
        errBox.textContent = "Could not save settings: " + msg;
        errBox.style.display = "block";
        errBox.focus();
      }
      showToast("Could not save settings.", "error");
    }
  } catch (err) {
    showToast("Network error: " + err, "error");
  }
}

async function refreshLogs() {
  const viewer = document.getElementById("technicalLogViewer");
  if (!viewer) return;
  try {
    const res = await fetch("/api/logs?limit=120");
    const data = await res.json();
    if (res.ok) {
      viewer.textContent = data.lines.length ? data.lines.join("\n") : "No log entries yet.";
    }
  } catch (err) {
    viewer.textContent = "Unable to load logs: " + err;
  }
}

async function uploadFlowSessionFile(inputEl) {
  if (!inputEl?.files?.length) return;
  const file = inputEl.files[0];
  inputEl.value = "";
  const email = document.getElementById("cfg-google_pro_email")?.value.trim() || "";
  if (!email) {
    showToast("Enter your Google Pro subscription email first.", "error");
    return;
  }
  const formData = new FormData();
  formData.append("email", email);
  formData.append("file", file);
  showToast("Uploading Playwright session for " + email + "...");
  try {
    const res = await fetch("/api/flow-session/upload", { method: "POST", body: formData });
    const data = await res.json();
    if (res.ok) {
      const badge = document.getElementById("flowSessionStatusBadge");
      const valid = Boolean(data.has_auth_cookie);
      if (badge) {
        badge.className = "badge badge-" + (valid ? "success" : "amber");
        badge.textContent = valid
          ? "Session Ready (" + (data.cookie_count || 0) + " cookies)"
          : "Incomplete Session (" + (data.cookie_count || 0) + " cookies — Missing Google Auth)";
      }
      showToast(
        valid
          ? "Saved Google Flow session for " + data.email + "."
          : "Uploaded " + (data.cookie_count || 0) + " cookie(s), but Google Sign-In cookies are missing. Re-run flow_login until Flow Studio opens.",
        valid ? "success" : "error"
      );
    } else {
      showToast("Session upload failed: " + (data.detail || "Invalid JSON"), "error");
    }
  } catch (err) {
    showToast("Upload error: " + err, "error");
  }
}

async function clearFlowSession() {
  const email = document.getElementById("cfg-google_pro_email")?.value.trim() || "";
  try {
    const res = await fetch("/api/flow-session?email=" + encodeURIComponent(email), { method: "DELETE" });
    if (res.ok) {
      const badge = document.getElementById("flowSessionStatusBadge");
      if (badge) {
        badge.className = "badge badge-amber";
        badge.textContent = "Session Missing — Link Account";
      }
      showToast("Cleared saved Google Flow session.", "info");
    }
  } catch (err) {
    showToast("Error clearing session: " + err, "error");
  }
}

const _flowPollTimers = {};

function pollFlowAutomation(runId) {
  clearTimeout(_flowPollTimers[runId]);
  const banner = document.getElementById("flow-job-" + runId);
  const btn = document.getElementById("flow-btn-" + runId);

  const tick = async () => {
    try {
      const res = await fetch("/api/flow-automate/" + encodeURIComponent(runId));
      const data = await res.json();
      if (!res.ok) return;
      const job = data.job || {};
      const status = job.status || "idle";
      const pct = typeof job.progress_percent === "number" ? job.progress_percent : 0;

      if (banner && status !== "idle") {
        const tone = status === "completed" ? "success" : status === "failed" || status === "auth_required" ? "error" : "info";
        renderUploadProgress(banner, job.message || status, status === "running" || status === "queued" ? pct : null, tone);
        if ((status === "failed" || status === "auth_required") && data.debug_screenshot_url) {
          const link = document.createElement("a");
          link.href = data.debug_screenshot_url;
          link.target = "_blank";
          link.style.display = "inline-block";
          link.style.marginTop = "6px";
          link.style.fontSize = "12px";
          link.textContent = "Inspect Browser Debug Screenshot";
          banner.appendChild(link);
        }
      }

      if (data.clips) {
        refreshRunClipsUi(runId, { clips: data.clips, has_custom_voiceover: data.has_custom_voiceover });
      }

      if (status === "queued" || status === "running") {
        _flowPollTimers[runId] = setTimeout(tick, 1800);
        return;
      }

      if (btn) setBtnBusy(btn, false);
      if (status === "completed") {
        showToast("Google Flow clips ready & extracted!", "success");
        setTimeout(() => window.location.reload(), 1000);
      } else if (status === "auth_required") {
        showToast("Google Flow login/session needed — upload session in Settings.", "error");
      } else if (status === "failed") {
        showToast("Google Flow run failed: " + (job.error || "See details in card"), "error");
      }
    } catch (_e) {
      _flowPollTimers[runId] = setTimeout(tick, 3000);
    }
  };

  _flowPollTimers[runId] = setTimeout(tick, 900);
}

async function runFlowAutomation(runId, btnEl) {
  const emailInput = document.getElementById("flow-email-" + runId);
  const email = emailInput ? emailInput.value.trim() : "";
  const banner = document.getElementById("flow-job-" + runId);
  setBtnBusy(btnEl, true, "Running Flow Bot...");
  renderUploadProgress(banner, "Starting Google Flow Playwright automation for " + (email || "configured account") + "...", 5, "info");
  try {
    const res = await fetch("/api/flow-automate/" + encodeURIComponent(runId), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ google_pro_email: email || null }),
    });
    const data = await res.json();
    if (res.ok || res.status === 202) {
      showToast("Playwright automation started for " + (data.google_pro_email || runId) + ".", "info");
      renderUploadProgress(banner, data.message || "Queued...", data.progress_percent || 5, "info");
      pollFlowAutomation(runId);
    } else {
      setBtnBusy(btnEl, false);
      const msg = data.detail || "Unable to start Google Flow automation";
      renderUploadProgress(banner, msg, null, "error");
      showToast(msg, "error");
    }
  } catch (err) {
    setBtnBusy(btnEl, false);
    const msg = "Network error starting Playwright automation: " + err;
    renderUploadProgress(banner, msg, null, "error");
    showToast(msg, "error");
  }
}

document.addEventListener("DOMContentLoaded", () => {
  applyTheme(getPreferredTheme());
  const initialHash = window.location.hash.replace("#", "");
  if (initialHash && SECTION_TITLES[initialHash]) {
    navigateTo(initialHash, false);
  }
  document.querySelectorAll("[data-flow-run]").forEach((el) => {
    const runId = el.getAttribute("data-flow-run");
    const status = el.getAttribute("data-flow-status");
    if (runId && (status === "queued" || status === "running")) {
      const btn = document.getElementById("flow-btn-" + runId);
      if (btn) setBtnBusy(btn, true, "Running Flow Bot...");
      pollFlowAutomation(runId);
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      closeModal();
      toggleSidebar(false);
    }
  });
});
