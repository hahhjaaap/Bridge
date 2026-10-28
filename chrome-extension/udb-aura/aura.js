"use strict";
(() => {
  const id = "__udb_aura_badge_v1";
  if (document.getElementById(id) || !document.documentElement) return;
  const host = document.createElement("div");
  host.id = id;
  host.setAttribute("aria-hidden", "true");
  host.style.cssText = "position:fixed!important;top:12px!important;right:12px!important;"
    + "height:30px!important;z-index:2147483647!important;"
    + "pointer-events:none!important;user-select:none!important;"
    + "margin:0!important;padding:0!important;border:0!important;";
  const root = host.attachShadow({mode: "closed"});
  const style = document.createElement("style");
  style.textContent = `
    .label {height:30px;display:flex;align-items:center;gap:8px;padding:0 12px;
      color:#edf5f3;background:#111b20f2;border:1px solid #385448;border-radius:15px;
      box-shadow:0 3px 12px #0002;backdrop-filter:blur(10px);
      font:600 11px/1.3 "Segoe UI",system-ui,sans-serif;letter-spacing:.4px;}
    .dot {width:5px;height:5px;border-radius:50%;background:#78dfbc;}
  `;
  const label = document.createElement("div");
  label.className = "label";
  const dot = document.createElement("span");
  dot.className = "dot";
  const text = document.createElement("span");
  text.textContent = "UDB · AI";
  label.append(dot, text);
  root.append(style, label);
  document.documentElement.appendChild(host);
  if (document.head) {
    const favicon = document.createElement("link");
    favicon.rel = "icon";
    favicon.type = "image/svg+xml";
    favicon.dataset.udbAuraFavicon = "1";
    const svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40">'
      + '<rect x="1" y="1" width="38" height="38" rx="12" fill="#151e24" stroke="#385c50"/>'
      + '<path d="M20 8L23 17L32 20L23 23L20 32L17 23L8 20L17 17Z" fill="#78dfbc"/></svg>';
    favicon.href = "data:image/svg+xml," + encodeURIComponent(svg);
    document.head.appendChild(favicon);
  }
})();
