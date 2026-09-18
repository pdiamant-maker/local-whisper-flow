(function () {
  "use strict";

  var TICK_SVG = '<svg class="status-icon" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="10" fill="rgba(45,180,92,0.15)"/><path d="M7.5 12.5l3 3 6-6.5" stroke="#2DB45C" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  var WARN_SVG = '<svg class="status-icon" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="10" fill="rgba(245,166,35,0.15)"/><path d="M12 8v5" stroke="#F5A623" stroke-width="2" stroke-linecap="round"/><circle cx="12" cy="16" r="1.1" fill="#F5A623"/></svg>';

  function api() {
    return window.pywebview && window.pywebview.api;
  }

  function callApi(method) {
    var a = api();
    if (!a || typeof a[method] !== "function") return Promise.reject(new Error("api not ready"));
    return a[method]();
  }

  // ---- Nav switching ----
  function initNav() {
    var items = document.querySelectorAll(".nav-item");
    items.forEach(function (item) {
      item.addEventListener("click", function () {
        items.forEach(function (i) { i.classList.remove("active"); });
        item.classList.add("active");
        var pane = item.getAttribute("data-pane");
        document.querySelectorAll(".pane").forEach(function (p) { p.classList.remove("active"); });
        var target = document.getElementById("pane-" + pane);
        if (target) target.classList.add("active");
      });
    });

    var gear = document.getElementById("settings-gear");
    if (gear) {
      gear.addEventListener("click", openSettingsView);
    }
  }

  // ---- Health checks ----
  function renderHealth(rows) {
    var container = document.getElementById("health-rows");
    if (!rows || !rows.length) {
      container.innerHTML = '<div class="muted">No health data available.</div>';
      return;
    }
    container.innerHTML = rows.map(function (row) {
      var icon = row.ok ? TICK_SVG : WARN_SVG;
      var badgeClass = row.ok ? "badge-ok" : "badge-warn";
      return (
        '<div class="health-row">' +
          icon +
          '<div class="health-text">' +
            '<div class="health-title">' + escapeHtml(row.title) + '</div>' +
            '<div class="health-subtitle">' + escapeHtml(row.subtitle) + '</div>' +
          '</div>' +
          '<div class="badge ' + badgeClass + '">' + escapeHtml(row.badge) + '</div>' +
        '</div>'
      );
    }).join("");
  }

  function escapeHtml(s) {
    var div = document.createElement("div");
    div.textContent = s == null ? "" : String(s);
    return div.innerHTML;
  }

  function loadHealth() {
    callApi("get_health").then(renderHealth).catch(function () {
      document.getElementById("health-rows").innerHTML = '<div class="muted">Could not load health checks.</div>';
    });
  }

  // ---- Mic / status polling ----
  var lastFinalText = "";

  function applyStatus(data) {
    if (!data) return;
    var status = data.current_status;
    var micButton = document.getElementById("mic-button");
    var micTitle = document.getElementById("mic-title");
    var micSubtitle = document.getElementById("mic-subtitle");
    var recording = status === "recording";
    micButton.classList.toggle("recording", recording);
    if (recording) {
      micTitle.textContent = "Recording... click to stop";
      micSubtitle.textContent = "Speak now";
    } else {
      micTitle.textContent = "Ready to dictate";
      micSubtitle.textContent = "Press Ctrl+Shift+J anywhere or start here";
    }

    if (typeof data.last_final_text === "string" && data.last_final_text !== lastFinalText) {
      lastFinalText = data.last_final_text;
      var box = document.getElementById("last-result-text");
      box.textContent = lastFinalText || "Nothing dictated yet.";
    }
  }

  function pollStatus() {
    callApi("get_status").then(applyStatus).catch(function () {});
  }

  function initMic() {
    document.getElementById("mic-button").addEventListener("click", function () {
      callApi("toggle_recording");
    });
    pollStatus();
    setInterval(pollStatus, 500);
  }

  // ---- Last result actions ----
  function initLastResult() {
    document.getElementById("copy-btn").addEventListener("click", function () {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(lastFinalText || "").catch(function () {
          callApi("copy_last");
        });
      } else {
        callApi("copy_last");
      }
    });

    document.getElementById("paste-btn").addEventListener("click", function () {
      callApi("paste_last");
    });
  }

  // ---- History ----
  var historySelectedId = null;
  var historyRows = [];

  function relativeTime(ts) {
    var seconds = Math.max(0, (Date.now() / 1000) - ts);
    if (seconds < 60) return "just now";
    var minutes = Math.floor(seconds / 60);
    if (minutes < 60) return minutes + "m ago";
    var hours = Math.floor(minutes / 60);
    if (hours < 24) return hours + "h ago";
    var days = Math.floor(hours / 24);
    if (days < 7) return days + "d ago";
    return new Date(ts * 1000).toLocaleDateString();
  }

  function renderHistoryList() {
    var list = document.getElementById("history-list");
    var count = document.getElementById("history-count");
    count.textContent = historyRows.length + " saved dictation" + (historyRows.length === 1 ? "" : "s");

    if (!historyRows.length) {
      list.innerHTML = '<div class="muted">No dictations yet.</div>';
      return;
    }

    list.innerHTML = historyRows.map(function (row) {
      var selected = row.id === historySelectedId ? " selected" : "";
      var badge = row.ai ? '<span class="ai-badge">AI</span>' : "";
      return (
        '<button class="history-row' + selected + '" data-id="' + row.id + '">' +
          '<div class="history-row-top">' +
            '<span class="history-row-app">' + escapeHtml(row.app || "Unknown app") + '</span>' +
            badge +
          '</div>' +
          '<div class="history-row-snippet">' + escapeHtml(row.snippet || "(empty)") + '</div>' +
          '<div class="history-row-time">' + relativeTime(row.ts) + '</div>' +
        '</button>'
      );
    }).join("");

    list.querySelectorAll(".history-row").forEach(function (el) {
      el.addEventListener("click", function () {
        selectHistoryItem(parseInt(el.getAttribute("data-id"), 10));
      });
    });
  }

  function loadHistoryList() {
    var query = document.getElementById("history-search").value.trim();
    var a = api();
    if (!a || typeof a.history_list !== "function") return;
    a.history_list(query, 200).then(function (rows) {
      historyRows = rows || [];
      if (historySelectedId !== null && !historyRows.some(function (r) { return r.id === historySelectedId; })) {
        historySelectedId = null;
        renderHistoryDetail(null);
      }
      renderHistoryList();
    }).catch(function () {});
  }

  function detailTile(label, value) {
    return (
      '<div class="detail-tile">' +
        '<div class="detail-tile-label">' + escapeHtml(label) + '</div>' +
        '<div class="detail-tile-value">' + escapeHtml(value) + '</div>' +
      '</div>'
    );
  }

  function renderHistoryDetail(row) {
    var pane = document.getElementById("history-detail");
    if (!row) {
      pane.innerHTML = '<div class="placeholder">Select a dictation to see details.</div>';
      return;
    }

    var tiles = [
      detailTile("Application", row.app || "Unknown"),
      detailTile("Window", row.window_title || "—"),
      detailTile("Characters", String(row.chars || 0)),
      detailTile("AI Processed", row.ai_processed ? "Yes" : "No"),
    ];
    if (row.model) tiles.push(detailTile("Model", row.model));
    if (row.audio_secs) tiles.push(detailTile("Audio", row.audio_secs.toFixed(1) + "s"));
    tiles.push(detailTile("Timestamp", new Date(row.ts * 1000).toLocaleString()));

    var originalBlock = "";
    if (row.raw && row.raw !== row.final) {
      originalBlock = (
        '<div class="detail-label">Original Transcription</div>' +
        '<div class="detail-text-box">' + escapeHtml(row.raw) + '</div>'
      );
    }

    pane.innerHTML = (
      '<div class="detail-date">' + new Date(row.ts * 1000).toLocaleString() + '</div>' +
      '<div class="detail-label">Final Text</div>' +
      '<div class="detail-text-box">' + escapeHtml(row.final || "(empty)") + '</div>' +
      originalBlock +
      '<div class="detail-tiles">' + tiles.join("") + '</div>' +
      '<div class="detail-actions">' +
        '<button class="btn" id="history-copy-final">Copy Final</button>' +
        '<button class="btn btn-primary" id="history-paste">Paste at Cursor</button>' +
        '<button class="btn btn-danger" id="history-delete-entry">Delete Entry</button>' +
      '</div>'
    );

    document.getElementById("history-copy-final").addEventListener("click", function () {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(row.final || "");
      }
    });
    document.getElementById("history-paste").addEventListener("click", function () {
      callApi_history_paste(row.id);
    });
    document.getElementById("history-delete-entry").addEventListener("click", function () {
      var a = api();
      if (!a) return;
      a.history_delete(row.id).then(function () {
        loadHistoryList();
      });
    });
  }

  function callApi_history_paste(id) {
    var a = api();
    if (!a || typeof a.history_paste !== "function") return;
    a.history_paste(id);
  }

  function selectHistoryItem(id) {
    historySelectedId = id;
    renderHistoryList();
    var a = api();
    if (!a || typeof a.history_get !== "function") return;
    a.history_get(id).then(renderHistoryDetail).catch(function () {});
  }

  function initHistory() {
    document.getElementById("history-search").addEventListener("input", debounce(loadHistoryList, 200));
    document.getElementById("history-refresh").addEventListener("click", loadHistoryList);

    var confirmBar = document.getElementById("history-confirm-bar");
    document.getElementById("history-delete-all").addEventListener("click", function () {
      confirmBar.hidden = false;
    });
    document.getElementById("history-confirm-no").addEventListener("click", function () {
      confirmBar.hidden = true;
    });
    document.getElementById("history-confirm-yes").addEventListener("click", function () {
      confirmBar.hidden = true;
      var a = api();
      if (!a) return;
      a.history_clear().then(function () {
        historySelectedId = null;
        loadHistoryList();
        renderHistoryDetail(null);
      });
    });

    // Load on nav activation, not on a poll.
    var historyNavItem = document.querySelector('.nav-item[data-pane="history"]');
    if (historyNavItem) {
      historyNavItem.addEventListener("click", loadHistoryList);
    }
  }

  function debounce(fn, wait) {
    var timer = null;
    return function () {
      var args = arguments;
      clearTimeout(timer);
      timer = setTimeout(function () { fn.apply(null, args); }, wait);
    };
  }

  // ---- Settings ----
  var KEY_NAME_MAP = {
    " ": "<space>", "Escape": "<esc>", "Tab": "<tab>", "Enter": "<enter>",
    "Backspace": "<backspace>", "Delete": "<delete>", "ArrowUp": "<up>",
    "ArrowDown": "<down>", "ArrowLeft": "<left>", "ArrowRight": "<right>",
    "Home": "<home>", "End": "<end>", "PageUp": "<page_up>", "PageDown": "<page_down>",
    "F1": "<f1>", "F2": "<f2>", "F3": "<f3>", "F4": "<f4>", "F5": "<f5>", "F6": "<f6>",
    "F7": "<f7>", "F8": "<f8>", "F9": "<f9>", "F10": "<f10>", "F11": "<f11>", "F12": "<f12>"
  };

  function keyToToken(e) {
    if (e.key.length === 1) return e.key.toLowerCase();
    return KEY_NAME_MAP[e.key] || null;
  }

  function formatHotkeyDisplay(combo) {
    if (!combo) return "Not set";
    return combo.replace(/<ctrl>/g, "Ctrl").replace(/<shift>/g, "Shift")
      .replace(/<alt>/g, "Alt").replace(/<cmd>/g, "Win").replace(/</g, "").replace(/>/g, "")
      .split("+").join(" + ");
  }

  function startHotkeyCapture(button, onCapture) {
    button.classList.add("capturing");
    button.textContent = "Press keys... (Esc to cancel)";

    function handler(e) {
      e.preventDefault();
      e.stopPropagation();
      if (e.key === "Escape") {
        cleanup();
        onCapture(null);
        return;
      }
      if (["Control", "Shift", "Alt", "Meta"].indexOf(e.key) !== -1) return;

      var token = keyToToken(e);
      if (!token) return;

      var parts = [];
      if (e.ctrlKey) parts.push("<ctrl>");
      if (e.shiftKey) parts.push("<shift>");
      if (e.altKey) parts.push("<alt>");
      if (e.metaKey) parts.push("<cmd>");
      parts.push(token);
      cleanup();
      onCapture(parts.join("+"));
    }

    function cleanup() {
      document.removeEventListener("keydown", handler, true);
      button.classList.remove("capturing");
    }

    document.addEventListener("keydown", handler, true);
  }

  function setSetting(key, value) {
    var updates = {};
    updates[key] = value;
    var a = api();
    if (!a || typeof a.set_settings !== "function") return Promise.reject(new Error("api not ready"));
    return a.set_settings(updates).then(function (snapshot) {
      if (snapshot && snapshot.restart_required) showRestartChip();
      return snapshot;
    });
  }

  function showRestartChip() {
    document.getElementById("restart-chip").hidden = false;
  }

  function bindToggle(id, key) {
    var el = document.getElementById(id);
    el.addEventListener("change", function () { setSetting(key, el.checked); });
  }

  function bindSelect(id, key, isNumber) {
    var el = document.getElementById(id);
    el.addEventListener("change", function () {
      setSetting(key, isNumber ? parseFloat(el.value) : el.value);
    });
  }

  function bindHotkeyRecorder(buttonId, key, allowEmpty) {
    var button = document.getElementById(buttonId);
    button.addEventListener("click", function () {
      startHotkeyCapture(button, function (combo) {
        if (combo === null) {
          renderHotkeyButton(button, key);
          return;
        }
        button.textContent = formatHotkeyDisplay(combo);
        setSetting(key, combo);
      });
    });
  }

  var _currentSettingsSnapshot = {};

  function renderHotkeyButton(button, key) {
    button.textContent = formatHotkeyDisplay(_currentSettingsSnapshot[key]);
  }

  function applySettingsToForm(snapshot) {
    _currentSettingsSnapshot = snapshot || {};

    document.getElementById("display-name-input").value = snapshot.display_name || "";
    document.querySelector(".profile-name").textContent = snapshot.display_name || "Local Flow";

    document.getElementById("hotkey-recorder").textContent = formatHotkeyDisplay(snapshot.hotkey);
    document.getElementById("paste-hotkey-recorder").textContent = formatHotkeyDisplay(snapshot.paste_last_hotkey);
    document.getElementById("activation-mode-select").value = snapshot.activation_mode || "toggle";

    document.getElementById("overlay-scale-select").value = String(snapshot.overlay_scale || 1);
    document.getElementById("live-preview-toggle").checked = !!snapshot.live_preview;

    document.getElementById("copy-clipboard-toggle").checked = !!snapshot.copy_to_clipboard;
    document.getElementById("lowercase-first-toggle").checked = !!snapshot.lowercase_first;
    document.getElementById("strip-period-toggle").checked = !!snapshot.strip_trailing_period;
    document.getElementById("space-between-toggle").checked = !!snapshot.space_between;

    document.getElementById("save-history-toggle").checked = !!snapshot.save_history;
    document.getElementById("save-audio-toggle").checked = !!snapshot.save_audio;
  }

  function loadMicList(selected) {
    var a = api();
    if (!a || typeof a.list_input_devices !== "function") return;
    a.list_input_devices().then(function (devices) {
      var select = document.getElementById("mic-select");
      var options = ['<option value="">System default</option>'];
      (devices || []).forEach(function (d) {
        options.push('<option value="' + escapeHtml(d.name) + '">' + escapeHtml(d.name) + "</option>");
      });
      select.innerHTML = options.join("");
      select.value = selected || "";
    }).catch(function () {});
  }

  function loadLaunchAtLogin() {
    var a = api();
    if (!a || typeof a.get_launch_at_login !== "function") return;
    a.get_launch_at_login().then(function (enabled) {
      document.getElementById("launch-at-login-toggle").checked = !!enabled;
    }).catch(function () {});
  }

  function loadSettingsData() {
    callApi("get_settings").then(function (snapshot) {
      applySettingsToForm(snapshot);
      loadMicList(snapshot.input_device);
    }).catch(function () {});
    loadLaunchAtLogin();
  }

  var hotkeyTestPolling = null;

  function startHotkeyTestPolling() {
    stopHotkeyTestPolling();
    hotkeyTestPolling = setInterval(function () {
      callApi("get_status").then(function (data) {
        var badge = document.getElementById("hotkey-test-badge");
        if (!badge) return;
        var recording = data && data.current_status === "recording";
        badge.textContent = recording ? "Recording" : "Idle";
        badge.className = "badge " + (recording ? "badge-accent" : "badge-warn");
      }).catch(function () {});
    }, 400);
  }

  function stopHotkeyTestPolling() {
    if (hotkeyTestPolling) {
      clearInterval(hotkeyTestPolling);
      hotkeyTestPolling = null;
    }
  }

  function switchSettingsSection(name) {
    document.querySelectorAll(".settings-nav-item").forEach(function (item) {
      item.classList.toggle("active", item.getAttribute("data-section") === name);
    });
    document.querySelectorAll(".settings-section").forEach(function (section) {
      section.classList.toggle("active", section.id === "settings-" + name);
    });
    if (name === "shortcuts") {
      startHotkeyTestPolling();
    } else {
      stopHotkeyTestPolling();
    }
  }

  function openSettingsView() {
    document.getElementById("main-content").classList.add("hidden-view");
    document.getElementById("settings-view").classList.add("open");
    document.getElementById("restart-chip").hidden = true;
    loadSettingsData();
  }

  function closeSettingsView() {
    stopHotkeyTestPolling();
    document.getElementById("settings-view").classList.remove("open");
    document.getElementById("main-content").classList.remove("hidden-view");
  }

  function initSettings() {
    document.getElementById("settings-back").addEventListener("click", closeSettingsView);

    document.querySelectorAll(".settings-nav-item").forEach(function (item) {
      item.addEventListener("click", function () {
        switchSettingsSection(item.getAttribute("data-section"));
      });
    });

    document.getElementById("display-name-save").addEventListener("click", function () {
      var value = document.getElementById("display-name-input").value.trim();
      setSetting("display_name", value).then(function (snapshot) {
        document.querySelector(".profile-name").textContent = value || "Local Flow";
      });
    });

    document.getElementById("launch-at-login-toggle").addEventListener("change", function (e) {
      var a = api();
      if (!a) return;
      a.set_launch_at_login(e.target.checked).catch(function () {});
    });
    document.getElementById("view-releases-btn").addEventListener("click", function () {
      var a = api();
      if (a && typeof a.open_url === "function") a.open_url("https://github.com/pdiamant-maker/local-whisper-flow/releases");
    });

    bindHotkeyRecorder("hotkey-recorder", "hotkey");
    bindHotkeyRecorder("paste-hotkey-recorder", "paste_last_hotkey");
    document.getElementById("paste-hotkey-clear").addEventListener("click", function () {
      document.getElementById("paste-hotkey-recorder").textContent = "Not set";
      setSetting("paste_last_hotkey", "");
    });
    bindSelect("activation-mode-select", "activation_mode", false);

    document.getElementById("mic-select").addEventListener("change", function (e) {
      setSetting("input_device", e.target.value);
    });
    document.getElementById("mic-refresh").addEventListener("click", function () {
      loadMicList(document.getElementById("mic-select").value);
    });
    bindSelect("overlay-scale-select", "overlay_scale", true);
    bindToggle("live-preview-toggle", "live_preview");

    bindToggle("copy-clipboard-toggle", "copy_to_clipboard");
    bindToggle("lowercase-first-toggle", "lowercase_first");
    bindToggle("strip-period-toggle", "strip_trailing_period");
    bindToggle("space-between-toggle", "space_between");

    bindToggle("save-history-toggle", "save_history");
    bindToggle("save-audio-toggle", "save_audio");

    document.getElementById("open-logs-btn").addEventListener("click", function () {
      var a = api();
      if (a && typeof a.open_logs === "function") a.open_logs();
    });
    document.getElementById("export-support-btn").addEventListener("click", function () {
      var a = api();
      if (a && typeof a.export_support_bundle === "function") a.export_support_bundle();
    });

    document.getElementById("restart-now-btn").addEventListener("click", function () {
      var a = api();
      if (a && typeof a.restart_app === "function") a.restart_app();
    });
  }

  var initialized = false;
  function init() {
    if (initialized) return;
    initialized = true;
    initNav();
    initMic();
    initLastResult();
    initHistory();
    initSettings();
    loadHealth();
    callApi("get_settings").then(function (snapshot) {
      var name = document.querySelector(".profile-name");
      if (name && snapshot && snapshot.display_name) name.textContent = snapshot.display_name;
    }).catch(function () {});
  }

  if (window.pywebview) {
    init();
  } else {
    window.addEventListener("pywebviewready", init);
    // Fallback in case pywebviewready never fires (e.g. stub testing without the event).
    setTimeout(init, 500);
  }
})();
