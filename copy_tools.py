"""Tables and charts with a Copy button.

    table(df, key, ...)   a dataframe (optionally styled) + "Copy table": puts it on the clipboard as tab-separated text,
                          which pastes straight into Excel, Outlook or Teams as a grid.
    chart(fig, key)       a Plotly chart + "Copy chart": renders the chart as a PNG and puts the image on the clipboard
                          (falls back to downloading the PNG if the browser will not let the page write images).

The charts are copied from the browser's own drawing (nothing is rendered on the server), so what is copied is what is on
screen. Each item lives in a keyed container (st-key-copywrap_<key>) that the button's script uses to find its chart.
"""

import streamlit as st

_CSS = """
.wrap { display:flex; justify-content:flex-end; }
button { font: 600 11px 'Source Sans 3','Source Sans Pro',system-ui,sans-serif; letter-spacing:.04em; text-transform:uppercase;
         color:#5f7267; background:#ffffff; border:1px solid #d7e2dc; border-radius:6px; padding:3px 9px; cursor:pointer;
         display:inline-flex; align-items:center; gap:5px; line-height:1.4; }
button:hover { color:#0693e3; border-color:#0693e3; }
button.ok { color:#16a34a; border-color:#16a34a; }
button.err { color:#dc2626; border-color:#dc2626; }
svg { width:12px; height:12px; }
"""

_HTML = """
<div class="wrap"><button type="button" title="Copy to clipboard">
<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
<span class="lbl">Copy</span></button></div>
"""

_JS = """
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
  return await new Promise(res => canvas.toBlob(res, 'image/png'));
}

function download(blob, name) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = name; document.body.appendChild(a); a.click(); a.remove();
}

export default function (component) {
  const { data, parentElement } = component;
  const btn = parentElement.querySelector('button');
  const lbl = parentElement.querySelector('.lbl');
  if (!btn) return;
  btn._data = data || {};
  lbl.textContent = btn._data.kind === 'chart' ? 'Copy chart' : 'Copy table';
  if (btn._wired) return;
  btn._wired = true;
  const flash = (cls, text) => {
    btn.className = cls; lbl.textContent = text;
    setTimeout(() => { btn.className = ''; lbl.textContent = btn._data.kind === 'chart' ? 'Copy chart' : 'Copy table'; }, 1800);
  };
  btn.addEventListener('click', async () => {
    const d = btn._data;
    try {
      if (d.kind === 'chart') {
        const host = parentElement.host || parentElement;
        const wrap = host.closest('[class*="st-key-copywrap_"]');
        const gd = wrap && wrap.querySelector('.js-plotly-plot');
        if (!gd) { flash('err', 'No chart'); return; }
        const blob = await chartBlob(gd);
        try {
          await navigator.clipboard.write([new ClipboardItem({'image/png': blob})]);
          flash('ok', 'Copied');
        } catch (e) {
          download(blob, (d.name || 'chart') + '.png');
          flash('ok', 'Saved PNG');
        }
      } else {
        const ok = await copyText(d.text || '');
        flash(ok ? 'ok' : 'err', ok ? 'Copied' : 'Failed');
      }
    } catch (e) {
      console.error('copy failed', e);
      flash('err', 'Failed');
    }
  });
}
"""

def _define():
    return st.components.v2.component("jsa_copy_button", html=_HTML, css=_CSS, js=_JS)


_COPY = _define()


def _button(kind, key, text="", name=""):
    global _COPY
    with st.container(horizontal=True, horizontal_alignment="right"):
        try:
            _COPY(data={"kind": kind, "text": text, "name": name}, key=f"copybtn_{key}", width="content")
        except Exception as e:                          # a fresh server/test runtime has an empty component registry
            if "not registered" not in str(e):
                raise
            _COPY = _define()
            _COPY(data={"kind": kind, "text": text, "name": name}, key=f"copybtn_{key}", width="content")


def table(df, key, styler=None, height=None, hide_index=True, wide=True):
    """Show `df` (or `styler`, built from it) with a Copy button. `df` is what gets copied, so give it the formatted values."""
    with st.container(key=f"copywrap_{key}"):
        _button("table", key, df.to_csv(sep="\t", index=not hide_index, lineterminator="\n"))
        with st.container(key=f"tablewrap_{key}"):          # the watermark overlay hangs on this container's class
            st.dataframe(styler if styler is not None else df, hide_index=hide_index, height=height,
                         width="stretch" if wide else "content")


def chart(fig, key):
    """Show a Plotly figure with a Copy-as-image button."""
    with st.container(key=f"copywrap_{key}"):
        _button("chart", key, name=key)
        st.plotly_chart(fig, key=f"fig_{key}", width="stretch")
