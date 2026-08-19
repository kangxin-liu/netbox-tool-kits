// Classification: INTERNAL
// AI-assisted (Cursor) — review before merge.
//
// The only JavaScript in this application: a busy state so an apply is not submitted twice while the
// operator waits (Requirement 15.4). The server refuses a second apply on its own — the plan carries
// the time it was applied — so this is about not making the operator discover that.
//
// Progressive enhancement: with JavaScript off, the form still submits and the server still refuses
// the duplicate.
(function () {
  "use strict";

  var form = document.getElementById("apply-form");
  if (!form) {
    return;
  }

  form.addEventListener("submit", function () {
    var button = document.getElementById("apply-button");
    var busy = document.getElementById("apply-busy");
    if (button) {
      button.disabled = true;
      button.textContent = button.getAttribute("data-busy-label") || "Applying…";
    }
    if (busy) {
      busy.hidden = false;
    }
  });
})();
