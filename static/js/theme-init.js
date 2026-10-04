/* Runs synchronously in <head> so the saved theme and sidebar state apply
   before first paint (no light flash for dark-mode users). Kept external
   because the Content-Security-Policy forbids inline scripts. */
(function () {
  "use strict";
  var root = document.documentElement;
  var theme = null;
  var sidebar = null;
  try {
    theme = window.localStorage.getItem("smart-dit-theme");
    sidebar = window.localStorage.getItem("smart-dit-sidebar");
  } catch (error) { /* storage unavailable: fall back to defaults */ }
  if (theme !== "dark" && theme !== "light") {
    theme = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  root.setAttribute("data-theme", theme);
  if (sidebar === "collapsed") root.setAttribute("data-sidebar", "collapsed");
})();
