/* Windows Job Scraper — dashboard behavior (CSP: local scripts only).
 *
 * Responsibilities:
 *  - one-time bootstrap ticket exchange from the URL fragment (#bootstrap=...);
 *  - authenticated JSON API helper (session cookie + CSRF header);
 *  - data-api-action buttons (dispositions, applications, runs);
 *  - data-api-form forms (profile creation);
 *  - run status polling.
 */
(function () {
  "use strict";

  function csrfToken() {
    var m = document.cookie.match(/(?:^|;\s*)wjs_csrf=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : "";
  }

  function api(method, url, body) {
    var headers = {};
    if (body !== undefined) headers["Content-Type"] = "application/json";
    var token = csrfToken();
    if (token) headers["X-CSRF-Token"] = token;
    return fetch(url, {
      method: method,
      headers: headers,
      credentials: "same-origin",
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  }
  window.wjs = { api: api };

  function setStatus(text) {
    var el = document.querySelector("[data-run-status]");
    if (el) el.textContent = text;
  }

  function reload() { location.reload(); }

  // ---- bootstrap ticket exchange (fragment never leaves the browser) ----
  function bootstrap() {
    var m = location.hash.match(/^#bootstrap=(.+)$/);
    if (!m) return;
    api("POST", "/api/bootstrap/exchange", { ticket: m[1] }).then(function (resp) {
      if (resp.ok) {
        history.replaceState(null, "", location.pathname + location.search);
        reload();
      }
    }).catch(function () { /* keep hash; user can retry */ });
  }

  // ---- run polling ----
  function pollRun(runId) {
    setStatus("Run " + runId + " started…");
    var attempts = 0;
    (function tick() {
      if (attempts++ > 240) { setStatus("Run still executing — refresh later."); return; }
      api("GET", "/api/runs/" + encodeURIComponent(runId)).then(function (resp) {
        if (!resp.ok) { setStatus("Run status unavailable."); return; }
        return resp.json().then(function (run) {
          setStatus("Run " + run.status);
          if (["SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"].indexOf(run.status) >= 0) {
            reload();
          } else {
            setTimeout(tick, 500);
          }
        });
      }).catch(function () { setTimeout(tick, 1000); });
    })();
  }

  // ---- data-api-action buttons ----
  function resolveBody(el) {
    var raw = el.getAttribute("data-body");
    if (!raw) return undefined;
    try { return JSON.parse(raw); } catch (e) { return undefined; }
  }

  function runAction(el) {
    var url = el.getAttribute("data-url");
    var method = el.getAttribute("data-method") || "POST";
    var body = resolveBody(el);
    // Buttons paired with a status <select> take the selected value.
    var selectId = el.getAttribute("data-use-status-select");
    if (selectId) {
      var sel = document.querySelector('[data-status-select="' + selectId + '"]');
      if (sel) body = { status: sel.value };
    }
    el.disabled = true;
    api(method, url, body).then(function (resp) {
      return resp.json().catch(function () { return {}; }).then(function (data) {
        if (!resp.ok) {
          setStatus("Error: " + (data.detail || resp.status));
          el.disabled = false;
          return;
        }
        if (el.hasAttribute("data-poll-run") && data.run_id) { pollRun(data.run_id); return; }
        reload();
      });
    }).catch(function () { setStatus("Network error"); el.disabled = false; });
  }

  document.addEventListener("click", function (ev) {
    var el = ev.target.closest ? ev.target.closest("[data-api-action]") : null;
    if (!el) return;
    ev.preventDefault();
    runAction(el);
  });

  // ---- data-api-form forms ----
  document.addEventListener("submit", function (ev) {
    var form = ev.target.closest ? ev.target.closest("form[data-api-form]") : null;
    if (!form) return;
    ev.preventDefault();
    var url = form.getAttribute("data-url");
    var body = {};
    var data = new FormData(form);
    data.forEach(function (value, key) { body[key] = value; });
    form.querySelectorAll("input[type=number]").forEach(function (inp) {
      if (inp.value !== "") body[inp.name] = parseFloat(inp.value);
    });
    var statusEl = form.querySelector("[data-form-status]");
    api("POST", url, body).then(function (resp) {
      return resp.json().catch(function () { return {}; }).then(function (data) {
        if (statusEl) {
          statusEl.textContent = resp.ok ? "Saved." : "Error: " + (data.detail || resp.status);
        }
        if (resp.ok) setTimeout(reload, 400);
      });
    }).catch(function () { if (statusEl) statusEl.textContent = "Network error"; });
  });

  // ---- profile selector navigation ----
  document.addEventListener("change", function (ev) {
    var sel = ev.target.closest ? ev.target.closest("select[data-profile-nav]") : null;
    if (!sel) return;
    var base = sel.getAttribute("data-base-url") || "/";
    location.href = base + "?profile_id=" + encodeURIComponent(sel.value);
  });

  bootstrap();
})();
