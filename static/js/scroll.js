// Classification: INTERNAL
// AI-assisted (Cursor) — review before merge.
//
// Validation reports can run far past the viewport. These controls jump to either end and hide
// once the page is already there. With JavaScript off they stay hidden, because a button that
// cannot scroll is only clutter.
(function () {
  "use strict";

  var toTop = document.getElementById("scroll-to-top");
  var toBottom = document.getElementById("scroll-to-bottom");
  if (!toTop || !toBottom) {
    return;
  }

  var slack = 8;

  function documentHeight() {
    var root = document.documentElement;
    var body = document.body;
    return Math.max(
      root.scrollHeight,
      body ? body.scrollHeight : 0
    );
  }

  function scrollOffset() {
    return window.scrollY || document.documentElement.scrollTop || 0;
  }

  function atTop() {
    return scrollOffset() <= slack;
  }

  function atBottom() {
    return scrollOffset() + window.innerHeight >= documentHeight() - slack;
  }

  function sync() {
    toTop.hidden = atTop();
    toBottom.hidden = atBottom();
  }

  function motion() {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      return "auto";
    }
    return "smooth";
  }

  toTop.addEventListener("click", function () {
    window.scrollTo({ top: 0, behavior: motion() });
  });

  toBottom.addEventListener("click", function () {
    window.scrollTo({ top: documentHeight(), behavior: motion() });
  });

  window.addEventListener("scroll", sync, { passive: true });
  window.addEventListener("resize", sync);
  if (typeof ResizeObserver === "function") {
    new ResizeObserver(sync).observe(document.documentElement);
  }
  sync();
})();
