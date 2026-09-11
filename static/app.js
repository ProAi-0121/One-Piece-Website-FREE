/* app.js — One Piece local stream UI + custom player */
let EPS = [];
let FILTER = "all";
let QUERY = "";
let currentEp = null;
let saveTimer = null;
let curIntro = null;
let curOutro = null;
let outroHandled = false;
let autoSkipPref = localStorage.getItem("autoSkipIntro") === "1";
let autoNextPref = localStorage.getItem("autoNext") !== "0";
let autoSkipOutroPref = localStorage.getItem("autoSkipOutro") !== "0";
let nextQueued = false;
let lastTap = { t: 0, x: null, timer: null };

const $ = (s) => document.querySelector(s);
const grid = $("#grid");

function fmt(t) {
  t = Math.floor(t || 0);
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
  return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(s).padStart(2, "0");
}

let toastTimer = null;
function toast(msg, ms) {
  ms = ms || 3000;
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), ms);
}

async function load() {
  EPS = await (await fetch("/api/episodes")).json();
  render();
  continueHero();
  const sy = parseInt(sessionStorage.getItem("scrollY") || "0", 10);
  if (sy > 0) requestAnimationFrame(() => window.scrollTo(0, sy));
}

function match(ep) {
  if (QUERY && !String(ep.ep).includes(QUERY) &&
      !(ep.title || "").toLowerCase().includes(QUERY.toLowerCase())) return false;
  if (FILTER === "downloaded") return ep.exists;
  if (FILTER === "watched") return ep.watched;
  if (FILTER === "unwatched") return ep.exists && !ep.watched;
  return true;
}

function render() {
  const list = EPS.filter(match);
  $("#empty").classList.toggle("hidden", list.length > 0);
  grid.innerHTML = "";
  const frag = document.createDocumentFragment();

  for (const ep of list) {
    const card = document.createElement("div");
    card.className = "card" + (ep.watched ? " watched" : "");
    card.dataset.ep = ep.ep;

    const pct = ep.pct || 0;
    const mark = `<button class="btn-mark${ep.watched ? " on" : ""}" data-mark="${ep.ep}" title="Mark watched / unwatched">${ep.watched ? "✓" : "○"}</button>`;
    const progline = ep.exists && pct
      ? `<div class="bar"><div style="width:${pct}%"></div></div>` : "";

    let dlBtn;
    if (ep.exists) {
      dlBtn = `<button class="btn-dl done" disabled>✓ On disk</button>`;
    } else {
      const j = ep.job;
      if (j && j.state !== "done" && j.state !== "error") {
        const p = j.total ? Math.round(100 * j.done / j.total) : 0;
        dlBtn = `<button class="btn-dl busy" disabled>${p}% — ${j.state}</button>`;
      } else if (j && j.state === "error") {
        dlBtn = `<button class="btn-dl" data-dl="${ep.ep}">↻ Retry</button>`;
      } else {
        dlBtn = `<button class="btn-dl" data-dl="${ep.ep}">⭳ Download</button>`;
      }
    }

    card.innerHTML = `
      <div class="num">Episode <b>${ep.ep}</b>${mark}</div>
      <div class="ep-title">${ep.title}</div>
      <div class="badges">${ep.exists ? `<span class="badge ok">READY</span>` : ""}<span class="badge hd">1080p</span><span class="badge dub">DUB</span>${ep.watched ? `<span class="badge watched-b">✓ WATCHED</span>` : ""}</div>
      ${progline}
      <div class="dl-info">${ep.exists ? (pct ? `Resume at ${fmt(ep.time)} · ${(ep.size/1e6).toFixed(0)} MB` : `${(ep.size/1e6).toFixed(0)} MB`) : (ep.job && ep.job.error ? ep.job.error : "Not downloaded yet")}</div>
      <div class="row">
        <button class="btn-play" data-play="${ep.ep}" ${ep.exists ? "" : "disabled"}>▶ Play</button>
        ${dlBtn}
      </div>`;
    frag.appendChild(card);
  }
  grid.appendChild(frag);
}

