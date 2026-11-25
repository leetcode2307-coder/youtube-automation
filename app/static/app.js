/* Shorts Studio Frontend Controller */

const AUTO_ASSEMBLE = Boolean(window.__STUDIO_CONFIG__?.autoAssemble);
const AUTO_OPEN_REVIEW = Boolean(window.__STUDIO_CONFIG__?.autoOpenReview);

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
  const topicInput = document.getElementById("quickTopic");
  const params = new URLSearchParams({ slot });
  if (nicheInput && nicheInput.value.trim()) params.set("niche", nicheInput.value.trim());
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

async function uploadFileList(runId, files) {
  const banner = document.getElementById("progress-" + runId);
  const setProgress = (msg) => {
    if (banner) {
      banner.style.display = "block";
      banner.textContent = msg;
    }
    showToast(msg);
  };

  setProgress("Step 1 of 3: Uploading " + files.length + " clip(s)...");
  const formData = new FormData();
  for (const f of files) formData.append("files", f);

  try {
    const upRes = await fetch("/api/upload-clips/" + encodeURIComponent(runId), {
      method: "POST",
      body: formData,
    });
    const upData = await upRes.json();
    if (!upRes.ok) {
      showToast("Upload failed: " + (upData.detail || "Invalid clip file"), "error");
      if (banner) banner.style.display = "none";
      return;
    }

    if (AUTO_ASSEMBLE && upData.missing_scenes && upData.missing_scenes.length === 0) {
      setProgress("Step 2 of 3: All clips uploaded. Preparing your 9:16 video and voiceover...");
      const asmRes = await fetch("/api/assemble/" + encodeURIComponent(runId), { method: "POST" });
      const asmData = await asmRes.json();
      if (!asmRes.ok) {
        showToast("Video assembly failed: " + (asmData.detail || "Check scene clips"), "error");
        setTimeout(() => window.location.reload(), 1600);
        return;
      }

      if (AUTO_OPEN_REVIEW) {
        setProgress("Step 3 of 3: Video ready! Opening your review screen...");
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
    showToast("Upload error: " + err, "error");
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
    niche: document.getElementById("cfg-niche").value.trim(),
    niche_domains: document.getElementById("cfg-niche_domains").value.trim(),
    audience_tone: document.getElementById("cfg-audience_tone").value.trim(),
    default_cta: document.getElementById("cfg-default_cta").value.trim(),
    category_id: document.getElementById("cfg-category_id").value,
    min_duration_seconds: parseInt(document.getElementById("cfg-min_duration_seconds").value, 10),
    max_duration_seconds: parseInt(document.getElementById("cfg-max_duration_seconds").value, 10),
    groq_model: document.getElementById("cfg-groq_model").value.trim(),
    tts_voice: document.getElementById("cfg-tts_voice").value.trim(),
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
