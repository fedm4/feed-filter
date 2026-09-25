// Client-side filtering over the scores already in the page.
//
// The point of doing it here rather than on the server: moving a slider must not
// re-query, and must never re-classify. The distributions were stored once; these are
// just a different reading of them.

(function () {
  "use strict";
  const cfg = window.FF || {};
  const items = Array.from(document.querySelectorAll(".item"));
  const minRelevance = document.getElementById("min-relevance");
  const maxSensationalism = document.getElementById("max-sensationalism");
  const hidePromotion = document.getElementById("hide-promotion");
  const showing = document.getElementById("showing");
  const allFiltered = document.getElementById("all-filtered");

  const STORAGE_KEY = "ff.filters";

  function readout(input) {
    const target = document.querySelector(`[data-readout="${input.id}"]`);
    if (target) target.textContent = Number(input.value).toFixed(1);
  }

  function apply() {
    const min = Number(minRelevance.value);
    const max = Number(maxSensationalism.value);
    let shown = 0;

    items.forEach((item) => {
      const classified = item.dataset.classified === "yes";
      const relevance = Number(item.dataset.relevance);
      const sensationalism = Number(item.dataset.sensationalism);
      const type = item.dataset.contentType;

      // An unclassified item is never filtered out. It has no scores to judge, and
      // hiding it would make a model outage look like an empty feed.
      let visible = !classified || (relevance >= min && sensationalism <= max);
      if (visible && classified && type === "promotion" && hidePromotion.checked) visible = false;

      // Opinion is never dropped silently: the policy may fold it, never hide it.
      const collapse = classified && cfg.opinionPolicy !== "tag" &&
        (type === "opinion" || type === "promotion");
      item.classList.toggle("collapsed", cfg.opinionPolicy === "collapse" && collapse);
      if (cfg.opinionPolicy === "hide" && collapse) visible = false;

      item.hidden = !visible;
      if (visible) shown += 1;
    });

    if (showing && cfg.showingTemplate) {
      showing.textContent = cfg.showingTemplate
        .replace("{shown}", shown)
        .replace("{total}", items.length);
    }
    if (allFiltered) allFiltered.hidden = shown !== 0 || items.length === 0;

    readout(minRelevance);
    readout(maxSensationalism);
    save();
  }

  function save() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({
        min: minRelevance.value,
        max: maxSensationalism.value,
        hide: hidePromotion.checked,
      }));
    } catch (e) {
      // A private window refuses storage. The filters still work; they just do not stick.
    }
  }

  function restore() {
    try {
      const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
      if (!saved) return;
      minRelevance.value = saved.min;
      maxSensationalism.value = saved.max;
      hidePromotion.checked = Boolean(saved.hide);
    } catch (e) { /* fall back to the server-rendered defaults */ }
  }

  function reset() {
    minRelevance.value = cfg.defaults.minRelevance;
    maxSensationalism.value = cfg.defaults.maxSensationalism;
    hidePromotion.checked = cfg.defaults.hidePromotion;
    apply();
  }

  async function label(button) {
    const body = new URLSearchParams({
      dimension: button.dataset.dimension,
      verdict: button.dataset.verdict,
    });
    button.disabled = true;
    try {
      const response = await fetch(`/items/${button.dataset.item}/label`, {
        method: "POST",
        headers: { "content-type": "application/x-www-form-urlencoded" },
        body,
      });
      if (!response.ok) throw new Error(response.status);
      const group = button.closest(".feedback-group");
      group.querySelectorAll(".feedback-button").forEach((other) => {
        other.setAttribute("aria-pressed", String(other === button));
      });
      if (!group.querySelector(".saved")) {
        const mark = document.createElement("small");
        mark.className = "saved";
        mark.textContent = "✓";
        group.appendChild(mark);
      }
    } catch (e) {
      button.style.borderColor = "#c2410c";
    } finally {
      button.disabled = false;
    }
  }

  async function poll(button) {
    const original = button.textContent;
    button.disabled = true;
    button.textContent = cfg.pollingLabel || original;
    try {
      await fetch("/admin/poll", { method: "POST" });
      window.location.reload();
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  }

  if (minRelevance && maxSensationalism) {
    restore();
    [minRelevance, maxSensationalism, hidePromotion].forEach((input) =>
      input.addEventListener("input", apply)
    );
    const resetButton = document.getElementById("reset");
    if (resetButton) resetButton.addEventListener("click", reset);
    apply();
  }

  document.querySelectorAll(".feedback-button").forEach((button) =>
    button.addEventListener("click", () => label(button))
  );
  const pollButton = document.getElementById("poll");
  if (pollButton) pollButton.addEventListener("click", () => poll(pollButton));
})();
