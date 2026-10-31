"""Fixed DOM programs shared by owned-browser operations.

Callers may supply only validated locators and JSON-encoded values, never code.
"""

DOM_TRAVERSAL_SCRIPT = r"""
const MAX_SHADOW_ROOTS = 32, MAX_SCANNED_ELEMENTS = 10000, MAX_MATCHES = 10000;
let traversalTruncated = false, openShadowRootCount = 0;
const collectOpenRoots = () => {
  const roots = [document];
  let cursor = 0, scanned = 0, truncated = false;
  while (cursor < roots.length) {
    const root = roots[cursor++];
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
    let node;
    while ((node = walker.nextNode())) {
      scanned++;
      if (node.shadowRoot) {
        if (roots.length < MAX_SHADOW_ROOTS) roots.push(node.shadowRoot);
        else truncated = true;
      }
      if (scanned >= MAX_SCANNED_ELEMENTS) {
        truncated = true;
        break;
      }
    }
    if (scanned >= MAX_SCANNED_ELEMENTS) break;
  }
  return {roots, truncated};
};
const queryAll = selector => {
  const discovered = collectOpenRoots();
  traversalTruncated = discovered.truncated;
  openShadowRootCount = Math.max(0, discovered.roots.length - 1);
  const found = [];
  for (const root of discovered.roots) {
    let nodes;
    try {
      nodes = root === document
        ? document.querySelectorAll(selector)
        : root.querySelectorAll(selector);
    } catch (_) {
      return null;
    }
    for (const element of nodes) {
      found.push(element);
      if (found.length >= MAX_MATCHES) {
        traversalTruncated = true;
        return found;
      }
    }
  }
  return found;
};
const isInert = element => {
  let node = element;
  while (node) {
    if (node.nodeType === 1 && node.hasAttribute('inert')) return true;
    if (node.parentElement) node = node.parentElement;
    else {
      const root = node.getRootNode && node.getRootNode();
      node = root && root.host ? root.host : null;
    }
  }
  return false;
};
"""

SELECT_OPTION_SCRIPT = """if(e.tagName!=='SELECT')return {ok:false,error:'unsupported_control'};
              if(e.matches(':disabled')||e.getAttribute('aria-disabled')==='true'||isInert(e))return {ok:false,error:'disabled'};
              const wanted=__VALUE__,options=Array.from(e.options);
              let choices=options.filter(o=>o.value===wanted);
              if(!choices.length)choices=options.filter(o=>o.label===wanted||String(o.textContent||'').trim()===wanted);
              if(!choices.length)return {ok:false,error:'option_not_found'};
              if(choices.length!==1)return {ok:false,error:'option_not_unique',matches:choices.length};
              const option=choices[0];if(option.matches(':disabled'))return {ok:false,error:'disabled'};
              Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(e,option.value);
              e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));
              const ok=e.value===option.value;
              return {ok,value:e.value,label:option.label||String(option.textContent||'').trim(),error:ok?'':'state_mismatch'};"""

CLICK_POINT_SCRIPT = """              e.scrollIntoView({block:'center',inline:'center',behavior:'instant'});
              const s=getComputedStyle(e);if(s.pointerEvents==='none')return {ok:false,error:'not_actionable',reason:'pointer_events'};
              const r=e.getBoundingClientRect();if(r.width<=0||r.height<=0)return {ok:false,error:'not_actionable',reason:'not_visible'};
              const cx=r.left+r.width/2,cy=r.top+r.height/2;
              const points=[[cx,cy],[r.left+Math.min(4,r.width/2),cy],[r.right-Math.min(4,r.width/2),cy],[cx,r.top+Math.min(4,r.height/2)],[cx,r.bottom-Math.min(4,r.height/2)]];
              const root=e.getRootNode(),hitRoot=root&&typeof root.elementFromPoint==='function'?root:document;
              const point=points.find(([x,y])=>x>=0&&y>=0&&x<innerWidth&&y<innerHeight&&(()=>{const top=hitRoot.elementFromPoint(x,y);return !!top&&(top===e||e.contains(top)||(root.host&&(top===root.host||root.host.contains(top))));})());
              if(!point)return {ok:false,error:'not_actionable',reason:'obscured'};
"""

LOCATOR_SCRIPT = """(() => {
          __TRAVERSAL__
          const selector = __SELECTOR__, wantedRole = __ROLE__, wantedName = __NAME__, exact = __EXACT__;
          const isVisible = e => { const s=getComputedStyle(e),r=e.getBoundingClientRect();
            return s.display!=='none'&&s.visibility!=='hidden'&&Number(s.opacity)>0&&r.width>0&&r.height>0; };
          const textOf = e => String(e.innerText||e.textContent||'').replace(/\\s+/g,' ').trim();
          const roleOf = e => {
            const explicit=(e.getAttribute('role')||'').trim().split(/\\s+/)[0]; if(explicit)return explicit;
            const tag=e.tagName.toLowerCase(),type=(e.getAttribute('type')||'').toLowerCase();
            if(tag==='a'&&e.hasAttribute('href'))return 'link';
            if(tag==='button'||tag==='summary'||['button','submit','reset','image'].includes(type))return 'button';
            if(tag==='textarea'||e.isContentEditable)return 'textbox';
            if(tag==='select')return e.multiple?'listbox':'combobox';
            if(tag==='input'){if(type==='checkbox')return 'checkbox';if(type==='radio')return 'radio';
              if(type==='range')return 'slider';if(type==='number')return 'spinbutton';if(type==='search')return 'searchbox';
              if(type==='hidden')return 'none';if(type==='file')return 'button';return 'textbox';}
            if(tag==='option')return 'option';if(/^h[1-6]$/.test(tag))return 'heading';return '';
          };
          const nameOf = e => {
            const ids=(e.getAttribute('aria-labelledby')||'').trim().split(/\\s+/).filter(Boolean);
            const root=e.getRootNode(),getById=id=>(root.getElementById&&root.getElementById(id))||document.getElementById(id);
            const labelled=ids.map(getById).filter(Boolean).map(textOf).join(' ').trim();
            const labels=e.labels?Array.from(e.labels).map(textOf).filter(Boolean).join(' '):'';
            return String(labelled||e.getAttribute('aria-label')||labels||e.getAttribute('alt')||e.getAttribute('title')
              ||e.getAttribute('placeholder')||textOf(e)||e.getAttribute('name')||e.id||'').trim().slice(0,500);
          };
          const findMatches = () => {
            const candidates=queryAll(selector||'a[href],button,input:not([type="hidden"]),textarea,select,option,summary,[role],[contenteditable="true"],[tabindex],h1,h2,h3,h4,h5,h6');
            if(candidates===null)return null;
            return selector?candidates:candidates.filter(e=>roleOf(e)===wantedRole&&(!wantedName||(exact?nameOf(e)===wantedName:nameOf(e).includes(wantedName))));
          };
          let matches=findMatches();
          if(matches===null)return {ok:false,error:'invalid_selector'};
          if(__VISIBLE_ONLY__)matches=matches.filter(e=>e.tagName==='OPTION'?!!e.closest('select')&&isVisible(e.closest('select')):isVisible(e));
          if(__UNIQUE__&&traversalTruncated)return {ok:false,error:'locator_search_truncated',matches:matches.length};
          if(__UNIQUE__&&matches.length!==1)return {ok:false,error:'locator_not_unique',matches:matches.length};
          const e=matches[0]||null;
          __BODY__
        })()"""
