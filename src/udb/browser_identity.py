"""Fixed presentation code for UDB-owned pages, evaluated in an isolated world.

The label is a visual aid, not a security boundary; ownership is established
by the private browser process/profile and tool response session IDs.
"""

IDENTITY_SCRIPT = r"""
(function installIdentity() {
  if (window.top !== window) return;
  if (document.readyState === 'loading' || !document.documentElement) {
    document.addEventListener('DOMContentLoaded', installIdentity, {once:true});
    return;
  }
  const id = '__udb_owned_browser_identity';
  if (document.getElementById(id) && globalThis.__udbMovePointer) return;
  document.getElementById(id)?.remove();
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
    .badge {position:absolute;right:14px;top:12px;padding:9px 13px;
      border:2px solid #27d8ef;border-radius:12px;background:#111b30;
      color:white;font:700 12px/1.3 system-ui,sans-serif;
      box-shadow:0 2px 12px #182f6680;}
    .pointer {position:absolute;left:24px;top:64px;display:flex;align-items:center;
      gap:4px;filter:drop-shadow(0 1px 3px #111b30);}
    .pointer svg {width:24px;height:28px;overflow:visible}
    .pointer span {background:#126579;color:white;border-radius:6px;
      padding:3px 6px;font:700 11px/1.3 system-ui,sans-serif;}
  `;
  const badge = document.createElement('div');
  badge.className = 'badge';
  badge.textContent = 'UDB • AI · Background browser';
  const pointer = document.createElement('div');
  pointer.className = 'pointer';
  // Static artwork only. No page content is interpolated into markup.
  pointer.innerHTML = '<svg viewBox="0 0 24 28" xmlns="http://www.w3.org/2000/svg">'
    + '<path d="M2 2L20 16L12 17L8 25Z" fill="#27d8ef" stroke="#fff" stroke-width="2"/>'
    + '</svg><span>AI</span>';
  root.append(style, badge, pointer);
  document.documentElement.appendChild(host);
  globalThis.__udbMovePointer = (x,y) => {
    if (!Number.isFinite(x) || !Number.isFinite(y)) return;
    pointer.style.left = Math.max(0,Math.min(innerWidth-24,x))+'px';
    pointer.style.top = Math.max(0,Math.min(innerHeight-28,y))+'px';
  };
  if (document.head && !document.querySelector('link[data-udb-owned-icon]')) {
    const icon = document.createElement('link');
    icon.rel = 'icon';
    icon.setAttribute('data-udb-owned-icon','true');
    icon.href = 'data:image/svg+xml,' + encodeURIComponent(
      '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40">'
      + '<rect width="40" height="40" rx="9" fill="#126579"/>'
      + '<text x="20" y="27" text-anchor="middle" fill="white" '
      + 'font-family="sans-serif" font-size="22" font-weight="bold">AI</text></svg>');
    document.head.appendChild(icon);
  }
})();
"""