grid.addEventListener("click", async (e) => {
  const p = e.target.closest("[data-play]");
  if (p && !p.disabled) {
    const ep = EPS.find(x => x.ep == p.dataset.play);
    openPlayer(ep.ep, ep.time || 0);
    return;
  }
  const m = e.target.closest("[data-mark]");
  if (m) {
    await fetch(`/api/watched/${m.dataset.mark}`, { method: "POST" });
    load();
    return;
  }
  const d = e.target.closest("[data-dl]");
  if (d && !d.disabled) {
    await fetch(`/api/download/${d.dataset.dl}`, { method: "POST" });
    poll();
  }
});

async function continueHero() {
  const c = await (await fetch("/api/continue")).json();
  const hero = $("#hero");
  if (!c.ep) { hero.classList.add("hidden"); return; }
  hero.classList.remove("hidden");
  let where;
  if (c.finished) where = "Finished — replay";
  else if (c.time > 0) where = "Resume at " + fmt(c.time);
  else where = "Start from the beginning";
  if (!c.exists) where += " · needs download";
  $("#hero-sub").textContent = `Episode ${c.ep} — ${where}`;
  $("#hero-play").onclick = () => playOrFetch(c.ep, c.time);
}

function playOrFetch(ep, time) {
  const info = EPS.find(x => x.ep === ep);
  if (info && info.exists) { openPlayer(ep, time); return; }
  fetch(`/api/download/${ep}`, { method: "POST" })
    .then(() => { poll(); toast(`Downloading episode ${ep} — will start when ready`); })
    .catch(() => {});
  waitAndPlay(ep, time);
}

function waitAndPlay(ep, time) {
  const iv = setInterval(async () => {
    try {
      const d = await (await fetch("/api/episodes")).json();
      const e = d.find(x => x.ep === ep);
      if (e && e.exists) { clearInterval(iv); EPS = d; openPlayer(ep, time); }
      else if (e && e.job && e.job.state === "error") {
        clearInterval(iv);
        toast(`Download failed for episode ${ep}`);
      }
    } catch (err) {}
  }, 2000);
}

/* ================= PLAYER ================= */
const V = $("#video");
const stage = $("#stage");
const playerBox = $("#playerBox");

function setPlayIcon() {
  $("#cPlay").textContent = V.paused ? "▶" : "⏸";
}

function openPlayer(ep, startAt) {
  currentEp = ep;
  const info = EPS.find(x => x.ep === ep) || {};
  $("#player-ep").textContent = "EP " + ep;
  $("#player-title").textContent = info.title || "";
  $("#po-ep").textContent = "EP " + ep;
  $("#po-title").textContent = info.title || "";
  fetch(`/api/started/${ep}`, { method: "POST" }).catch(() => {});
  loadSubtitleText(ep, info.sub);
  V.src = `/video/${ep}`;
  $("#player-wrap").classList.remove("hidden");
  curIntro = (info.intro && info.intro.end > info.intro.start) ? info.intro : null;
  curOutro = (info.outro && info.outro.end > info.outro.start) ? info.outro : null;
  outroHandled = false;
  nextQueued = false;
  V.onloadedmetadata = () => {
    if (startAt > 0 && startAt < V.duration - 5) V.currentTime = startAt;
    else if (autoSkipPref && curIntro && V.currentTime < curIntro.start) V.currentTime = curIntro.end;
    V.play().catch(() => {});
  };
  V.ontimeupdate = onTimeUpdate;
  V.onplay = () => { setPlayIcon(); $("#pauseOverlay").classList.add("hidden"); };
  V.onpause = () => {
    setPlayIcon();
    if (!V.ended) $("#pauseOverlay").classList.remove("hidden");
  };
  syncPrefsUI();
  V.play().catch(() => {});
}

function closePlayer() {
  V.pause();
  if (currentEp != null && V.duration) {
    fetch("/api/progress", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ep: currentEp, time: V.currentTime, duration: V.duration }),
    });
  }
  V.removeAttribute("src"); V.load();
  $("#player-wrap").classList.add("hidden");
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  currentEp = null;
  load();
}

function nextEp() {
  if (currentEp == null) return;
  const nxt = EPS.find(x => x.ep === currentEp + 1);
  if (nxt && nxt.exists) openPlayer(nxt.ep, 0);
  else if (nxt) playOrFetch(nxt.ep, 0);
}

