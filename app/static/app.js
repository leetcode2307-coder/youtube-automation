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

function copyText(text, label = "Text") {
  navigator.clipboard.writeText(text || "");
  showToast(label + " copied to clipboard.", "success");
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
    btn.dataset.origHtml = btn.innerHTML;
    btn.disabled = true;
    btn.textContent = busyLabel;
  } else {
    btn.disabled = false;
    if (btn.dataset.origHtml) btn.innerHTML = btn.dataset.origHtml;
  }
}

async function generateShort(slot, btnEl) {
  const nicheInput = document.getElementById("quickNiche");
  const langSelect = document.getElementById("quickLanguage");
  const topicInput = document.getElementById("quickTopic");
  const params = new URLSearchParams({ slot });
  if (nicheInput && nicheInput.value.trim()) params.set("niche", nicheInput.value.trim());
  if (langSelect && langSelect.value) params.set("language", langSelect.value);
  if (topicInput && topicInput.value.trim()) params.set("topic", topicInput.value.trim());

  setBtnBusy(btnEl, true, "Creating script...");
  showToast("Generating your " + slot + " video script and scene prompts...");
  try {
    const res = await fetch("/api/generate?" + params.toString(), { method: "POST" });
    const data = await res.json();
    if (res.ok) {
      showToast("Created new video brief: " + data.title, "success");
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

function handleDrop(ev, runId) {
  ev.preventDefault();
  ev.currentTarget.classList.remove("drag-over");
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

function formatBytesMB(bytes) {
  return (bytes / (1024 * 1024)).toFixed(1) + " MB";
}

function renderUploadProgress(banner, text, percent = null) {
  if (!banner) return;
  banner.style.display = "block";
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

function uploadSingleFileXhr(runId, file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/upload-clips/" + encodeURIComponent(runId), true);
    xhr.timeout = 140000;

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
  const setProgress = (msg, pct = null) => {
    renderUploadProgress(banner, msg, pct);
  };

  // Client-side preflight validation so invalid files fail instantly without uploading bytes
  for (const f of fileArr) {
    const lower = f.name.toLowerCase();
    if (!lower.endsWith(".mp4") && lower !== "voiceover.mp3") {
      showToast("Unsupported file '" + f.name + "'. Upload scene_01.mp4..scene_0N.mp4 or voiceover.mp3.", "error");
      return;
    }
    if (lower.endsWith(".mp4") && !/scene_?0*\d+/i.test(f.name)) {
      showToast("MP4 filename '" + f.name + "' must follow scene_01.mp4 naming convention.", "error");
      return;
    }
    if (f.size < 1024) {
      showToast("File '" + f.name + "' is too small or empty.", "error");
      return;
    }
  }

  const totalBytes = fileArr.reduce((sum, f) => sum + (f.size || 0), 0);
  const loadedByFile = new Array(fileArr.length).fill(0);
  let completedCount = 0;

  const updateUploadUi = () => {
    const loadedTotal = loadedByFile.reduce((a, b) => a + b, 0);
    const pct = totalBytes > 0 ? Math.min(100, Math.round((loadedTotal / totalBytes) * 100)) : 0;
    setProgress(
      "Step 1 of 3: Uploading " +
        fileArr.length +
        " clip(s) — " +
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
      pct
    );
  };

  updateUploadUi();
  showToast("Uploading " + fileArr.length + " clip(s) in parallel...");

  try {
    // Upload up to 3 files concurrently so requests stay small, fast, and under proxy limits
    const concurrency = Math.min(3, fileArr.length);
    let nextIdx = 0;

    async function worker() {
      while (nextIdx < fileArr.length) {
        const idx = nextIdx++;
        const file = fileArr[idx];
        await uploadSingleFileXhr(runId, file, (loaded, total) => {
          const scaled = total > 0 ? Math.round((loaded / total) * (file.size || total)) : loaded;
          loadedByFile[idx] = Math.min(file.size || scaled, scaled);
          updateUploadUi();
        });
        loadedByFile[idx] = file.size || loadedByFile[idx];
        completedCount += 1;
        updateUploadUi();
      }
    }

    await Promise.all(Array.from({ length: concurrency }, () => worker()));

    const clipsRes = await fetch("/api/clips/" + encodeURIComponent(runId));
    const upData = await clipsRes.json();
    if (!clipsRes.ok) {
      showToast("Upload check failed: " + (upData.detail || "Unable to verify clips"), "error");
      return;
    }

    if (AUTO_ASSEMBLE && upData.missing_scenes && upData.missing_scenes.length === 0) {
      setProgress("Step 2 of 3: All clips uploaded. Preparing your 9:16 video and voiceover...", null);
      showToast("All clips uploaded. Assembling video...", "info");
      const asmRes = await fetch("/api/assemble/" + encodeURIComponent(runId), { method: "POST" });
      const asmData = await asmRes.json();
      if (!asmRes.ok) {
        showToast("Video assembly failed: " + (asmData.detail || "Check scene clips"), "error");
        setTimeout(() => window.location.reload(), 1600);
        return;
      }

      if (AUTO_OPEN_REVIEW) {
        setProgress("Step 3 of 3: Video ready! Opening your review screen...", null);
        const notRes = await fetch("/api/notify/" + encodeURIComponent(runId), { method: "POST" });
        const notData = await notRes.json();
        if (notRes.ok && notData.review_url) {
          window.location.assign(notData.review_url);
          return;
        }
      }
      showToast("Your video is ready for approval!", "success");
      setTimeout(() => window.location.reload(), 900);
    } else {
      const missing = (upData.missing_scenes || []).join(", ");
      showToast(
        missing
          ? "Clips uploaded. Still waiting for scene(s): " + missing
          : "All clips uploaded. Click Assemble Video when ready.",
        "success"
      );
      setTimeout(() => window.location.reload(), 900);
    }
  } catch (err) {
    showToast("Upload failed: " + (err?.message || err), "error");
    if (banner) banner.style.display = "none";
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

document.addEventListener("DOMContentLoaded", () => {
  applyTheme(getPreferredTheme());
  const initialHash = window.location.hash.replace("#", "");
  if (initialHash && SECTION_TITLES[initialHash]) {
    navigateTo(initialHash, false);
  }
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      closeModal();
      toggleSidebar(false);
    }
  });
});
