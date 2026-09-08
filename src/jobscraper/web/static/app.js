// Windows Job Scraper — dashboard client (Slice 1).
// All requests are same-origin; mutations carry the mirrored CSRF cookie
// value in the X-CSRF-Token header (Slice-0 security shell).

(function () {
  "use strict";

  var csrf = function () {
    var m = document.cookie.match(/(?:^|;\s*)wjs_csrf=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : "";
  };

  var api = function (method, path, body) {
    return fetch(path, {
      method: method,
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrf(),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "same-origin",
    }).then(function (res) {
      if (res.status === 401) {
        window.location.replace("/");
        throw new Error("session expired");
      }
      return res.json().then(function (data) {
        if (!res.ok) {
          throw Object.assign(new Error(data.detail || data.error || res.status),
                              { status: res.status, data: data });
        }
        return data;
      });
    });
  };

  var el = function (id) { return document.getElementById(id); };
  var esc = function (value) {
    // defense-in-depth alongside server-side Jinja autoescape
    var d = document.createElement("span");
    d.textContent = value == null ? "" : String(value);
    return d.innerHTML;
  };

  // ------------------------------------------------------------- profiles
  function refreshProfiles() {
    return api("GET", "/api/profiles").then(function (data) {
      var list = el("profiles-list");
      var select = el("run-profile");
      var inboxSelect = el("inbox-profile");
      list.innerHTML = "";
      select.innerHTML = '<option value="">(no profile)</option>';
      inboxSelect.innerHTML = '<option value="">Select a profile…</option>';
      (data.profiles || []).forEach(function (p) {
        var li = document.createElement("li");
        li.className = "meta-list-item";
        li.innerHTML = "<strong>" + esc(p.name) + "</strong> · updated " + esc(p.updated_at);
        list.appendChild(li);
        var o1 = document.createElement("option");
        o1.value = p.id; o1.textContent = p.name;
        select.appendChild(o1);
        var o2 = document.createElement("option");
        o2.value = p.id; o2.textContent = p.name;
        inboxSelect.appendChild(o2);
      });
      if (!data.profiles || !data.profiles.length) {
        list.innerHTML = '<p class="hint">No profiles yet.</p>';
      }
    });
  }

  el("profile-form").addEventListener("submit", function (event) {
    event.preventDefault();
    var form = new FormData(event.target);
    var payload = {
      name: form.get("name"),
      keywords: String(form.get("keywords") || "").split(",").map(function (s) { return s.trim(); }).filter(Boolean),
      eligible_countries: String(form.get("eligible_countries") || "").split(",").map(function (s) { return s.trim().toUpperCase(); }).filter(Boolean),
      remote_rules: { remote_ok: true },
      min_score_inbox: 0,
    };
    api("POST", "/api/profiles", payload).then(refreshProfiles).catch(function (err) {
      alert("Profile error: " + err.message);
    });
  });

  // ------------------------------------------------------------ collection
  el("run-form").addEventListener("submit", function (event) {
    event.preventDefault();
    el("run-status").innerHTML = '<p class="hint">Collecting…</p>';
    api("POST", "/api/runs", { profile_id: el("run-profile").value || null })
      .then(function (data) {
        el("run-status").innerHTML =
          "<p>Run <code>" + esc(data.run_id) + "</code>: <strong>" + esc(data.status) +
          "</strong> · jobs saved " + esc(data.jobs_saved) +
          " · requests " + esc(data.requests_total) + " (" + esc(data.requests_failed) + " failed)</p>";
        refreshInbox();
      })
      .catch(function (err) {
        el("run-status").innerHTML = '<p class="error">Run failed: ' + esc(err.message) + "</p>";
      });
  });

  // --------------------------------------------------------------- inbox
  var inboxItems = [];
  var cursor = 0;

  function refreshInbox() {
    var profileId = el("inbox-profile").value;
    var list = el("inbox-list");
    if (!profileId) { list.innerHTML = ""; inboxItems = []; return; }
    return api("GET", "/api/inbox?profile_id=" + encodeURIComponent(profileId)).then(function (data) {
      inboxItems = data.inbox || [];
      cursor = 0;
      renderInbox();
      refreshApplications(profileId);
    });
  }

  function renderInbox() {
    var list = el("inbox-list");
    list.innerHTML = "";
    inboxItems.forEach(function (item, index) {
      var li = document.createElement("li");
      li.className = "inbox-item" + (index === cursor ? " selected" : "");
      li.setAttribute("data-index", String(index));
      var contributions = (item.breakdown || [])
        .map(function (c) { return c.rule + " " + (c.points > 0 ? "+" : "") + c.points; })
        .join(", ");
      li.innerHTML =
        "<div class='inbox-main'>" +
        "<strong>" + esc(item.title || "(untitled)") + "</strong> " +
        "<span class='score'>score " + esc(item.score == null ? "—" : item.score) + "</span> " +
        "<span class='verdict'>" + esc(item.eligibility || "UNCLEAR") + "</span> " +
        "<span class='kind'>" + esc(item.event_kind) + "</span></div>" +
        "<div class='inbox-meta'>" + esc(contributions) + "</div>";
      li.addEventListener("click", function () { cursor = index; renderInbox(); loadJob(item.job_id); });
      list.appendChild(li);
    });
    if (!inboxItems.length) {
      list.innerHTML = '<p class="hint">Inbox empty — collect or adjust the profile.</p>';
    }
  }

  function act(action) {
    if (!inboxItems.length) return;
    var item = inboxItems[cursor];
    if (!item) return;
    var profileId = el("inbox-profile").value;
    var payload = { disposition: action };
    if (action === "SNOOZED") {
      var days = window.prompt("Snooze for how many days?", "3");
      if (!days) return;
      var until = new Date(Date.now() + parseInt(days, 10) * 86400000);
      payload.snoozed_until = until.toISOString().replace(/\.\d{3}Z$/, ".000000Z");
    }
    api("POST", "/api/profiles/" + encodeURIComponent(profileId) + "/jobs/" +
        encodeURIComponent(item.job_id) + "/disposition", payload)
      .then(refreshInbox)
      .catch(function (err) {
        if (err.status === 409) { refreshInbox(); } // stale revision: reload
        else alert("Disposition error: " + err.message);
      });
  }

  function openApply() {
    if (!inboxItems.length) return;
    var item = inboxItems[cursor];
    if (!item) return;
    api("GET", "/api/jobs/" + encodeURIComponent(item.job_id)).then(function (job) {
      if (job.apply_url) {
        window.open(job.apply_url, "_blank", "noopener");
      } else {
        alert("No safe direct application URL for this job.");
      }
    });
  }

  function loadJob(jobId) {
    api("GET", "/api/jobs/" + encodeURIComponent(jobId)).then(function (job) {
      var box = el("job-detail");
      var sources = (job.sources || []).map(function (s) {
        return "<li>" + esc(s.source_id) + " · " + esc(s.presence_state) +
          (s.application_url ? " · <a href='" + esc(s.application_url) + "' target='_blank' rel='noopener'>apply</a>" : "") +
          (s.canonical_job_url ? " · <a href='" + esc(s.canonical_job_url) + "' target='_blank' rel='noopener'>listing</a>" : "") +
          "</li>";
      }).join("");
      box.innerHTML =
        "<h3>" + esc(job.job.title) + "</h3>" +
        "<p>" + esc(job.job.listing_status) + " · first seen " + esc(job.job.first_seen_at) + "</p>" +
        (job.job.salary_original_text ? "<p>Salary: " + esc(job.job.salary_original_text) + "</p>" : "") +
        "<ul class='meta-list'>" + sources + "</ul>" +
        "<p><button id='open-apply'>Open direct application link</button></p>";
      var btn = box.querySelector("#open-apply");
      if (btn) btn.addEventListener("click", openApply);
    });
  }

  // --------------------------------------------------------- applications
  function refreshApplications(profileId) {
    api("GET", "/api/applications?profile_id=" + encodeURIComponent(profileId)).then(function (data) {
      var box = el("applications-list");
      box.innerHTML = (data.applications || []).map(function (a) {
        return "<li><strong>" + esc(a.status) + "</strong> · job " + esc(a.job_id) +
          (a.next_action_text ? " · next: " + esc(a.next_action_text) : "") + "</li>";
      }).join("") || '<p class="hint">No applications yet.</p>';
    });
  }

  // -------------------------------------------------------- keyboard triage
  document.addEventListener("keydown", function (event) {
    if (event.target && /INPUT|TEXTAREA|SELECT/.test(event.target.tagName)) return;
    switch (event.key) {
      case "j": if (cursor < inboxItems.length - 1) { cursor += 1; renderInbox(); } break;
      case "k": if (cursor > 0) { cursor -= 1; renderInbox(); } break;
      case "s": act("SHORTLISTED"); break;
      case "d": act("DISMISSED"); break;
      case "a": act("ARCHIVED"); break;
      case "n": act("SNOOZED"); break;
      case "o": openApply(); break;
    }
  });

  el("inbox-profile").addEventListener("change", refreshInbox);

  refreshProfiles();
})();