function onTimeUpdate() {
  const t = V.currentTime, d = V.duration || 0;
  if (d && !dragging) {
    $("#seekFill").style.width = (100 * t / d) + "%";
    $("#seekKnob").style.left = (100 * t / d) + "%";
    $("#cTime").textContent = fmt(t) + " / " + fmt(d);
  }
  try {
    if (V.buffered.length && d) {
      const end = V.buffered.end(V.buffered.length - 1);
      $("#seekBuf").style.width = (100 * end / d) + "%";
    }
  } catch (e) {}
  if (!nextQueued && t > 120) {
    nextQueued = true;
    const nx = EPS.find(x => x.ep === currentEp + 1);
    if (nx && !nx.exists) {
      fetch(`/api/download/${currentEp + 1}`, { method: "POST" }).then(poll).catch(() => {});
      toast(`Episode ${currentEp + 1} downloading for next`);
    }
  }
  const inIntro = curIntro && t >= curIntro.start && t < curIntro.end - 1;
  $("#btn-skipintro").classList.toggle("hidden", !inIntro);
  if (autoSkipPref && inIntro) V.currentTime = curIntro.end;
  const inOutro = curOutro && t >= curOutro.start && t < curOutro.end - 1;
  $("#btn-skipoutro").classList.toggle("hidden", !(inOutro && !outroHandled));
  if (curOutro && !outroHandled && t >= curOutro.start) {
    outroHandled = true;
    if (autoSkipOutroPref) {
      if (autoNextPref) { nextEp(); return; }
      V.currentTime = Math.min(curOutro.end, d - 1);
    }
  }
  if (autoNextPref && d && t >= d - 0.5 && !outroHandled) nextEp();
  $("#pauseOverlay").classList.toggle("hidden", !V.paused);
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    fetch("/api/progress", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ep: currentEp, time: t, duration: d }),
    });
  }, 800);
}

/* ---------- controls wiring ---------- */
$("#cPlay").onclick = () => V.paused ? V.play() : V.pause();
$("#cNext").onclick = nextEp;
$("#btn-next").onclick = nextEp;
$("#cMute").onclick = () => { V.muted = !V.muted; $("#cMute").textContent = V.muted ? "🔇" : "🔊"; };
$("#cVol").oninput = (e) => { V.volume = e.target.value / 100; V.muted = e.target.value == 0; };

V.addEventListener("click", (e) => {
  // double-tap on left/right half skips ±10s (works on touch + mouse);
  // a single tap toggles play/pause
  const now = Date.now();
  const half = e.clientX < innerWidth / 2 ? "L" : "R";
  if (now - lastTap.t < 300 && lastTap.x === half) {
    clearTimeout(lastTap.timer);
    lastTap.t = 0;
    V.currentTime = Math.min(V.duration || 1e9,
      Math.max(0, V.currentTime + (half === "L" ? -10 : 10)));
    return;
  }
  lastTap.t = now;
  lastTap.x = half;
  lastTap.timer = setTimeout(() => {
    lastTap.t = 0;
    V.paused ? V.play() : V.pause();
  }, 300);
});
$("#pauseOverlay").addEventListener("click", () => V.play());

/* seek: click + drag — visual scrub while dragging, commit on release */
const seek = $("#seek");
let dragging = false;
let dragTime = 0;
function seekFraction(clientX) {
  const r = seek.getBoundingClientRect();
  return Math.min(1, Math.max(0, (clientX - r.left) / r.width));
}
function paintSeek(f) {
  $("#seekFill").style.width = (f * 100) + "%";
  $("#seekKnob").style.left = (f * 100) + "%";
  if (V.duration) $("#cTime").textContent = fmt(f * V.duration) + " / " + fmt(V.duration);
}
seek.addEventListener("pointerdown", (e) => {
  dragging = true;
  try { seek.setPointerCapture(e.pointerId); } catch (err) {}
  const f = seekFraction(e.clientX);
  dragTime = f * (V.duration || 0);
  paintSeek(f);
});
seek.addEventListener("pointermove", (e) => {
  if (!dragging) return;
  const f = seekFraction(e.clientX);
  dragTime = f * (V.duration || 0);
  paintSeek(f);
});
function endDrag() {
  if (!dragging) return;
  dragging = false;
  if (V.duration) V.currentTime = dragTime;
}
seek.addEventListener("pointerup", endDrag);
seek.addEventListener("pointercancel", endDrag);

