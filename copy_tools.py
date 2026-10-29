"""Tables and charts with a Copy button and a PNG button.

    table(df, key, ...)   a dataframe (optionally styled) + "Copy table": puts it on the clipboard as tab-separated text,
                          which pastes straight into Excel, Outlook or Teams as a grid; and "PNG": saves it as an image under a
                          title and above a source line.
    chart(fig, key)       a Plotly chart + "Copy chart": renders the chart as a PNG and puts the image on the clipboard
                          (falls back to downloading the PNG if the browser will not let the page write images); and "PNG",
                          which always downloads it.

Charts and tables are saved from the browser's own drawing (nothing is rendered on the server), so what is saved is what is on
screen. An st.dataframe is a canvas grid with no cells in the page, so a table's PNG is drawn from the same rows as HTML (its
Styler when it has one, so the colours and the bold totals carry over) and measured off screen: every cell's box, borders and text
are read from the browser and painted on a canvas. Each item lives in a keyed container (st-key-copywrap_<key>) that the
buttons' script uses to find its chart.
"""

import copy
import datetime
import re

import streamlit as st

SOURCE = "Source: daily port line-up reports (US, Brazil, Argentina) and USDA FGIS vessel inspections"

_CSS = """
.wrap { display:flex; justify-content:flex-end; gap:6px; }
button { font: 600 11px 'Source Sans 3','Source Sans Pro',system-ui,sans-serif; letter-spacing:.04em; text-transform:uppercase;
         color:#5f7267; background:#ffffff; border:1px solid #d7e2dc; border-radius:6px; padding:3px 9px; cursor:pointer;
         display:inline-flex; align-items:center; gap:5px; line-height:1.4; }
button:hover { color:#0693e3; border-color:#0693e3; }
button.ok { color:#16a34a; border-color:#16a34a; }
button.err { color:#dc2626; border-color:#dc2626; }
svg { width:12px; height:12px; }
"""

_HTML = """
<div class="wrap"><button type="button" class="copy" title="Copy to clipboard">
<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
<span class="lbl">Copy</span></button>
<button type="button" class="png" title="Download as a PNG image">
<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg>
<span class="lbl">PNG</span></button></div>
"""

