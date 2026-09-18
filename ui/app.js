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
      gear.addEventListener("click", function () {
        callApi("open_settings_placeholder");
      });
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

  var initialized = false;
  function init() {
    if (initialized) return;
    initialized = true;
    initNav();
    initMic();
    initLastResult();
    initHistory();
    loadHealth();
  }

  if (window.pywebview) {
    init();
  } else {
    window.addEventListener("pywebviewready", init);
    // Fallback in case pywebviewready never fires (e.g. stub testing without the event).
    setTimeout(init, 500);
  }
})();