/* fullscreen */
function toggleFs() {
  if (document.fullscreenElement) document.exitFullscreen();
  else playerBox.requestFullscreen().catch(() => {});
}
$("#cFs").onclick = toggleFs;
document.addEventListener("fullscreenchange", () => {
  $("#cFs").textContent = document.fullscreenElement ? "⤡" : "⛶";
});

/* auto-next chip */
$("#cAutoNext").onclick = () => setPref("autoNext", !autoNextPref);

/* popup menus */
function togglePopup(id) {
  const el = $("#" + id);
  const wasOpen = !el.classList.contains("hidden");
  ["subMenu", "speedMenu", "settingsMenu"].forEach(x => $("#" + x).classList.add("hidden"));
  if (!wasOpen) el.classList.remove("hidden");
}
$("#cSettings").onclick = (e) => { e.stopPropagation(); togglePopup("settingsMenu"); };
$("#cSub").onclick = (e) => { e.stopPropagation(); togglePopup("subMenu"); };
$("#cSpeed").onclick = (e) => { e.stopPropagation(); togglePopup("speedMenu"); };
document.addEventListener("click", (e) => {
  if (e.target.closest(".ppopup") || e.target.closest("#cSettings") ||
      e.target.closest("#cSub") || e.target.closest("#cSpeed")) return;
  ["subMenu", "speedMenu", "settingsMenu"].forEach(x => $("#" + x).classList.add("hidden"));
});
$("#speeds").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-s]");
  if (!b) return;
  V.playbackRate = parseFloat(b.dataset.s);
  localStorage.setItem("speed", b.dataset.s);
  [...$("#speeds").children].forEach(x => x.classList.toggle("on", x === b));
  $("#cSpeed").textContent = b.dataset.s + "x";
});
["setAutoNext", "setSkipIntro", "setSkipOutro"].forEach((id, i) => {
  const keys = ["autoNext", "autoSkipIntro", "autoSkipOutro"];
  $("#" + id).onchange = (e) => setPref(keys[i], e.target.checked);
});

/* ---------- subtitle engine ---------- */
let subOffset = parseFloat(localStorage.getItem("subOffset") ?? "7.5");
if (isNaN(subOffset)) subOffset = 7.5;
let subSize = parseInt(localStorage.getItem("subSize") || "20", 10);
let subBg = parseInt(localStorage.getItem("subBg") || "50", 10);
let subsOn = localStorage.getItem("subsOn") !== "0";
let fsZoom = parseInt(localStorage.getItem("fsZoom") || "100", 10);
let currentVttText = null;
let lastSubUrl = null;

function shiftVtt(text, off) {
  if (!off) return text;
  return text.replace(/(\d{2}):(\d{2}):(\d{2})\.(\d{3})/g, (m, h, mi, sec, ms) => {
    let total = (+h) * 3600 + (+mi) * 60 + (+sec) + (+ms) / 1000 + off;
    if (total < 0) total = 0;
    const H = String(Math.floor(total / 3600)).padStart(2, "0");
    const M = String(Math.floor(total / 60) % 60).padStart(2, "0");
    const S = String(Math.floor(total % 60)).padStart(2, "0");
    const MS = String(Math.round((total % 1) * 1000)).padStart(3, "0");
    return H + ":" + M + ":" + S + "." + MS;
  });
}

function applySubtitleTrack() {
  const st = $("#subtrack");
  if (!currentVttText) { st.removeAttribute("src"); return; }
  if (lastSubUrl) URL.revokeObjectURL(lastSubUrl);
  const blob = new Blob([shiftVtt(currentVttText, subOffset)], { type: "text/vtt" });
  lastSubUrl = URL.createObjectURL(blob);
  st.src = lastSubUrl;
  st.track.mode = subsOn ? "showing" : "hidden";
}

