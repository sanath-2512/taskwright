// Renders the current page as compact, readable text for the LLM.
// Every interactive element gets a short ref (e1, e2, ...) stored in a
// data attribute, so actions can target exactly what the model saw.
// Refs are reassigned on every snapshot; old refs become invalid.
(maxChars) => {
  document.querySelectorAll('[data-tw-ref]').forEach(e => e.removeAttribute('data-tw-ref'));
  let n = 0;
  const lines = [];
  let cur = [];
  let prefix = '';
  const flush = () => {
    const s = cur.join(' ').replace(/[ \t ]+/g, ' ').replace(/ ([,.:;)])/g, '$1').trim();
    if (s) lines.push(prefix + s);
    cur = [];
  };
  const BLOCK = new Set(['ADDRESS','ARTICLE','ASIDE','BLOCKQUOTE','DD','DIV','DL','DT','FIELDSET','FIGCAPTION',
    'FIGURE','FOOTER','FORM','HEADER','HR','LI','MAIN','NAV','OL','P','PRE','SECTION','TABLE','TR','UL','BR',
    'THEAD','TBODY','TFOOT','DETAILS','SUMMARY','CAPTION','LEGEND']);
  const SKIP = new Set(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','SVG','HEAD','META','LINK','IFRAME','CANVAS','OBJECT']);
  const ROLES = new Set(['button','link','checkbox','radio','tab','menuitem','option','switch','combobox','textbox']);

  const hidden = el => {
    if (el.getAttribute('aria-hidden') === 'true') return true;
    const st = getComputedStyle(el);
    return st.display === 'none' || st.visibility === 'hidden';
  };
  const interactive = el => {
    const t = el.tagName;
    if (t === 'A') return el.hasAttribute('href');
    if (t === 'BUTTON' || t === 'SELECT' || t === 'TEXTAREA') return true;
    if (t === 'INPUT') return el.type !== 'hidden';
    const role = el.getAttribute('role');
    if (role && ROLES.has(role)) return true;
    return el.getAttribute('contenteditable') === 'true' || el.hasAttribute('onclick');
  };
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const q = s => '"' + clean(s).slice(0, 120).replace(/"/g, "'") + '"';
  const nameOf = el => {
    const aria = el.getAttribute('aria-label');
    if (aria) return clean(aria);
    const lb = el.getAttribute('aria-labelledby');
    if (lb) { const t = lb.split(/\s+/).map(id => document.getElementById(id)).filter(Boolean).map(x => x.innerText).join(' '); if (clean(t)) return clean(t); }
    if (el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) return clean(l.innerText); }
    const pl = el.closest('label'); if (pl) return clean(pl.innerText);
    if (el.placeholder) return clean(el.placeholder);
    if (el.title) return clean(el.title);
    return clean(el.name || '');
  };
  const shortHref = a => {
    try { const u = new URL(a.href, location.href); return u.origin === location.origin ? u.pathname + u.search : u.href; }
    catch (e) { return a.getAttribute('href'); }
  };
  const describe = el => {
    const ref = 'e' + (++n);
    el.setAttribute('data-tw-ref', ref);
    const t = el.tagName;
    const role = el.getAttribute('role');
    if (t === 'A') {
      const dl = el.hasAttribute('download') ? ' (downloads file)' : '';
      return `[${ref} link ${q(el.innerText || nameOf(el))} -> ${shortHref(el)}${dl}]`;
    }
    if (t === 'BUTTON' || (t === 'INPUT' && ['submit','button','reset'].includes(el.type)) || role === 'button') {
      const label = clean(el.innerText || el.value || nameOf(el));
      return `[${ref} button ${q(label)}${el.disabled ? ' disabled' : ''}]`;
    }
    if (t === 'SELECT') {
      const opts = Array.from(el.options).slice(0, 30).map(o => q(o.text)).join(' | ');
      const sel = el.selectedOptions[0] ? el.selectedOptions[0].text : '';
      return `[${ref} select ${q(nameOf(el))} selected=${q(sel)} options: ${opts}]`;
    }
    if (t === 'INPUT' && (el.type === 'checkbox' || el.type === 'radio')) {
      return `[${ref} ${el.type} ${q(nameOf(el))}${el.checked ? ' checked' : ''}]`;
    }
    if (t === 'INPUT' && el.type === 'file') return `[${ref} file-input ${q(nameOf(el))}]`;
    if (t === 'INPUT' || t === 'TEXTAREA' || el.getAttribute('contenteditable') === 'true') {
      let v = t === 'INPUT' || t === 'TEXTAREA' ? el.value : el.innerText;
      if (el.type === 'password') v = v ? '********' : '';
      const kind = t === 'TEXTAREA' ? 'textarea' : (el.type === 'password' ? 'password' : 'textbox');
      const vv = v ? ` value=${q(v.length > 160 ? v.slice(0, 160) + '…' : v)}` : ' (empty)';
      return `[${ref} ${kind} ${q(nameOf(el))}${vv}${el.disabled ? ' disabled' : ''}]`;
    }
    return `[${ref} ${role || t.toLowerCase()} ${q(el.innerText || nameOf(el))}]`;
  };

  const walk = (node, pre, cell) => {
    if (node.nodeType === Node.TEXT_NODE) {
      const text = node.textContent;
      if (!text.trim()) return;
      if (pre) {
        const parts = text.split('\n');
        parts.forEach((p, i) => { if (i > 0) flush(); if (p.trim()) cur.push(p.trim()); });
      } else cur.push(text.trim());
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    const el = node, t = el.tagName;
    if (SKIP.has(t)) return;
    if (t === 'LABEL' && el.htmlFor) return;           // the labelled control carries the text
    if (hidden(el)) return;
    if (interactive(el)) { cur.push(describe(el)); return; }
    const isPre = pre || getComputedStyle(el).whiteSpace.startsWith('pre');
    const heading = /^H([1-6])$/.exec(t);
    const role = el.getAttribute('role');
    if (heading || role === 'alert' || role === 'status' || t === 'LI') {
      flush();
      const saved = prefix;
      prefix = heading ? '#'.repeat(+heading[1]) + ' ' : role === 'alert' ? '[ALERT] ' : role === 'status' ? '[STATUS] ' : prefix + '- ';
      for (const c of el.childNodes) walk(c, isPre, cell);
      flush();
      prefix = saved;
      return;
    }
    const isCell = t === 'TD' || t === 'TH';
    if (isCell && el.cellIndex > 0) cur.push('|');
    if (t === 'DT') { flush(); }
    const block = BLOCK.has(t) && !(cell && t !== 'TABLE' && t !== 'TR');   // inside a table cell, keep it on one line
    if (block && t !== 'DD') flush();
    for (const c of el.childNodes) walk(c, isPre, cell || isCell);
    if (t === 'DT') { cur.push(':'); return; }
    if (block) flush();
  };
  walk(document.body, false, false);
  flush();
  let out = lines.join('\n');
  let truncated = false;
  if (out.length > maxChars) { out = out.slice(0, maxChars); truncated = true; }
  return { text: out, truncated, refs: n };
}