_JS = r"""
const SCALE = 2;
const PAD = 22;
const OFF_CSS = `
.jsa-png-off table { border-collapse: collapse; font: 12.5px Arial, sans-serif; color: #32373c; background: #fff; }
.jsa-png-off th { background: #eef3ef; color: #5f7267; font-weight: 700; font-size: 11px; text-transform: uppercase; letter-spacing: .04em;
                  padding: 7px 12px; border-bottom: 2px solid #d7e2dc; text-align: right; white-space: nowrap; }
.jsa-png-off td { padding: 6px 12px; border-bottom: 1px solid #e8eeea; text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }
.jsa-png-off th:first-child, .jsa-png-off td:first-child { text-align: left; }
`;

const PX = (v) => parseFloat(v) || 0;
const CLEAR = (c) => !c || c === 'transparent' || /rgba\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*,\s*0\s*\)/.test(c);
const toBlob = (canvas) => new Promise((res) => canvas.toBlob(res, 'image/png'));

async function copyText(t) {
  try { await navigator.clipboard.writeText(t); return true; }
  catch (e) {
    const ta = document.createElement('textarea');
    ta.value = t; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (_) {}
    ta.remove();
    return ok;
  }
}

async function chartBlob(gd) {
  const rect = gd.getBoundingClientRect(), scale = 2;
  const canvas = document.createElement('canvas');
  canvas.width = Math.round(rect.width * scale); canvas.height = Math.round(rect.height * scale);
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#ffffff'; ctx.fillRect(0, 0, canvas.width, canvas.height);
  for (const svg of gd.querySelectorAll('svg.main-svg')) {
    const r = svg.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    const clone = svg.cloneNode(true);
    clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
    clone.setAttribute('xmlns:xlink', 'http://www.w3.org/1999/xlink');
    clone.setAttribute('width', r.width); clone.setAttribute('height', r.height);
    const url = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(new XMLSerializer().serializeToString(clone));
    const img = await new Promise((res, rej) => { const i = new Image(); i.onload = () => res(i); i.onerror = rej; i.src = url; });
    ctx.drawImage(img, (r.left - rect.left) * scale, (r.top - rect.top) * scale, r.width * scale, r.height * scale);
  }
  return await toBlob(canvas);
}

function download(blob, name) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = name; document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

// ── a table: every box, border and line of text, read from the page ─────────────────────────────────────────────────────
function textLines(node) {
  // the text of one text node, line by line, each with the left / top / height of its first character
  const text = node.textContent, range = document.createRange(), out = [];
  let cur = null;
  for (let i = 0; i < text.length; i++) {
    range.setStart(node, i); range.setEnd(node, i + 1);
    const r = range.getClientRects()[0];
    if (!r) continue;                                           // collapsed whitespace has no box
    if (!cur || Math.abs(r.top - cur.top) > r.height * 0.6) {
      if (/\s/.test(text[i])) continue;                         // a line starts at its first visible character
      cur = { left: r.left, top: r.top, height: r.height, text: '' }; out.push(cur);
    }
    cur.text += text[i];
  }
  return out;
}

function edge(ctx, x1, y1, x2, y2, w, color, style) {
  if (w <= 0 || style === 'none' || style === 'hidden' || CLEAR(color)) return;
  ctx.save(); ctx.strokeStyle = color; ctx.lineWidth = w;
  if (style === 'dashed') ctx.setLineDash([4 * w, 2 * w]); else if (style === 'dotted') ctx.setLineDash([w, w]);
  ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke(); ctx.restore();
}

async function tableBlob(table, opt) {
  const tr = table.getBoundingClientRect();
  const W = Math.ceil(tr.width), H = Math.ceil(tr.height);
  const probe = document.createElement('canvas').getContext('2d');
  const TITLE_FONT = '700 21px Arial, sans-serif', SUB_FONT = '13px Arial, sans-serif', FOOT_FONT = '11px Arial, sans-serif';
  probe.font = TITLE_FONT; const wTitle = opt.title ? probe.measureText(opt.title).width : 0;
  probe.font = SUB_FONT; const wSub = opt.subtitle ? probe.measureText(opt.subtitle).width : 0;
  const width = Math.ceil(Math.max(W, wTitle, wSub)) + 2 * PAD;
  const headH = opt.title ? (opt.subtitle ? 74 : 52) : 0, footH = 46;
  const height = H + headH + footH + 2 * PAD;
  const canvas = document.createElement('canvas');
  canvas.width = width * SCALE; canvas.height = height * SCALE;
  const ctx = canvas.getContext('2d');
  ctx.scale(SCALE, SCALE);
  ctx.fillStyle = '#ffffff'; ctx.fillRect(0, 0, width, height);
  const ox = PAD, oy = PAD + headH;                             // where the table starts on the canvas

  if (opt.title) {
    ctx.textBaseline = 'top'; ctx.textAlign = 'left';
    ctx.font = TITLE_FONT; ctx.fillStyle = '#1e2124'; ctx.fillText(opt.title, PAD, PAD);
    if (opt.subtitle) { ctx.font = SUB_FONT; ctx.fillStyle = '#5f7267'; ctx.fillText(opt.subtitle, PAD, PAD + 30); }
    ctx.fillStyle = '#0693e3'; ctx.fillRect(PAD, PAD + headH - 14, width - 2 * PAD, 2);
  }

  // boxes, outermost first, so a cell's colour covers its row's
  const boxes = [table, ...table.querySelectorAll('thead, tbody, tfoot, tr, th, td')];
  for (const el of boxes) {
    const bg = getComputedStyle(el).backgroundColor;
    if (CLEAR(bg)) continue;
    const r = el.getBoundingClientRect();
    ctx.fillStyle = bg; ctx.fillRect(ox + r.left - tr.left, oy + r.top - tr.top, r.width, r.height);
  }
  // borders (a row's own border only shows when the table's borders are collapsed)
  const collapsed = getComputedStyle(table).borderCollapse === 'collapse';
  for (const el of table.querySelectorAll('tr, th, td')) {
    if (el.tagName === 'TR' && !collapsed) continue;
    const cs = getComputedStyle(el), r = el.getBoundingClientRect();
    const x = ox + r.left - tr.left, y = oy + r.top - tr.top, w = r.width, h = r.height;
    const t = PX(cs.borderTopWidth), b = PX(cs.borderBottomWidth), l = PX(cs.borderLeftWidth), rt = PX(cs.borderRightWidth);
    edge(ctx, x, y + t / 2, x + w, y + t / 2, t, cs.borderTopColor, cs.borderTopStyle);
    edge(ctx, x, y + h - b / 2, x + w, y + h - b / 2, b, cs.borderBottomColor, cs.borderBottomStyle);
    edge(ctx, x + l / 2, y, x + l / 2, y + h, l, cs.borderLeftColor, cs.borderLeftStyle);
    edge(ctx, x + w - rt / 2, y, x + w - rt / 2, y + h, rt, cs.borderRightColor, cs.borderRightStyle);
  }
  // text, line by line at the position the browser gave it
  const walker = document.createTreeWalker(table, NodeFilter.SHOW_TEXT);
  ctx.textBaseline = 'middle'; ctx.textAlign = 'left';
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (!node.textContent.trim()) continue;
    const cs = getComputedStyle(node.parentElement);
    if (cs.visibility === 'hidden' || cs.display === 'none') continue;
    ctx.font = `${cs.fontStyle} ${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
    ctx.fillStyle = cs.color;
    if ('letterSpacing' in ctx) ctx.letterSpacing = cs.letterSpacing === 'normal' ? '0px' : cs.letterSpacing;
    for (const ln of textLines(node)) {
      let s = ln.text.replace(/\s+$/, '');
      if (cs.textTransform === 'uppercase') s = s.toUpperCase();
      else if (cs.textTransform === 'lowercase') s = s.toLowerCase();
      else if (cs.textTransform === 'capitalize') s = s.replace(/\b\w/g, (c) => c.toUpperCase());
      ctx.fillText(s, ox + ln.left - tr.left, oy + ln.top - tr.top + ln.height / 2);
    }
  }
  if ('letterSpacing' in ctx) ctx.letterSpacing = '0px';

  // the source line and who made it
  ctx.textBaseline = 'top'; ctx.font = FOOT_FONT; ctx.fillStyle = '#5f7267';
  const fy = oy + H + 14;
  if (opt.source) ctx.fillText(opt.source, PAD, fy);
  const stamp = new Date().toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' });
  ctx.fillText('John Stewart & Associates · jpsi.com · ' + stamp, PAD, fy + (opt.source ? 16 : 0));
  return await toBlob(canvas);
}

async function htmlTableBlob(html, opt) {
  // the table is measured off screen, in the page, so its Styler's own <style> rules apply
  const host = document.createElement('div');
  host.className = 'jsa-png-off';
  host.style.cssText = 'position:fixed;left:-30000px;top:0;background:#fff;';
  host.innerHTML = '<style>' + OFF_CSS + '</style>' + html;
  document.body.appendChild(host);
  try {
    const table = host.querySelector('table');
    if (!table) throw new Error('no table');
    return await tableBlob(table, opt);
  } finally {
    host.remove();
  }
}

export default function (component) {
  const { data, parentElement } = component;
  const copyBtn = parentElement.querySelector('button.copy');
  const pngBtn = parentElement.querySelector('button.png');
  if (!copyBtn || !pngBtn) return;
  const copyLbl = copyBtn.querySelector('.lbl'), pngLbl = pngBtn.querySelector('.lbl');
  copyBtn._data = pngBtn._data = data || {};
  const copyName = () => copyBtn._data.kind === 'chart' ? 'Copy chart' : 'Copy table';
  copyLbl.textContent = copyName();
  if (copyBtn._wired) return;
  copyBtn._wired = true;
  const flash = (btn, lbl, cls, text, back) => {
    btn.classList.remove('ok', 'err'); if (cls) btn.classList.add(cls); lbl.textContent = text;
    setTimeout(() => { btn.classList.remove('ok', 'err'); lbl.textContent = back(); }, 1800);
  };
  const wrapOf = () => (parentElement.host || parentElement).closest('[class*="st-key-copywrap_"]');
  copyBtn.addEventListener('click', async () => {
    const d = copyBtn._data;
    const f = (cls, text) => flash(copyBtn, copyLbl, cls, text, copyName);
    try {
      if (d.kind === 'chart') {
        const wrap = wrapOf();
        const gd = wrap && wrap.querySelector('.js-plotly-plot');
        if (!gd) { f('err', 'No chart'); return; }
        const blob = await chartBlob(gd);
        try {
          await navigator.clipboard.write([new ClipboardItem({'image/png': blob})]);
          f('ok', 'Copied');
        } catch (e) {
          download(blob, (d.name || 'chart') + '.png');
          f('ok', 'Saved PNG');
        }
      } else {
        const ok = await copyText(d.text || '');
        f(ok ? 'ok' : 'err', ok ? 'Copied' : 'Failed');
      }
    } catch (e) {
      console.error('copy failed', e);
      f('err', 'Failed');
    }
  });
  pngBtn.addEventListener('click', async () => {
    const d = pngBtn._data;
    const f = (cls, text) => flash(pngBtn, pngLbl, cls, text, () => 'PNG');
    try {
      pngLbl.textContent = 'Saving…';
      let blob = null;
      if (d.kind === 'chart') {
        const wrap = wrapOf();
        const gd = wrap && wrap.querySelector('.js-plotly-plot');
        if (!gd) { f('err', 'No chart'); return; }
        blob = await chartBlob(gd);
      } else if (d.html) {
        blob = await htmlTableBlob(d.html, d);
      } else {
        f('err', 'No table'); return;
      }
      if (!blob) { f('err', 'Failed'); return; }
      download(blob, (d.name || 'jsa_export') + '.png');
      f('ok', 'Saved');
    } catch (e) {
      console.error('png export failed', e);
      f('err', 'Failed');
    }
  });
}
"""