function applySubStyle() {
  document.getElementById("substyle").textContent =
    "video::cue { font-size: " + subSize + "px; background: rgba(0,0,0," + (subBg / 100) + ") !important; }";
  $("#subSizeVal").textContent = subSize + "px";
  $("#subBgVal").textContent = subBg + "%";
}

function loadSubtitleText(ep, has) {
  currentVttText = null;
  if (!has) { applySubtitleTrack(); return; }
  fetch(`/sub/${ep}`).then(r => r.text()).then(txt => {
    currentVttText = txt;
    applySubtitleTrack();
  }).catch(() => applySubtitleTrack());
}

$("#subOn").onchange = (e) => {
  subsOn = e.target.checked;
  localStorage.setItem("subsOn", subsOn ? "1" : "0");
  $("#cSub").classList.toggle("on", subsOn);
  applySubtitleTrack();
};
$("#subOff").oninput = (e) => {
  let v = parseFloat(e.target.value);
  if (isNaN(v)) v = 0;
  subOffset = v;
  localStorage.setItem("subOffset", String(v));
  applySubtitleTrack();
};
function stepOffset(d) {
  const i = $("#subOff");
  const v = Math.round(((parseFloat(i.value) || 0) + d) * 100) / 100;
  i.value = v;
  subOffset = v;
  localStorage.setItem("subOffset", String(v));
  applySubtitleTrack();
}
$("#offMinus").onclick = () => stepOffset(-0.5);
$("#offPlus").onclick = () => stepOffset(0.5);
$("#subSize").oninput = (e) => {
  subSize = parseInt(e.target.value, 10);
  localStorage.setItem("subSize", String(subSize));
  applySubStyle();
};
$("#subBg").oninput = (e) => {
  subBg = parseInt(e.target.value, 10);
  localStorage.setItem("subBg", String(subBg));
  applySubStyle();
};
$("#fsZoom").oninput = (e) => {
  fsZoom = parseInt(e.target.value, 10);
  localStorage.setItem("fsZoom", String(fsZoom));
  applyFsZoom();
};
function applyFsZoom() {
  playerBox.style.setProperty("--fszoom", fsZoom / 100);
  $("#fsZoomVal").textContent = fsZoom + "%";
}

function setPref(name, val) {
  if (name === "autoNext") autoNextPref = val;
  if (name === "autoSkipIntro") autoSkipPref = val;
  if (name === "autoSkipOutro") autoSkipOutroPref = val;
  localStorage.setItem(name, val ? "1" : "0");
  syncPrefsUI();
  const labels = { autoNext: "Auto-next", autoSkipIntro: "Auto-skip intro", autoSkipOutro: "Auto-skip outro" };
  toast(labels[name] + (val ? " ON" : " OFF"));
}

function syncPrefsUI() {
  const map = { setAutoNext: autoNextPref, setSkipIntro: autoSkipPref, setSkipOutro: autoSkipOutroPref };
  for (const id in map) {
    const el = document.getElementById(id);
    if (el) el.checked = map[id];
  }
  const chip = $("#cAutoNext");
  if (chip) chip.classList.toggle("on", autoNextPref);
  const h = document.getElementById("autoNextT");
  if (h) { h.checked = autoNextPref; h.closest(".tgl").classList.toggle("on", autoNextPref); }
  const si = document.getElementById("skipIntroT");
  if (si) { si.checked = autoSkipPref; si.closest(".tgl").classList.toggle("on", autoSkipPref); }
}

/* skip buttons */
$("#btn-skipintro").onclick = () => { if (curIntro) V.currentTime = curIntro.end; };
$("#btn-skipoutro").onclick = () => {
  if (curOutro) V.currentTime = Math.min(curOutro.end, (V.duration || 0) - 1);
};

/* UI auto-hide in fullscreen + hover title */
let uiTimer = null;
stage.addEventListener("mousemove", () => {
  playerBox.classList.remove("hideui");
  clearTimeout(uiTimer);
  if (document.fullscreenElement) {
    uiTimer = setTimeout(() => playerBox.classList.add("hideui"), 2500);
  }
});
playerBox.addEventListener("mouseleave", () => {
  if (document.fullscreenElement) playerBox.classList.add("hideui");
});

/* close */
$("#player-close").onclick = closePlayer;

