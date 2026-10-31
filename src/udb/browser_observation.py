"""Read-only bounded page observations for a UDB-owned browser.

No arbitrary caller JavaScript: these are fixed and data-redacting DOM programs.
"""


def read(browser, tab):
    return browser.evaluate(
        tab,
        """(() => {
          const textOf=e=>String(e.innerText||e.textContent||'').replace(/\\s+/g,' ').trim();
          const visible=e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();
            return s.display!=='none'&&s.visibility!=='hidden'&&Number(s.opacity)>0&&r.width>0&&r.height>0;};
          return {url:location.origin==='null'?'about:blank':location.origin+location.pathname,
            title:String(document.title||'').slice(0,500),
            text:(document.body?.innerText||'').slice(0,20000),
            elements:Array.from(document.querySelectorAll('a[href],button,input:not([type="hidden"]),textarea,select,[role]'))
              .filter(visible).slice(0,120).map(e=>{
                const type=(e.getAttribute('type')||'').toLowerCase();
                const secret=type==='password'||type==='file'||/password|secret|token|otp|one-time-code/i.test([e.autocomplete,e.name,e.id].join(' '));
                return {tag:e.tagName,id:String(e.id||'').slice(0,120),name:e.getAttribute('name'),type,
                  role:e.getAttribute('role')||'',placeholder:String(e.getAttribute('placeholder')||'').slice(0,160),
                  text:textOf(e).slice(0,200),disabled:e.matches(':disabled')||e.getAttribute('aria-disabled')==='true'||!!e.closest('[inert]'),readonly:!!e.readOnly,
                  options:e.tagName==='SELECT'&&!secret?Array.from(e.options).slice(0,20).map(o=>({value:String(o.value).slice(0,80),label:String(o.label).slice(0,120),disabled:o.matches(':disabled'),selected:o.selected})):undefined,
                  options_truncated:e.tagName==='SELECT'?e.options.length>20:undefined,
                  value:secret?'[redacted]':String(e.value||'').slice(0,300)};
              })};
        })()""",
        timeout=5,
    )


def snapshot(browser, tab):
    return browser.evaluate(
        tab,
        """(() => {
          const textOf=e=>String(e.innerText||e.textContent||'').replace(/\\s+/g,' ').trim();
          const roleOf=e=>{const x=(e.getAttribute('role')||'').trim().split(/\\s+/)[0];if(x)return x;
            const t=e.tagName.toLowerCase(),y=(e.getAttribute('type')||'').toLowerCase();
            if(t==='a'&&e.hasAttribute('href'))return 'link';if(t==='button'||t==='summary'||['button','submit','reset','image'].includes(y))return 'button';
            if(t==='textarea'||e.isContentEditable)return 'textbox';if(t==='select')return e.multiple?'listbox':'combobox';
            if(t==='input'){if(y==='checkbox')return 'checkbox';if(y==='radio')return 'radio';if(y==='range')return 'slider';if(y==='number')return 'spinbutton';if(y==='search')return 'searchbox';if(y==='hidden')return 'none';if(y==='file')return 'button';return 'textbox';}
            if(t==='option')return 'option';if(/^h[1-6]$/.test(t))return 'heading';return '';};
          const nameOf=e=>{const ids=(e.getAttribute('aria-labelledby')||'').trim().split(/\\s+/).filter(Boolean);
            const labelled=ids.map(id=>document.getElementById(id)).filter(Boolean).map(textOf).join(' ').trim();
            const labels=e.labels?Array.from(e.labels).map(textOf).filter(Boolean).join(' '):'';
            return String(labelled||e.getAttribute('aria-label')||labels||e.getAttribute('alt')||e.getAttribute('title')||e.getAttribute('placeholder')||textOf(e)||e.getAttribute('name')||e.id||'').trim().slice(0,240);};
          const visible=e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&Number(s.opacity)>0&&r.width>0&&r.height>0;};
          const all=Array.from(document.querySelectorAll('a[href],button,input:not([type="hidden"]),textarea,select,summary,[role],[contenteditable="true"],[tabindex],h1,h2,h3,h4,h5,h6')).filter(visible);
          const elements=all.slice(0,160).map((e,i)=>{const type=(e.getAttribute('type')||'').toLowerCase();
            const secret=type==='password'||type==='file'||/password|secret|token|otp|one-time-code/i.test([e.autocomplete,e.name,e.id].join(' '));
            let href='';try{const u=new URL(e.href,location.href);if(['http:','https:'].includes(u.protocol))href=u.origin+u.pathname;}catch(_){}
            return {index:i+1,role:roleOf(e),name:nameOf(e),tag:e.tagName.toLowerCase(),type,
              id:String(e.id||'').slice(0,120),name_attr:String(e.getAttribute('name')||'').slice(0,120),
              placeholder:String(e.getAttribute('placeholder')||'').slice(0,160),disabled:e.matches(':disabled')||e.getAttribute('aria-disabled')==='true'||!!e.closest('[inert]'),readonly:!!e.readOnly,
              options:e.tagName==='SELECT'&&!secret?Array.from(e.options).slice(0,20).map(o=>({value:String(o.value).slice(0,80),label:String(o.label).slice(0,120),disabled:o.matches(':disabled'),selected:o.selected})):undefined,
              options_truncated:e.tagName==='SELECT'?e.options.length>20:undefined,
              checked:typeof e.checked==='boolean'?e.checked:null,selected:typeof e.selected==='boolean'?e.selected:null,
              value:secret?'[redacted]':String(e.value||'').slice(0,160),href};});
          return {url:location.origin==='null'?'about:blank':location.origin+location.pathname,title:String(document.title||'').slice(0,500),
            count:all.length,truncated:all.length>160,elements};
        })()""",
        timeout=5,
    )
