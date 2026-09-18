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

  var initialized = false;
  function init() {
    if (initialized) return;
    initialized = true;
    initNav();
    initMic();
    initLastResult();
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