/* keyboard shortcuts (only while the player is open) */
document.addEventListener("keydown", (e) => {
  if ($("#player-wrap").classList.contains("hidden")) return;
  if (e.target.tagName === "INPUT") return;
  if (e.key === "Escape") { closePlayer(); return; }
  if (e.code === "Space") { e.preventDefault(); V.paused ? V.play() : V.pause(); }
  else if (e.key === "ArrowRight") V.currentTime = Math.min(V.duration || 0, V.currentTime + 10);
  else if (e.key === "ArrowLeft") V.currentTime = Math.max(0, V.currentTime - 10);
  else if (e.key === "ArrowUp") { V.volume = Math.min(1, V.volume + 0.1); $("#cVol").value = V.volume * 100; }
  else if (e.key === "ArrowDown") { V.volume = Math.max(0, V.volume - 0.1); $("#cVol").value = V.volume * 100; }
  else if (e.key.toLowerCase() === "m") { V.muted = !V.muted; $("#cMute").textContent = V.muted ? "🔇" : "🔊"; }
  else if (e.key.toLowerCase() === "f") toggleFs();
  else if (e.key.toLowerCase() === "n") nextEp();
});
$("#player-wrap").addEventListener("click", (e) => { if (e.target.id === "player-wrap") closePlayer(); });

/* remember scroll position across reloads */
window.addEventListener("scroll", () => {
  clearTimeout(window._scrollSave);
  window._scrollSave = setTimeout(() => {
    sessionStorage.setItem("scrollY", String(window.scrollY));
  }, 300);
}, { passive: true });
document.querySelectorAll(".filters button").forEach(b => {
  b.onclick = () => {
    document.querySelectorAll(".filters button").forEach(x => x.classList.remove("on"));
    b.classList.add("on");
    FILTER = b.dataset.f;
    render();
  };
});
$("#search").addEventListener("input", (e) => { QUERY = e.target.value.trim(); render(); });

/* ---------- live download polling ---------- */
let polling = null;
function poll() {
  clearInterval(polling);
  polling = setInterval(async () => {
    const busy = EPS.some(x => x.job && x.job.state !== "done" && x.job.state !== "error");
    await load();
    if (!busy) clearInterval(polling);
  }, 2000);
}

async function refreshStats() {
  const s = await (await fetch("/api/stats")).json();
  $("#stats").textContent =
    `${s.downloaded} eps · ${(s.bytes / 1e9).toFixed(1)} GB · ${s.watched} watched`;
}

async function boot() {
  /* header toggle pills */
  function bindTgl(id, initial, onChange) {
    const el = document.getElementById(id);
    if (!el) return;
    el.checked = initial;
    el.closest(".tgl").classList.toggle("on", initial);
    el.onchange = () => {
      el.closest(".tgl").classList.toggle("on", el.checked);
      onChange(el.checked);
    };
  }
  let autoDel = true;
  try {
    const s = await (await fetch("/api/settings")).json();
    autoDel = !!s.auto_delete;
  } catch (e) {}
  bindTgl("autoDel", autoDel, async (on) => {
    await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ auto_delete: on }),
    });
    toast(on ? "Auto-delete ON" : "Auto-delete OFF");
  });
  bindTgl("autoNextT", autoNextPref, (on) => setPref("autoNext", on));
  bindTgl("skipIntroT", autoSkipPref, (on) => setPref("autoSkipIntro", on));
  /* saved playback speed */
  const sp = parseFloat(localStorage.getItem("speed") || "1");
  if (sp !== 1) {
    V.playbackRate = sp;
    [...$("#speeds").children].forEach(x => x.classList.toggle("on", x.dataset.s == sp));
  }
  $("#cSpeed").textContent = (V.playbackRate || 1) + "x";
  /* subtitle prefs */
  $("#subOn").checked = subsOn;
  $("#subOff").value = subOffset;
  $("#subSize").value = subSize;
  $("#subBg").value = subBg;
  $("#cSub").classList.toggle("on", subsOn);
  applySubStyle();
  applyFsZoom();
  $("#fsZoom").value = fsZoom;
  syncPrefsUI();
  await load();
  refreshStats();
  setInterval(refreshStats, 10000);
}
boot();
