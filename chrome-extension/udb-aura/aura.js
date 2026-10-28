"use strict";
(() => {
  const id = "__udb_aura_badge_v1";
  if (document.getElementById(id) || !document.documentElement) return;
  const host = document.createElement("div");
  host.id = id;
  host.setAttribute("aria-hidden", "true");
  host.style.cssText = "position:fixed!important;top:12px!important;right:14px!important;"
    + "width:112px!important;height:35px!important;z-index:2147483647!important;"
    + "pointer-events:none!important;user-select:none!important;"
    + "margin:0!important;padding:0!important;border:0!important;";
  const root = host.attachShadow({mode: "closed"});
  const style = document.createElement("style");
  style.textContent = `
    @keyframes udb-spin {to {transform:rotate(360deg)}}
    .frame {position:relative;width:112px;height:35px;isolation:isolate;
      filter:drop-shadow(0 3px 12px rgba(56,164,255,.35));}
    .frame:before {content:"";position:absolute;inset:-2px;border-radius:12px;
      background:conic-gradient(#17e9ff,#647aff,#e85dff,#ff5a9e,#17e9ff);
      animation:udb-spin 3.8s linear infinite;}
    .frame:after {content:"";position:absolute;inset:1px;border-radius:10px;
      background:#101a2b;}
    .label {position:absolute;z-index:1;inset:0;display:flex;align-items:center;
      justify-content:center;gap:7px;color:#f6fbff;
      font:700 12px/1 "Segoe UI",system-ui,sans-serif;letter-spacing:.4px;}
    .dot {width:7px;height:7px;border-radius:50%;background:#19e9ff;
      box-shadow:0 0 9px #19e9ff;}
    @media(prefers-reduced-motion:reduce){.frame:before{animation:none}}
  `;
  const frame = document.createElement("div");
  frame.className = "frame";
  const label = document.createElement("div");
  label.className = "label";
  const dot = document.createElement("span");
  dot.className = "dot";
  const text = document.createElement("span");
  text.textContent = "UDB · AI";
  label.append(dot, text);
  frame.append(label);
  root.append(style, frame);
  document.documentElement.appendChild(host);
  // Best-effort tab favicon identity. No page text or credentials are read.
  if (document.head) {
    const favicon = document.createElement("link");
    favicon.rel = "icon";
    favicon.type = "image/svg+xml";
    favicon.dataset.udbAuraFavicon = "1";
    const svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
      + '<defs><linearGradient id="g"><stop stop-color="#18e8ff"/>'
      + '<stop offset=".5" stop-color="#746eff"/>'
      + '<stop offset="1" stop-color="#ff5caa"/></linearGradient></defs>'
      + '<rect x="3" y="3" width="58" height="58" rx="15" fill="#111b30" '
      + 'stroke="url(#g)" stroke-width="5"/>'
      + '<text x="32" y="39" fill="white" font-family="Arial" '
      + 'font-size="19" font-weight="bold" text-anchor="middle">UDB</text></svg>';
    favicon.href = "data:image/svg+xml," + encodeURIComponent(svg);
    document.head.appendChild(favicon);
  }
})();
