(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);

  // Build DOM without ever putting server or TikTok text through innerHTML.
  function el(tag, props = {}, ...kids) {
    const node = document.createElement(tag);
    for (const [key, val] of Object.entries(props)) {
      if (val == null || val === false) continue;
      if (key === "class") node.className = val;
      else if (key === "text") node.textContent = val;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), val);
      else node.setAttribute(key, val === true ? "" : val);
    }
    for (const kid of kids.flat()) if (kid != null) node.append(kid);
    return node;
  }

  const pad = (n) => String(n).padStart(2, "0");
  function clock(total) {
    const s = Math.max(0, Math.floor(total));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    return h ? `${h}:${pad(m)}:${pad(s % 60)}` : `${m}:${pad(s % 60)}`;
  }
  function bytes(n) {
    if (n >= 1 << 30) return `${(n / (1 << 30)).toFixed(1)} GB`;
    if (n >= 1 << 20) return `${(n / (1 << 20)).toFixed(n >= 100 << 20 ? 0 : 1)} MB`;
    return `${Math.max(1, Math.round(n / 1024))} KB`;
  }
  const when = (t) =>
    new Date(t * 1000).toLocaleString(undefined, { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" });

  async function api(path, { method = "GET", body } = {}) {
    const init = { method, headers: {} };
    if (method !== "GET") init.headers["X-Upright"] = "1";
    if (body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(path, init);
    } catch {
      throw new Error("Can't reach the app. Check that it's still running.");
    }
    let data = null;
    try { data = await res.json(); } catch { /* not JSON */ }
    if (!res.ok) throw new Error((data && data.error) || `The app returned an error (${res.status}).`);
    return data;
  }

  // ---------- notices ----------
  function notify(message, kind = "ok") {
    const item = el("li", { class: `notice ${kind}`, role: kind === "error" ? "alert" : null },
      el("span", { text: message }),
      el("button", { class: "linkish", type: "button", text: "Dismiss", onclick: () => item.remove() }));
    $("#notices").append(item);
    if (kind !== "error") setTimeout(() => item.remove(), 8000);
  }

  // ---------- command line ----------
  const ui = { busy: false };

  async function withBusy(button, busyText, work) {
    if (ui.busy) return;
    ui.busy = true;
    const label = button.textContent;
    const all = document.querySelectorAll("#rec-form .btn, #check-result .btn");
    all.forEach((b) => (b.disabled = true));
    button.textContent = busyText;
    try {
      await work();
    } catch (err) {
      notify(err.message, "error");
    } finally {
      ui.busy = false;
      button.textContent = label;
      all.forEach((b) => (b.disabled = false));
    }
  }

  async function record(name) {
    const s = await api("/api/rec", { method: "POST", body: { username: name } });
    $("#username").value = "";
    $("#check-result").replaceChildren();
    notify(`Recording @${s.username}.`);
    await refreshStatus(true);
  }

  async function check(name) {
    const r = await api("/api/check", { method: "POST", body: { username: name } });
    renderCheck(r);
  }

  function renderCheck(r) {
    const box = $("#check-result");
    box.replaceChildren();
    if (!r.live) {
      box.append(el("p", { class: "check-off", text: r.message }));
      return;
    }
    const plan = r.plan;
    let planText;
    if (plan.mode === "vertical") planText = `Will record the ${plan.width}×${plan.height} vertical stream.`;
    else if (plan.mode === "crop") planText = `No vertical stream found. Will crop the ${plan.width}×${plan.height} landscape picture to 9:16.`;
    else planText = plan.message;

    const layouts = r.layouts.map((l) =>
      el("li", { class: l.vertical ? "is-vertical" : "", text: `${l.width}×${l.height} ${l.vertical ? "vertical" : "landscape"}` }));

    const cover = r.cover
      ? el("div", { class: "cover" }, el("img", { src: r.cover, alt: "", referrerpolicy: "no-referrer" }))
      : null;

    const actions = el("div", { class: "row" });
    if (plan.mode !== "none") {
      const go = el("button", { type: "button", class: "btn btn-rec", text: `Record @${r.username}` });
      go.addEventListener("click", () => withBusy(go, "Finding the live…", () => record(r.username)));
      actions.append(go);
    }
    actions.append(el("button", { type: "button", class: "linkish", text: "Dismiss", onclick: () => box.replaceChildren() }));

    box.append(el("div", { class: "check" },
      cover,
      el("div", {},
        el("h3", { class: "cond", text: `@${r.username} is live` }),
        r.title ? el("p", { text: r.title }) : null,
        r.viewers != null ? el("p", { class: "meta", text: `${r.viewers.toLocaleString()} watching` }) : null,
        layouts.length ? el("ul", {}, layouts) : null,
        el("p", { text: planText }),
        actions)));
  }

  $("#rec-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const name = $("#username").value.trim();
    if (!name) return $("#username").focus();
    withBusy($("#btn-rec"), "Finding the live…", () => record(name));
  });
  $("#btn-check").addEventListener("click", () => {
    const name = $("#username").value.trim();
    if (!name) return $("#username").focus();
    withBusy($("#btn-check"), "Checking…", () => check(name));
  });

  // ---------- active recordings ----------
  const frames = new Map(); // lowercase username -> parts of that card

  function buildFrame(s) {
    const user = s.username.toLowerCase();
    const timer = el("span", { class: "timer", text: clock(s.seconds) });
    const badge = el("div", { class: "badge", role: "img", "aria-label": "Recording" },
      el("span", { class: "dot", "aria-hidden": "true" }), timer);
    const img = el("img", { class: "shot", alt: `Latest frame from @${s.username}'s live`, hidden: true });
    const title = el("p", { class: "frame-title" });
    const fit = el("p", { class: "fit" });
    const viewers = el("p", { class: "viewers" });
    const stop = el("button", { type: "button", class: "btn btn-small", text: "Stop" });
    stop.addEventListener("click", async () => {
      stop.disabled = true;
      try { await api(`/api/stop/${encodeURIComponent(user)}`, { method: "POST" }); await refreshStatus(true); }
      catch (err) { notify(err.message, "error"); stop.disabled = false; }
    });
    const watch = el("a", { class: "linkish", href: s.watch_url, target: "_blank", rel: "noopener noreferrer", text: "Watch on TikTok" });

    const root = el("article", { class: "frame" },
      el("div", { class: "viewfinder" },
        el("div", { class: "screen" }, img, badge, el("div", { class: "who", text: `@${s.username}` })),
        el("span", { class: "marks", "aria-hidden": "true" })),
      el("div", { class: "frame-info" }, title, fit, viewers, el("div", { class: "row" }, stop, watch)));

    return { root, user, timer, badge, img, title, fit, viewers, stop, base: s.seconds, at: Date.now(), hasPreview: false };
  }

  function updateFrame(f, s) {
    f.base = s.seconds;
    f.at = Date.now();
    f.title.textContent = s.title || "";
    f.title.hidden = !s.title;
    const lay = s.layout;
    f.fit.textContent = lay.cropped ? `Cropped to 9:16 from ${lay.width}×${lay.height}` : `Vertical ${lay.width}×${lay.height}`;
    f.fit.classList.toggle("cropped", lay.cropped);
    f.viewers.textContent = s.viewers != null ? `${s.viewers.toLocaleString()} watching` : "";
    f.viewers.hidden = s.viewers == null;
    f.stop.textContent = s.stopping ? "Stopping…" : "Stop";
    f.stop.disabled = s.stopping;
    f.badge.classList.toggle("stopping", s.stopping);
    if (s.preview && !f.hasPreview) { f.hasPreview = true; loadPreview(f); }
  }

  function loadPreview(f) {
    const probe = new Image();
    probe.onload = () => { f.img.src = probe.src; f.img.hidden = false; };
    probe.src = `/api/preview/${encodeURIComponent(f.user)}?t=${Date.now()}`;
  }

  function syncActive(list, limit) {
    const seen = new Set();
    for (const s of list) {
      const key = s.username.toLowerCase();
      seen.add(key);
      let f = frames.get(key);
      if (!f) {
        f = buildFrame(s);
        frames.set(key, f);
        $("#stage").append(f.root);
      }
      updateFrame(f, s);
    }
    for (const [key, f] of frames) {
      if (!seen.has(key)) { f.root.remove(); frames.delete(key); }
    }
    $("#ghost").hidden = frames.size > 0;
    $("#slots").textContent = frames.size ? `Recording ${frames.size} of ${limit}` : "Not recording";
  }

  setInterval(() => {
    for (const f of frames.values()) f.timer.textContent = clock(f.base + (Date.now() - f.at) / 1000);
  }, 1000);

  setInterval(() => {
    if (document.hidden) return;
    for (const f of frames.values()) if (f.hasPreview) loadPreview(f);
  }, 3000);

  // ---------- status polling ----------
  const poll = { boot: null, lastEvent: 0, running: false, offline: false };

  async function refreshStatus(force = false) {
    if (poll.running || (document.hidden && !force)) return;
    poll.running = true;
    try {
      const s = await api("/api/status");
      if (poll.offline) { poll.offline = false; $("#offline").hidden = true; refreshLibrary(); }
      const newest = s.events.reduce((m, e) => Math.max(m, e.id), 0);
      if (poll.boot !== s.boot) { poll.boot = s.boot; poll.lastEvent = newest; } // don't replay old events
      let libraryChanged = false;
      for (const ev of s.events) {
        if (ev.id <= poll.lastEvent) continue;
        poll.lastEvent = ev.id;
        notify(ev.message, ev.kind === "done" ? "ok" : "error");
        if (ev.file) libraryChanged = true;
      }
      syncActive(s.active, s.limit);
      if (libraryChanged) await refreshLibrary();
    } catch {
      poll.offline = true;
      $("#offline").hidden = false;
    } finally {
      poll.running = false;
    }
  }

  setInterval(refreshStatus, 2000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshStatus(true); });

  // ---------- library ----------
  async function refreshLibrary() {
    let items;
    try { items = (await api("/api/library")).items; } catch { return; }
    const list = $("#library");
    list.replaceChildren();
    if (!items.length) {
      list.append(el("li", { class: "empty", text: "Finished recordings show up here." }));
      return;
    }
    for (const it of items) {
      const url = `/files/${encodeURIComponent(it.username)}/${encodeURIComponent(it.name)}`;
      const meta = el("p", { class: "take-meta" },
        it.seconds != null ? el("span", { text: clock(it.seconds) }) : null,
        el("span", { text: bytes(it.size) }),
        el("span", { text: when(it.modified) }),
        it.playable ? null : el("span", { text: "mkv file, download to play" }));

      const actions = el("div", { class: "take-actions" },
        it.playable ? el("button", { type: "button", class: "btn btn-small", text: "Play", onclick: () => play(url, `@${it.username}`) }) : null,
        el("a", { class: "btn btn-small", href: `${url}?download=1`, download: it.name, text: "Download" }),
        el("button", {
          type: "button", class: "btn btn-small", text: "Delete",
          onclick: async () => {
            if (!confirm(`Delete this recording of @${it.username}? This can't be undone.`)) return;
            try {
              await api(`/api/library/${encodeURIComponent(it.username)}/${encodeURIComponent(it.name)}`, { method: "DELETE" });
              notify("Deleted.");
            } catch (err) { notify(err.message, "error"); }
            refreshLibrary();
          },
        }));

      list.append(el("li", { class: "take" },
        el("div", { class: "thumb" }, it.thumb ? el("img", { src: url.replace(/\.[^./]+$/, ".jpg"), alt: "", loading: "lazy" }) : null),
        el("div", { class: "take-main" },
          el("p", { class: "take-name", text: `@${it.username}` }),
          it.title ? el("p", { class: "take-title", text: it.title }) : null,
          meta),
        actions));
    }
  }

  // ---------- player ----------
  const dialog = $("#player");
  function play(url, label) {
    $("#player-title").textContent = label;
    const video = $("video", dialog);
    video.src = url;
    dialog.showModal();
    video.play().catch(() => {});
  }
  dialog.addEventListener("click", (e) => { if (e.target === dialog) dialog.close(); });
  dialog.addEventListener("close", () => {
    const video = $("video", dialog);
    video.pause();
    video.removeAttribute("src");
    video.load();
  });

  refreshStatus(true);
  refreshLibrary();
})();
