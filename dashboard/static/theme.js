"use strict";

(() => {
  const storageKey = "quantar-dashboard-theme";
  let theme = "";
  try {
    theme = window.localStorage.getItem(storageKey) || "";
  } catch (_) {
    // Storage can be unavailable in locked-down browser profiles.
  }
  if (theme !== "light" && theme !== "dark") {
    theme = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  document.documentElement.dataset.theme = theme;
  document.documentElement.style.colorScheme = theme;
})();
