"""Minimized dialogs: drag their pill along the bottom, or up into a picture-in-picture window.

A minimized (keep-alive) dialog shows as a pill in the tray at the bottom left;
a click on it still brings the dialog back. The pill can also be dragged:

- Along the bottom, it stays a pill where it is dropped. Nothing goes below the tray.
- Lifted higher, it becomes a small window showing the dialog as it looks in
  front, scaled down. The window only shows: clicks never reach the page in it
  (no hanging up from there), a click brings the dialog back instead. Its
  corner handle resizes it, keeping its proportions.
- Pushed down to the bottom, the window becomes a pill again.

The window is the dialog's own element, restyled, so its page keeps running and
is never reloaded. Everything happens in the browser, without a round trip per
move: places are kept per dialog in sessionStorage and applied by a stylesheet
keyed by dialog id, so they outlast re-renders, page changes and reloads.
"""

import json

_TRAY_JS = """(() => {
  if (window.__ddnsIntentTray) return;
  window.__ddnsIntentTray = true;

  const KEY = "ddns-intent-tray";
  const SHOW_BUTTON = __SHOW_BUTTON_ID__;
  const FLOOR = 16;     // the tray's gap to the bottom: nothing goes lower
  const MARGIN = 8;     // windows stay this far inside the page
  const LIFT = 40;      // a pill lifted higher than this becomes a window
  const DOCK = 12;      // a window brought this close to the floor becomes a pill
  const START_W = 240;  // width of a new window
  const MIN_W = 160;
  const SLOP = 4;       // how far a press moves before it is a drag

  // dialog id -> {mode: "pill", dx} (moved along the bottom) or {mode: "pip", x, y, w}
  let places = {};
  try { places = JSON.parse(sessionStorage.getItem(KEY) || "{}") || {}; } catch (e) { places = {}; }
  const save = () => {
    // Dialog ids are never reused: keep the latest few.
    const ids = Object.keys(places).sort((a, b) => (places[b].t || 0) - (places[a].t || 0));
    for (const id of ids.slice(20)) delete places[id];
    try { sessionStorage.setItem(KEY, JSON.stringify(places)); } catch (e) {}
  };
  const put = (id, place) => { places[id] = {...place, t: Date.now()}; };

  const clamp = (v, lo, hi) => Math.max(lo, Math.min(v, hi));
  const dialogOf = (id) => document.querySelector(`[data-intent-dialog="${id}"]`);
  const pillOf = (id) => document.querySelector(`[data-intent-tray-item="${id}"]`);

  const BASE = "[data-intent-tray-item]{touch-action:none;cursor:grab;user-select:none;-webkit-user-select:none}"
    + "[data-intent-resize]{position:absolute;right:0;bottom:0;width:22px;height:22px;cursor:nwse-resize;"
    + "touch-action:none;background:linear-gradient(135deg,transparent 52%,rgba(15,23,42,.45) 52% 58%,"
    + "transparent 58% 68%,rgba(15,23,42,.45) 68% 74%,transparent 74%)}";
  const sheet = document.createElement("style");
  sheet.setAttribute("data-ddns-intent-tray", "");
  document.head.appendChild(sheet);

  // A window shows the dialog's box scaled down to the window's width.
  const sizes = new ResizeObserver(() => render());
  const watched = new WeakSet();
  const fit = (id, w) => {
    const dialog = dialogOf(id);
    const box = dialog && dialog.querySelector("[data-intent-box]");
    if (box && !watched.has(box)) { watched.add(box); sizes.observe(box); }
    const bw = (box && box.offsetWidth) || 800;
    const bh = (box && box.offsetHeight) || 480;
    w = Math.round(clamp(w, Math.min(MIN_W, bw), Math.min(bw, innerWidth - 2 * MARGIN)));
    return {w, h: Math.round(bh * w / bw), s: w / bw};
  };
  // Where a window shows: its place, kept inside the page and above the floor.
  const frame = (id) => {
    const place = places[id];
    const {w, h, s} = fit(id, place.w);
    const x = Math.round(clamp(place.x, MARGIN, innerWidth - w - MARGIN));
    const y = Math.round(clamp(place.y, MARGIN, innerHeight - h - FLOOR));
    return {x, y, w, h, s};
  };

  let retry = null;
  let retries = 0;
  const render = () => {
    let css = BASE;
    let missing = false;
    for (const id of Object.keys(places)) {
      if (!/^[A-Za-z0-9_-]+$/.test(id)) continue;
      const place = places[id];
      if (place.mode !== "pip") {
        if (place.dx) css += `[data-intent-tray-item="${id}"]{transform:translateX(${Math.round(place.dx)}px)}`;
        continue;
      }
      if (!dialogOf(id)) { missing = true; continue; }
      const {x, y, w, h, s} = frame(id);
      const win = `[data-intent-dialog="${id}"]:not([data-intent-front="1"])`;
      css += `${win}{inset:auto!important;left:${x}px!important;top:${y}px!important;`
        + `width:${w}px!important;height:${h}px!important;display:block!important;`
        + "background:transparent!important;visibility:visible!important;pointer-events:auto!important;"
        + "z-index:1001!important;overflow:hidden!important;border-radius:12px!important;"
        + "box-shadow:0 12px 32px rgba(15,23,42,.35)!important;cursor:grab;touch-action:none;"
        + "user-select:none;-webkit-user-select:none}"
        + `${win} [data-intent-box]{transform:scale(${s})!important;transform-origin:0 0!important;`
        + "pointer-events:none!important;box-shadow:none!important;border-radius:0!important}"
        + `${win} [data-intent-control]{display:none!important}`
        + `${win} [data-intent-resize]{display:block!important}`
        + `[data-intent-tray-item="${id}"]{display:none!important}`;
    }
    if (sheet.textContent !== css) sheet.textContent = css;
    // A window whose dialog is not on the page (yet, e.g. right after a reload):
    // look again shortly. One still missing after a while was closed.
    if (missing && !retry) {
      retry = setTimeout(() => {
        retry = null;
        if (++retries > 10) {
          for (const id of Object.keys(places)) {
            if (places[id].mode === "pip" && !dialogOf(id)) delete places[id];
          }
          save();
        }
        render();
      }, 1000);
    }
  };

  const show = (id) => {
    window.__ddnsIntentShowId = id;
    const button = document.getElementById(SHOW_BUTTON);
    if (button) button.click();
  };
  // The click that ends a drag, or lands on a window, is ours: it is neither a
  // click on the pill nor a click outside the dialog in front.
  let swallow = false;
  const swallowNextClick = () => { swallow = true; setTimeout(() => { swallow = false; }, 0); };
  addEventListener("click", (ev) => {
    if (!swallow) return;
    swallow = false;
    ev.preventDefault();
    ev.stopPropagation();
  }, true);

  // A pill's place in the tray, before any offset.
  const slotOf = (el) => {
    const parent = el.offsetParent;
    const r = parent ? parent.getBoundingClientRect() : {left: 0, top: 0};
    return {
      left: r.left + el.offsetLeft,
      bottom: r.top + el.offsetTop + el.offsetHeight,
      width: el.offsetWidth,
      height: el.offsetHeight,
    };
  };
  // While dragged, the element's bottom left corner stays at the pointer plus (ax, ab).
  const asPill = (d, x, y) => {
    const el = pillOf(d.id);
    if (!el) return;
    if (!d.slot) d.slot = slotOf(el);
    const left = clamp(x + d.ax, MARGIN, innerWidth - d.slot.width - MARGIN);
    const bottom = Math.min(y + d.ab, d.slot.bottom);
    d.left = left;
    d.lift = d.slot.bottom - bottom;
    el.style.transform = `translate(${left - d.slot.left}px, ${bottom - d.slot.bottom}px)`;
  };
  const asWindow = (d, x, y) => {
    const place = places[d.id];
    const {h} = fit(d.id, place.w);
    place.x = x + d.ax;
    place.y = y + d.ab - h;
    render();
  };
  const toWindow = (d, x, y) => {
    const el = pillOf(d.id);
    if (el) el.style.transform = "";
    put(d.id, {mode: "pip", x: 0, y: 0, w: START_W});
    const {w, h} = fit(d.id, START_W);
    d.ax = clamp(d.ax, 12 - w, -12);
    d.ab = clamp(d.ab, 12, h - 12);
    d.form = "pip";
    asWindow(d, x, y);
  };
  const toPill = (d, x, y) => {
    put(d.id, {mode: "pill", dx: 0});
    render();
    const el = pillOf(d.id);
    if (!el) return;
    d.slot = slotOf(el);
    d.ax = -d.slot.width / 2;
    d.ab = d.slot.height / 2;
    d.form = "pill";
    asPill(d, x, y);
  };

  let drag = null;
  document.addEventListener("pointerdown", (ev) => {
    if (ev.button !== 0 || drag) return;
    const handle = ev.target.closest("[data-intent-resize]");
    const pill = !handle && ev.target.closest("[data-intent-tray-item]");
    const win = !handle && !pill && ev.target.closest("[data-intent-dialog]");
    let id, kind;
    if (handle) { id = handle.dataset.intentResize; kind = "resize"; }
    else if (pill) { id = pill.dataset.intentTrayItem; kind = "pill"; }
    else if (win && win.dataset.intentFront !== "1") { id = win.dataset.intentDialog; kind = "pip"; }
    else return;
    if (kind !== "pill" && !(places[id] && places[id].mode === "pip")) return;
    const r = (handle || pill || win).getBoundingClientRect();
    drag = {
      id, kind, form: kind === "pill" ? "pill" : "pip", pointer: ev.pointerId,
      x0: ev.clientX, y0: ev.clientY, moved: false,
      ax: r.left - ev.clientX, ab: r.bottom - ev.clientY,
    };
    if (kind === "pill") drag.slot = slotOf(pill);
    if (kind === "resize") {
      const f = frame(id);
      Object.assign(drag, {w0: f.w, ratio: f.h / f.w, fx: f.x, fy: f.y});
    }
    if (kind !== "pill") ev.preventDefault();
  }, true);

  addEventListener("pointermove", (ev) => {
    const d = drag;
    if (!d || ev.pointerId !== d.pointer) return;
    if (!d.moved) {
      if (Math.abs(ev.clientX - d.x0) + Math.abs(ev.clientY - d.y0) < SLOP) return;
      d.moved = true;
      // Captured from now on only: a plain click must still reach the pill's buttons.
      // By the page itself, which stays while the pill turns into a window and back.
      const root = document.documentElement;
      try { root.setPointerCapture(ev.pointerId); } catch (e) {}
      root.style.cursor = d.kind === "resize" ? "nwse-resize" : "grabbing";
    }
    ev.preventDefault();
    const x = ev.clientX;
    const y = ev.clientY;
    if (d.kind === "resize") {
      const grow = Math.max(x - d.x0, (y - d.y0) / d.ratio);
      places[d.id].w = Math.min(d.w0 + grow, innerWidth - MARGIN - d.fx, (innerHeight - FLOOR - d.fy) / d.ratio);
      render();
    } else if (d.form === "pill") {
      asPill(d, x, y);
      if (d.lift > LIFT) toWindow(d, x, y);
    } else if (y + d.ab >= innerHeight - FLOOR - DOCK) {
      toPill(d, x, y);
    } else {
      asWindow(d, x, y);
    }
  }, true);

  const end = (ev) => {
    const d = drag;
    if (!d || ev.pointerId !== d.pointer) return;
    drag = null;
    document.documentElement.style.cursor = "";
    if (!d.moved) {
      // A click: a pill handles its own, a window brings the dialog back.
      if (d.kind === "pip" && ev.type === "pointerup") show(d.id);
      if (d.kind !== "pill") swallowNextClick();
      return;
    }
    swallowNextClick();
    if (d.form === "pill") {
      const el = pillOf(d.id);
      if (el) el.style.transform = "";
      put(d.id, {mode: "pill", dx: d.slot && d.left !== undefined ? d.left - d.slot.left : 0});
    } else {
      const f = frame(d.id);
      put(d.id, {mode: "pip", x: f.x, y: f.y, w: f.w});
    }
    render();
    save();
  };
  addEventListener("pointerup", end, true);
  addEventListener("pointercancel", end, true);
  addEventListener("resize", () => render());
  render();
})()"""


def tray_js(show_button_id: str) -> str:
    """Installs (once) the dragging of minimized dialogs; ``show_button_id`` brings one back."""
    return _TRAY_JS.replace("__SHOW_BUTTON_ID__", json.dumps(show_button_id))