def _define():
    return st.components.v2.component("jsa_copy_button", html=_HTML, css=_CSS, js=_JS)


_COPY = _define()

# ── what a PNG is called and says ───────────────────────────────────────────────────────────────────────────────────────────
_TITLES = {"snap_now": "Line-up now", "snap_mtd": "Sailed month-to-date",
           "combo_types": "Combo boats: what each combo type carries (learned shares)",
           "vet_history": "History of the combo-boat check against USDA"}
_PREFIX = (("sum_ct_", "Line-up by commodity, week / month / year change: "), ("sum_sailed_", "Sailed, week / month / year change: "),
           ("sum_area_", "Line-up by area, week / month / year change: "))
_SUFFIX = (("_ct", "Line-up, week / month / year change"), ("_ports", "Line-up by port, week / month / year change"),
           ("_sailed", "Sailed, week / month / year change"), ("_pivot", "Shipped by month and commodity"))


def _slug(text, limit=70):
    return re.sub(r"[^a-z0-9]+", "_", re.sub(r"<[^>]+>", " ", str(text or "")).lower()).strip("_")[:limit].strip("_") or "vessel_lineup"


def _table_title(key):
    """A table's title from its key (the page's headings are separate elements, so the table itself carries none)."""
    if key in _TITLES:
        return _TITLES[key]
    for prefix, text in _PREFIX:
        if key.startswith(prefix):
            return text + key[len(prefix):]
    for suffix, text in _SUFFIX:
        if key.endswith(suffix):
            return text
    return key.replace("_", " ").strip().capitalize()


