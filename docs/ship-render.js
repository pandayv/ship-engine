"use strict";

// Shared between alexa-simulator.html and ship-display.html so the two
// pages can never silently drift on how a finding is rendered — the
// same payload shape (from blocked_pull_requests, whether fetched
// directly or received over a push) must always look the same wherever
// it lands. This file has already once been the site of a real bug
// (structuredContent assumed to exist where it didn't); keeping one
// copy is what makes that class of bug fixable in one place instead of
// two that can disagree.

function shipEsc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// A link target is used only if it is an absolute http(s) URL; anything else (a javascript: URL,
// a relative path, garbage) becomes "#". The result is escaped for use inside an attribute.
function shipSafeUrl(u) {
  try {
    const url = new URL(String(u));
    if (url.protocol === "http:" || url.protocol === "https:") return shipEsc(url.href);
  } catch (e) { /* not a URL */ }
  return "#";
}

// The table styles travel with the renderer, so a page that uses it needs no extra CSS.
// Colours come from the page's own theme variables, with fallbacks.
function shipInstallStyles() {
  if (document.getElementById("ship-render-style")) return;
  const style = document.createElement("style");
  style.id = "ship-render-style";
  style.textContent = `
    .ship-pr { margin: 0 0 22px; }
    .ship-pr-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 12px; margin: 0 0 8px; }
    .ship-pr-title { font-weight: 700; font-size: 15px; }
    .ship-pr-repo { font-family: "JetBrains Mono", monospace; font-size: 12px; color: var(--ink-soft, #52605c); }
    .ship-pr-counts { margin-left: auto; font-size: 12.5px; color: var(--ink-soft, #52605c); }
    .ship-pr-counts b { color: var(--ink, #14181a); }
    .ship-scroll { overflow-x: auto; border: 1px solid var(--border, #dde3e0); border-radius: 10px; }
    table.ship-table { width: 100%; border-collapse: collapse; font-size: 14px; line-height: 1.45; }
    .ship-table th { text-align: left; font-family: "JetBrains Mono", monospace; font-size: 10.5px; letter-spacing: .06em;
      text-transform: uppercase; color: var(--ink-soft, #52605c); background: var(--infra-tint, #e7ecf0);
      padding: 8px 12px; white-space: nowrap; }
    .ship-table td { padding: 11px 12px; vertical-align: top; border-top: 1px solid var(--border, #dde3e0); box-sizing: border-box; }
    .ship-table tr.blocking td:first-child { box-shadow: inset 3px 0 0 var(--blocked, #b5432f); }
    .ship-table tr.review td:first-child { box-shadow: inset 3px 0 0 var(--review, #a15c1f); }
    .ship-num { font-family: "JetBrains Mono", monospace; font-weight: 700; font-size: 16px; width: 1%; }
    .ship-file { font-family: "JetBrains Mono", monospace; font-size: 12px; color: var(--ink-soft, #52605c); min-width: 150px; overflow-wrap: anywhere; }
    .ship-cite { font-family: "JetBrains Mono", monospace; font-size: 11.5px; color: var(--ink-soft, #52605c); min-width: 140px; overflow-wrap: anywhere; }
    .ship-chip { display: inline-block; font-family: "JetBrains Mono", monospace; font-size: 10.5px; font-weight: 600;
      letter-spacing: .05em; text-transform: uppercase; padding: 2px 7px; border-radius: 4px; white-space: nowrap; }
    .ship-chip.blocking { background: var(--blocked-tint, #fbeae6); color: var(--blocked, #b5432f); }
    .ship-chip.review { background: var(--review-tint, #f7ecdf); color: var(--review, #a15c1f); }
    .ship-pending { margin-top: 6px; font-size: 12px; color: var(--agent, #0d6e5c); }
    .ship-hint { margin: 4px 0 0; font-size: 12.5px; color: var(--ink-soft, #52605c); }
    @media (max-width: 640px) {
      .ship-table thead { display: none; }
      .ship-table, .ship-table tbody, .ship-table tr, .ship-table td { display: block; width: 100%; }
      .ship-table td { border-top: none; padding: 3px 12px; }
      .ship-table tr { border-top: 1px solid var(--border, #dde3e0); padding: 8px 0; }
      .ship-table tr:first-child { border-top: none; }
      .ship-table td[data-label]::before { content: attr(data-label); display: block; font-size: 10px;
        letter-spacing: .06em; text-transform: uppercase; color: var(--ink-soft, #52605c); }
      .ship-num { width: auto; }
      .ship-file, .ship-cite { min-width: 0; }
    }`;
  document.head.appendChild(style);
}

