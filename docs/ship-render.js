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
  return String(s == null ? "" : s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

// Renders a blocked_pull_requests-shaped payload into `bodyEl`, and sets
// `barEl`'s text to a label. consoleUrl controls the "open review
// console" link at the bottom — omit it to hide that section entirely
// (the display page, sitting ambient and unattended, has no one there
// to click it; the simulator, which a person is actively driving, keeps
// it).
function shipRenderFindings(barEl, bodyEl, label, payload, opts) {
  opts = opts || {};
  if (barEl) barEl.textContent = label;

  if (!payload || !payload.pull_requests || !payload.pull_requests.length) {
    bodyEl.innerHTML = '<div class="empty-state">Nothing blocked. Everything reviewed clean.</div>';
    return;
  }

  let html = "";
  for (const pr of payload.pull_requests) {
    for (const f of pr.findings) {
      html +=
        '<div class="finding ' + (f.blocks_merge ? "blocking" : "review") + '">' +
          '<div class="f-top">' +
            '<span class="pill ' + (f.blocks_merge ? "blocking" : "review") + '">' +
              (f.blocks_merge ? "Merge blocked" : "Flagged") + "</span>" +
            '<span class="f-file">' + shipEsc(pr.repo) + " #" + pr.pr_number + " &middot; " + shipEsc(f.file) + "</span>" +
          "</div>" +
          '<p class="f-what">' + shipEsc(f.what_is_wrong) + "</p>" +
          '<span class="f-cite">' + shipEsc(f.citation) + "</span>" +
        "</div>";
    }
  }
  bodyEl.innerHTML = html || '<div class="empty-state">Nothing blocked. Everything reviewed clean.</div>';

  if (opts.consoleUrl !== undefined) {
    const decisionBox = document.createElement("div");
    decisionBox.className = "decision-box show";
    decisionBox.innerHTML =
      '<div class="decision-head">Making the call</div>' +
      '<div class="decision-body">Accepting a risk or confirming a fix happens in Gate, with a typed, attributed reason — not from this screen and never by voice.</div>' +
      '<a class="console-link" href="' + (opts.consoleUrl || "#") + '" target="_blank" rel="noopener">Open the review console &rarr;</a>';
    bodyEl.appendChild(decisionBox);
  }
}
