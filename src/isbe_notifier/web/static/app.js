/* Signup + manage page behavior: committee search, push subscription, form submit. */
(function () {
  "use strict";

  const form = document.getElementById("signup-form") || document.getElementById("manage-form");
  if (!form) return;
  const isManage = form.id === "manage-form";
  const statusEl = document.getElementById("form-status");
  const vapidKey = form.dataset.vapidKey || "";

  // --- iOS hint: web push requires the PWA to be installed to the home screen ---
  const iosHint = document.getElementById("ios-hint");
  const isIos = /iphone|ipad|ipod/i.test(navigator.userAgent);
  const isStandalone = window.matchMedia("(display-mode: standalone)").matches ||
    window.navigator.standalone === true;
  if (iosHint && isIos && !isStandalone) iosHint.hidden = false;

  // --- committee search ---
  const q = document.getElementById("committee-q");
  const results = document.getElementById("committee-results");
  const chosen = document.getElementById("committee-chosen");
  let searchTimer = null;

  function chosenIds() {
    return Array.from(chosen.querySelectorAll("li")).map((li) => parseInt(li.dataset.id, 10));
  }

  function addCommittee(id, name) {
    if (chosenIds().includes(id)) return;
    const li = document.createElement("li");
    li.dataset.id = id;
    li.textContent = name + " ";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "remove";
    btn.textContent = "×";
    btn.setAttribute("aria-label", "Remove " + name);
    li.appendChild(btn);
    chosen.appendChild(li);
  }

  chosen.addEventListener("click", (e) => {
    if (e.target.classList.contains("remove")) e.target.closest("li").remove();
  });

  if (q) {
    q.addEventListener("input", () => {
      clearTimeout(searchTimer);
      const term = q.value.trim();
      if (term.length < 2) { results.hidden = true; return; }
      searchTimer = setTimeout(async () => {
        try {
          const resp = await fetch("/api/committees?q=" + encodeURIComponent(term));
          const data = await resp.json();
          results.innerHTML = "";
          for (const c of data.results) {
            const li = document.createElement("li");
            li.textContent = `${c.name} (#${c.id}${c.status ? ", " + c.status : ""})`;
            li.addEventListener("click", () => {
              addCommittee(c.id, c.name);
              results.hidden = true;
              q.value = "";
            });
            results.appendChild(li);
          }
          results.hidden = data.results.length === 0;
        } catch { /* search is best-effort */ }
      }, 250);
    });
  }

  // --- web push ---
  function b64ToUint8(base64) {
    const padding = "=".repeat((4 - (base64.length % 4)) % 4);
    const raw = atob((base64 + padding).replace(/-/g, "+").replace(/_/g, "/"));
    return Uint8Array.from(raw, (ch) => ch.charCodeAt(0));
  }

  async function enablePush(manageToken) {
    if (!("serviceWorker" in navigator) || !("PushManager" in window) || !vapidKey) {
      return "Push isn't supported in this browser.";
    }
    const reg = await navigator.serviceWorker.register("/static/sw.js");
    const permission = await Notification.requestPermission();
    if (permission !== "granted") return "Push permission was declined.";
    const sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: b64ToUint8(vapidKey),
    });
    const json = sub.toJSON();
    const resp = await fetch("/api/push/subscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        token: manageToken,
        endpoint: sub.endpoint,
        p256dh: json.keys.p256dh,
        auth: json.keys.auth,
      }),
    });
    if (!resp.ok) return "Could not save the push subscription.";
    return null;
  }

  // --- submit ---
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const submitBtn = form.querySelector("button[type=submit]");
    submitBtn.disabled = true;
    statusEl.textContent = "";

    const wantsEmail = document.getElementById("wants-email").checked;
    const wantsPush = document.getElementById("wants-push").checked;
    const emailInput = document.getElementById("email");
    const allFilings = document.getElementById("all-filings");
    const allCps = document.getElementById("all-cps");
    const dailyDigest = document.getElementById("daily-digest");
    const weeklyDigest = document.getElementById("weekly-digest");
    const payload = {
      email: emailInput && emailInput.value ? emailInput.value : null,
      wants_email: wantsEmail,
      wants_push: wantsPush,
      race_slugs: Array.from(form.querySelectorAll('input[name="race"]:checked'))
        .map((el) => el.value),
      committee_ids: chosenIds(),
      all_filings: !!(allFilings && allFilings.checked),
      all_cps: !!(allCps && allCps.checked),
      wants_daily_digest: !!(dailyDigest && dailyDigest.checked),
      wants_weekly_digest: !!(weeklyDigest && weeklyDigest.checked),
    };

    try {
      let manageToken;
      if (isManage) {
        payload.token = form.dataset.token;
        const resp = await fetch("/api/manage", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        if (!resp.ok) throw new Error((await resp.json()).detail || "Something went wrong.");
        manageToken = form.dataset.token;
        statusEl.textContent = "Saved.";
      } else {
        const resp = await fetch("/api/subscribe", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.detail || "Something went wrong.");
        manageToken = data.manage_token;
        localStorage.setItem("manage_token", manageToken);
        statusEl.textContent = data.needs_verification
          ? "Check your email to confirm your subscription!"
          : "You're signed up!";
      }

      if (wantsPush) {
        const pushError = await enablePush(manageToken);
        if (pushError) statusEl.textContent += " " + pushError;
        else if (!isManage) statusEl.textContent += " Push notifications enabled.";
      }
    } catch (err) {
      statusEl.textContent = err.message;
    } finally {
      submitBtn.disabled = false;
    }
  });
})();