def _subtitle():
    """What the page is showing (vessels or volume), read from its toggle: a table's numbers carry no unit of their own."""
    for k, v in st.session_state.items():
        if str(k).endswith("_measure") and isinstance(v, str):
            return f"Showing: {v}"
    return ""


def _table_html(df, styler, hide_index):
    """The table as HTML for its PNG: the Styler's (colours, bold totals) when there is one, else the plain frame."""
    try:
        if styler is not None:
            s = copy.deepcopy(styler)
            return (s.hide(axis="index") if hide_index else s).to_html()
        return df.to_html(index=not hide_index, border=0, na_rep="")
    except Exception:
        try:
            return df.to_html(index=not hide_index, border=0, na_rep="")
        except Exception:
            return ""


def _fig_title(fig):
    try:
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(fig.layout.title.text or ""))).strip()
    except Exception:
        return ""


def _button(kind, key, text="", name="", png=None):
    global _COPY
    data = {"kind": kind, "text": text, "name": name, **(png or {})}
    with st.container(horizontal=True, horizontal_alignment="right"):
        try:
            _COPY(data=data, key=f"copybtn_{key}", width="content")
        except Exception as e:                          # a fresh server/test runtime has an empty component registry
            if "not registered" not in str(e):
                raise
            _COPY = _define()
            _COPY(data=data, key=f"copybtn_{key}", width="content")


def table(df, key, styler=None, height=None, hide_index=True, wide=True, title=""):
    """Show `df` (or `styler`, built from it) with Copy and PNG buttons. `df` is what gets copied, so give it the formatted values.
    `title` heads the PNG (when it is not given, it comes from the key)."""
    title = title or _table_title(key)
    png = {"title": title, "subtitle": _subtitle(), "source": SOURCE, "html": _table_html(df, styler, hide_index)}
    with st.container(key=f"copywrap_{key}"):
        _button("table", key, df.to_csv(sep="\t", index=not hide_index, lineterminator="\n"),
                name=f"{_slug(title)}_{datetime.date.today().isoformat()}", png=png)
        with st.container(key=f"tablewrap_{key}"):          # the watermark overlay hangs on this container's class
            st.dataframe(styler if styler is not None else df, hide_index=hide_index, height=height,
                         width="stretch" if wide else "content")


def chart(fig, key):
    """Show a Plotly figure with Copy-as-image and PNG buttons."""
    with st.container(key=f"copywrap_{key}"):
        _button("chart", key, name=f"{_slug(_fig_title(fig) or key)}_{datetime.date.today().isoformat()}")
        st.plotly_chart(fig, key=f"fig_{key}", width="stretch")
