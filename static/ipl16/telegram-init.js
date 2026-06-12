// Telegram Mini App glue. Safe no-op when the page is opened in a normal
// browser (window.Telegram is undefined there).
(function () {
  var tg = window.Telegram && window.Telegram.WebApp;

  // Open external links through Telegram when available — window.open is
  // unreliable inside the in-app WebView. Falls back to a new tab otherwise.
  window.openExternal = function (url) {
    if (tg && typeof tg.openLink === "function") tg.openLink(url);
    else window.open(url, "_blank");
  };

  if (!tg) return;
  try {
    tg.ready();   // tell Telegram the Mini App has loaded
    tg.expand();  // use the full available height instead of a short sheet
  } catch (e) {}
})();
