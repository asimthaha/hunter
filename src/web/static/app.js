// No inline scripts are allowed by the CSP, so UI glue lives here.
document.addEventListener("click", function (e) {
  var btn = e.target.closest("[data-copy]");
  if (!btn) return;
  var el = document.getElementById(btn.getAttribute("data-copy"));
  if (el && navigator.clipboard) navigator.clipboard.writeText(el.textContent);
});
document.addEventListener("htmx:responseError", function (e) {
  var flash = document.getElementById("flash");
  if (flash) flash.textContent = e.detail.xhr.responseText.slice(0, 300);
});
