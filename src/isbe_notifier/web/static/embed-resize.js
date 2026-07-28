// Tells the embedding page (e.g. a WordPress story) how tall this iframe's
// content is, so it can resize the iframe instead of showing a scrollbar.
// Paired with a small listener script on the embedding page.
(function () {
  if (window.self === window.top) return; // not framed; nothing to do

  function postHeight() {
    var height = document.documentElement.scrollHeight;
    window.parent.postMessage({ type: "iap-embed-height", height: height }, "*");
  }

  window.addEventListener("load", postHeight);
  new ResizeObserver(postHeight).observe(document.body);
})();
