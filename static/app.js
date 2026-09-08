/* app.js — One Piece local stream UI */
let EPS = [];
let FILTER = "all";
let QUERY = "";
let currentEp = null;
let saveTimer = null;
let nextQueued = false;

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
}

async function continueHero() {
  const c = await (await fetch("/api/continue")).json();
  const hero = $("#hero");
  if (!c.ep || !c.exists) { hero.classList.add("hidden"); return; }
  hero.classList.remove("hidden");
  $("#hero-sub").textContent = `Episode ${c.ep} — ${fmt(c.time)} watched`;
  $("#hero-play").onclick = () => openPlayer(c.ep, c.time);
}

function match(ep) {
  if (QUERY && !String(ep.ep).includes(QUERY)) return false;
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
function openPlayer(ep, startAt) {
  currentEp = ep;
  $("#player-ep").textContent = "EP " + ep;
  $("#player-title").textContent = "Episode " + ep;
  const v = $("#video");
  v.controls = true;
  v.src = `/video/${ep}`;
  $("#player-wrap").classList.remove("hidden");
  v.onloadedmetadata = () => {
    if (startAt > 0 && startAt < v.duration - 5) v.currentTime = startAt;
    v.play().catch(() => {});
  };
  v.ontimeupdate = () => {
    // after ~2 minutes of watching, prefetch the next episode
    if (!nextQueued && v.currentTime > 120) {
      nextQueued = true;
      const nx = EPS.find(x => x.ep === ep + 1);
      if (nx && !nx.exists) {
        fetch(`/api/download/${ep + 1}`, { method: "POST" }).then(poll).catch(() => {});
        toast(`Episode ${ep + 1} downloading for next`);
      }
    }
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => {
      fetch("/api/progress", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ep, time: v.currentTime, duration: v.duration || 0 }),
      });
    }, 800);
  };
}

function closePlayer() {
  const v = $("#video");
  v.pause();
  if (currentEp != null && v.duration) {
    fetch("/api/progress", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ep: currentEp, time: v.currentTime, duration: v.duration }),
    });
  }
  v.removeAttribute("src"); v.load();
  $("#player-wrap").classList.add("hidden");
  currentEp = null;
  load();
}
$("#player-close").onclick = closePlayer;

/* keyboard shortcuts (only while the player is open) */
document.addEventListener("keydown", (e) => {
  if ($("#player-wrap").classList.contains("hidden")) return;
  const v = $("#video");
  if (e.target.tagName === "INPUT") return;
  if (e.key === "Escape") { closePlayer(); return; }
  if (e.code === "Space") { e.preventDefault(); v.paused ? v.play() : v.pause(); }
  else if (e.key === "ArrowRight") v.currentTime = Math.min(v.duration || 0, v.currentTime + 10);
  else if (e.key === "ArrowLeft") v.currentTime = Math.max(0, v.currentTime - 10);
  else if (e.key.toLowerCase() === "f") {
    if (document.fullscreenElement) document.exitFullscreen();
    else v.requestFullscreen();
  }
});
$("#player-wrap").addEventListener("click", (e) => { if (e.target.id === "player-wrap") closePlayer(); });

/* ---------- filters + search ---------- */
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
  await load();
  refreshStats();
  setInterval(refreshStats, 10000);
}
boot();