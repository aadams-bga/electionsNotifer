/* If this device has a saved manage token, "Manage alerts" links go straight to
   the manage page instead of the email sign-in form (covers push-only users). */
(function () {
  "use strict";
  var token = localStorage.getItem("manage_token");
  if (!token) return;
  var links = document.querySelectorAll("[data-manage-link]");
  for (var i = 0; i < links.length; i++) {
    links[i].href = "/manage?token=" + encodeURIComponent(token);
  }
})();
