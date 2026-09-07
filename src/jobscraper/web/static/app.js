/* Windows Job Scraper — local dashboard bootstrap logic.
 *
 * Slice 0 contract (SEC-01): the launcher opens the dashboard with a
 * one-time bootstrap ticket in the URL *fragment* (never query/path), so it
 * is not sent with any HTTP request and never appears in server logs. This
 * script reads the fragment and exchanges the ticket exactly once via a
 * same-origin POST with strict Origin; on success the service sets the
 * HttpOnly session cookie and a mirrored CSRF cookie, and we navigate to the
 * authenticated dashboard.
 */
(function () {
  "use strict";

  function readBootstrapTicket() {
    if (!window.location.hash || window.location.hash.length < 2) {
      return null;
    }
    var fragment = window.location.hash.slice(1);
    var params = new URLSearchParams(fragment);
    var ticket = params.get("bootstrap");
    // The fragment never leaves the browser: strip it immediately.
    if (window.history && window.history.replaceState) {
      window.history.replaceState(null, "", window.location.pathname);
    }
    return ticket;
  }

  async function bootstrapExchange(exchangeUrl, successUrl) {
    var status = document.getElementById("status");
    var ticket = readBootstrapTicket();
    if (!ticket) {
      if (status) { status.textContent = "No bootstrap ticket. Open the application from JobScraper.exe."; }
      return;
    }
    try {
      var response = await fetch(exchangeUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ticket: ticket }),
      });
      if (!response.ok) {
        if (status) { status.textContent = "Bootstrap rejected. Launch the application again."; }
        return;
      }
      window.location.replace(successUrl);
    } catch (err) {
      if (status) { status.textContent = "Local service unreachable."; }
    }
  }

  window.wjs = { bootstrapExchange: bootstrapExchange };
})();
