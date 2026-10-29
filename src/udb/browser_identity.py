"""Quiet visual identity for owned pages, evaluated only in an isolated world.

The decoration is not a security boundary. Ownership comes from the private
browser process/profile and session IDs, never from page pixels or labels.
"""

import json

from .ui_identity import CURSOR_SVG, ICON_SVG

_TEMPLATE = r"""
(function installIdentity() {
  if (window.top !== window) return;
  if (document.readyState === 'loading' || !document.documentElement) {
    document.addEventListener('DOMContentLoaded', installIdentity, {once:true});
    return;
  }
  const id = '__udb_owned_browser_identity';
  if (document.getElementById(id) && globalThis.__udbMovePointer) return;
  document.getElementById(id)?.remove();
  const ownerControl = __OWNER_CONTROL__;
  const host = document.createElement('div');
  host.id = id;
  host.setAttribute('aria-hidden', 'true');
  host.style.cssText = 'all:initial!important;position:fixed!important;inset:0!important;'
    + 'z-index:2147483647!important;pointer-events:none!important;'
    + 'user-select:none!important;display:block!important;';
  const root = host.attachShadow({mode:'closed'});
  const style = document.createElement('style');
  style.textContent = `
    :host, * {pointer-events:none!important;box-sizing:border-box}
    .badge {position:absolute;right:12px;top:12px;display:flex;align-items:center;
      gap:9px;max-width:calc(100vw - 24px);height:30px;padding:0 11px;
      border:1px solid #385448;border-radius:15px;background:#111b20f2;
      color:#edf5f3;font:600 11px/1.3 system-ui,sans-serif;
      box-shadow:0 3px 12px #0002;backdrop-filter:blur(10px);white-space:nowrap;}
    .dot {width:5px;height:5px;border-radius:50%;background:#78dfbc;flex-shrink:0}
    .brand {letter-spacing:.5px}.state {color:#b8c9cb;font-weight:400}
    .divider {width:1px;height:12px;background:#385448}
    .pointer {position:absolute;width:28px;height:32px;opacity:0;transition:opacity 140ms;
      filter:drop-shadow(0 1px 2px #0003)}
    .pointer svg {width:28px;height:32px;overflow:visible;transform-origin:3px 2px}
    .tag {position:absolute;left:29px;top:5px;padding:4px 8px;border-radius:8px;
      border:1px solid #385c50;background:#151e24;color:#b8f4de;
      font:700 10px/1.3 system-ui,sans-serif;letter-spacing:.4px}
    .pointer.left svg {transform:scaleX(-1)}
    .pointer.above svg {transform:scaleY(-1)}
    .pointer.left.above svg {transform:scale(-1,-1)}
    .pointer.left .tag {left:auto;right:35px}
    .pointer.above .tag {top:auto;bottom:36px}
    .owner {border-color:#3b5365}.owner .dot {background:#a8d4ee}
    @media(prefers-reduced-motion:reduce){.pointer{transition:none}}
  `;
  const badge = document.createElement('div');
  badge.className = 'badge' + (ownerControl ? ' owner' : '');
  const dot = document.createElement('span'); dot.className = 'dot';
  const brand = document.createElement('span'); brand.className = 'brand'; brand.textContent = 'UDB · AI';
  const divider = document.createElement('span'); divider.className = 'divider';
  const state = document.createElement('span'); state.className = 'state';
  const idleText = ownerControl ? 'Bạn điều khiển' : 'Chạy nền';
  state.textContent = idleText;
  badge.append(dot, brand, divider, state);
  const pointer = document.createElement('div');
  pointer.className = 'pointer';
  // Fixed artwork only; no page data or caller-supplied HTML.
  pointer.innerHTML = __CURSOR__ + '<span class="tag">AI</span>';
  root.append(style, badge, pointer);
  document.documentElement.appendChild(host);
  let idleTimer;
  globalThis.__udbMovePointer = (x,y) => {
    if (ownerControl || !Number.isFinite(x) || !Number.isFinite(y)) return;
    pointer.classList.toggle('left', x + 110 > innerWidth);
    pointer.classList.toggle('above', y + 45 > innerHeight);
    pointer.style.left = (x - 3)+'px'; pointer.style.top = (y - 2)+'px';
    pointer.style.opacity = '1'; state.textContent = 'Thao tác';
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => {pointer.style.opacity='0';state.textContent=idleText;}, 1800);
  };
  if (document.head && !document.querySelector('link[data-udb-owned-icon]')) {
    const icon = document.createElement('link');
    icon.rel = 'icon'; icon.setAttribute('data-udb-owned-icon','true');
    icon.href = 'data:image/svg+xml,' + encodeURIComponent(__ICON__);
    document.head.appendChild(icon);
  }
})();
""".replace("__CURSOR__", json.dumps(CURSOR_SVG)).replace(
    "__ICON__", json.dumps(ICON_SVG)
)


def identity_script(owner_control=False):
    return _TEMPLATE.replace("__OWNER_CONTROL__", "true" if owner_control else "false")


IDENTITY_SCRIPT = identity_script()