function shipPendingNote(f) {
  if (!f.pending) return "";
  return '<div class="ship-pending">Pending: ' + (f.pending.approved ? "approve" : "reject") +
    " &mdash; " + shipEsc(f.pending.reason) + "</div>";
}

// Renders a blocked_pull_requests-shaped payload into `bodyEl`, and sets
// `barEl`'s text to a label. One table per pull request, pull requests with a
// blocker first. Rows follow `position`, the same number a spoken "approve the
// first one on pull request 2" points at, and numbering restarts in each pull
// request. consoleUrl controls the "open review console" link at the bottom —
// omit it to hide that section entirely (the display page, sitting ambient and
// unattended, has no one there to click it; the simulator, which a person is
// actively driving, keeps it).
function shipRenderFindings(barEl, bodyEl, label, payload, opts) {
  opts = opts || {};
  if (barEl) barEl.textContent = label;

  const held = payload && Array.isArray(payload.held) ? payload.held : [];
  const heldNote = held.length
    ? '<p class="ship-hint">Rejected findings still block the merge until the code is fixed: ' +
      held.map(h => shipEsc(h.repo) + " #" + shipEsc(h.pr_number) + " (" + shipEsc(h.count) + ")").join(", ") + ".</p>"
    : "";

  if (!payload || !payload.pull_requests || !payload.pull_requests.length) {
    // Nothing is waiting for a decision. That is only "all clear" if no rejected finding still holds a merge.
    bodyEl.innerHTML = held.length
      ? '<div class="empty-state">Nothing is waiting for a decision.</div>' + heldNote
      : '<div class="empty-state">Nothing blocked. Everything reviewed clean.</div>';
    return;
  }
  shipInstallStyles();

  const hasBlocker = pr => pr.findings.some(f => f.blocks_merge);
  // Array.sort is stable, so pull requests keep Relay's order within each group.
  const pulls = payload.pull_requests.slice().sort((a, b) => Number(hasBlocker(b)) - Number(hasBlocker(a)));

  let html = "";
  for (const pr of pulls) {
    const findings = pr.findings.slice().sort((a, b) => a.position - b.position);
    const blocking = findings.filter(f => f.blocks_merge).length;
    const flagged = findings.length - blocking;
    html +=
      '<section class="ship-pr">' +
        '<div class="ship-pr-head">' +
          '<span class="ship-pr-title">Pull request #' + shipEsc(pr.pr_number) + "</span>" +
          '<span class="ship-pr-repo">' + shipEsc(pr.repo) + "</span>" +
          '<span class="ship-pr-counts"><b>' + blocking + "</b> blocking &middot; <b>" + flagged + "</b> flagged</span>" +
        "</div>" +
        '<div class="ship-scroll"><table class="ship-table"><thead><tr>' +
          "<th>#</th><th>Status</th><th>File</th><th>What&rsquo;s wrong</th><th>Rule</th>" +
        "</tr></thead><tbody>";
    for (const f of findings) {
      html +=
        '<tr class="' + (f.blocks_merge ? "blocking" : "review") + '">' +
          '<td class="ship-num" data-label="Finding">' + shipEsc(f.position) + "</td>" +
          '<td data-label="Status"><span class="ship-chip ' + (f.blocks_merge ? "blocking" : "review") + '">' +
            (f.blocks_merge ? "Merge blocked" : "Flagged") + "</span>" + shipPendingNote(f) + "</td>" +
          '<td class="ship-file" data-label="File">' + shipEsc(f.file) + "</td>" +
          '<td data-label="What&rsquo;s wrong">' + shipEsc(f.what_is_wrong) + "</td>" +
          '<td class="ship-cite" data-label="Rule">' + shipEsc(f.citation) + "</td>" +
        "</tr>";
    }
    html += "</tbody></table></div></section>";
  }
  const first = pulls[0];
  html += '<p class="ship-hint">Numbers restart in each pull request. Try: &ldquo;approve the first one on pull request ' +
    shipEsc(first.pr_number) + " because it&rsquo;s test data.&rdquo;</p>" + heldNote;
  bodyEl.innerHTML = html;

  if (opts.consoleUrl !== undefined) {
    const decisionBox = document.createElement("div");
    decisionBox.className = "decision-box show";
    decisionBox.innerHTML =
      '<div class="decision-head">Making the call</div>' +
      '<div class="decision-body">Every decision needs a reason on the record. Say it by voice once you have seen the findings here, or type it in Gate.</div>' +
      '<a class="console-link" href="' + shipSafeUrl(opts.consoleUrl) + '" target="_blank" rel="noopener">Open the review console &rarr;</a>';
    bodyEl.appendChild(decisionBox);
  }
}
