"""Fixed DOM programs shared by owned-browser operations.

Callers may supply only validated locators and JSON-encoded values, never code.
"""

CLICK_POINT_SCRIPT = """              e.scrollIntoView({block:'center',inline:'center',behavior:'instant'});
              const s=getComputedStyle(e);if(s.pointerEvents==='none')return {ok:false,error:'not_actionable',reason:'pointer_events'};
              const r=e.getBoundingClientRect();if(r.width<=0||r.height<=0)return {ok:false,error:'not_actionable',reason:'not_visible'};
              const cx=r.left+r.width/2,cy=r.top+r.height/2;
              const points=[[cx,cy],[r.left+Math.min(4,r.width/2),cy],[r.right-Math.min(4,r.width/2),cy],[cx,r.top+Math.min(4,r.height/2)],[cx,r.bottom-Math.min(4,r.height/2)]];
              const point=points.find(([x,y])=>x>=0&&y>=0&&x<innerWidth&&y<innerHeight&&(()=>{const top=document.elementFromPoint(x,y);return !!top&&(top===e||e.contains(top));})());
              if(!point)return {ok:false,error:'not_actionable',reason:'obscured'};
"""

LOCATOR_SCRIPT = """(() => {
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
            const labelled=ids.map(id=>document.getElementById(id)).filter(Boolean).map(textOf).join(' ').trim();
            const labels=e.labels?Array.from(e.labels).map(textOf).filter(Boolean).join(' '):'';
            return String(labelled||e.getAttribute('aria-label')||labels||e.getAttribute('alt')||e.getAttribute('title')
              ||e.getAttribute('placeholder')||textOf(e)||e.getAttribute('name')||e.id||'').trim().slice(0,500);
          };
          let matches=[];
          if(selector){try{matches=Array.from(document.querySelectorAll(selector));}catch(_){return {ok:false,error:'invalid_selector'};}}
          else matches=Array.from(document.querySelectorAll('a[href],button,input:not([type="hidden"]),textarea,select,summary,[role],[contenteditable="true"],[tabindex],h1,h2,h3,h4,h5,h6'))
            .filter(e=>roleOf(e)===wantedRole&&(!wantedName||(exact?nameOf(e)===wantedName:nameOf(e).includes(wantedName))));
          if(__VISIBLE_ONLY__)matches=matches.filter(isVisible);
          if(__UNIQUE__&&matches.length!==1)return {ok:false,error:'locator_not_unique',matches:matches.length};
          const e=matches[0]||null;
          __BODY__
        })()"""
