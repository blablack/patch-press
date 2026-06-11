#!/usr/bin/env python3
"""Audition a Deluge loop locally — hear it and *watch* the playhead loop.

Saves to copy onto the Deluge just to check a loop point. This builds a single
self-contained HTML file (no GUI / audio Python deps — just a browser) that:

  * plays the loop-expanded sample (so you HEAR the repeats), and
  * animates a playhead sweeping the loop region and snapping back at each wrap
    (Deluge hard-jumps loop_end -> loop_start; there is no on-device crossfade for
    osc/multisample loops, which is why patch-press bakes its own), and
  * shows a zoomed "seam" view of the exact samples played across the wrap
    (...loop_end-1, loop_start...) so a phase/shape discontinuity is visible, not
    just audible.

Usage:
    # From a rendered Deluge multisample XML, pick the sample by MIDI note:
    python tools/loop_audition.py "output/Deluge/SYNTHS/Mini From Mars/Grand Square.xml" --note 69

    # Or audition any WAV with explicit loop points (e.g. a candidate from analysis):
    python tools/loop_audition.py --wav "input/Mini From Mars/Grand Square/Mini_GrandSquare_A4.wav" \
        --loop-start 112205 --loop-end 200358

Options: --loops N (repeats baked into the audio, default 6), --out FILE, --no-open.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import re
import sys
import webbrowser
from pathlib import Path

import numpy as np
import soundfile as sf
from lxml import etree

WAVE_W = 1400  # waveform columns (min/max bars)


def _resolve_wav(xml_path: Path, file_name: str) -> Path:
    # file_name is SAMPLES-rooted (e.g. "SAMPLES/Dexed/preset/n.wav").
    # XML lives at <root>/SYNTHS/<plugin>/<preset>.xml, so the SD root is three levels up.
    return xml_path.parent.parent.parent / file_name


def _recorded_note(range_el) -> int | None:
    """The MIDI note this zone was captured at. The renderer stamps it into the
    filename (`noteNNN`), which is ground truth; fall back to the Deluge transpose
    convention (recorded = 48 - transpose) if the name has no note."""
    m = re.search(r"note(\d+)", range_el.get("fileName", ""))
    if m:
        return int(m.group(1))
    t = range_el.get("transpose")
    return 48 - int(t) if t is not None else None


def _range_for_note(xml_path: Path, note: int):
    root = etree.parse(str(xml_path)).getroot()
    ranges = root.findall(".//sampleRange")
    if not ranges:
        raise SystemExit(f"No <sampleRange> in {xml_path.name} — is it a multisample XML?")
    # Snap the requested note to the nearest *captured* sample so we always audition a
    # real recorded loop at its native pitch — not a between-zones note that Deluge would
    # pitch-shift. (Ties pick the lower note.) If no recorded-note info is available, fall
    # back to Deluge's range-containment mapping (which zone actually plays that note).
    keyed = [(rec, sr_el) for sr_el in ranges if (rec := _recorded_note(sr_el)) is not None]
    if keyed:
        _, chosen = min(keyed, key=lambda kv: (abs(kv[0] - note), kv[0]))
    else:
        chosen = ranges[-1]
        for sr_el in ranges:
            if note <= int(sr_el.get("rangeTopNote", "127")):
                chosen = sr_el
                break
    zone = chosen.find("zone")
    wav = _resolve_wav(xml_path, chosen.get("fileName", ""))
    has = zone is not None and "startLoopPos" in zone.attrib
    ls = int(zone.get("startLoopPos")) if has else None
    le = int(zone.get("endLoopPos")) if has else None
    sampled = _recorded_note(chosen)
    if sampled is None:
        sampled = 48 - int(chosen.get("transpose", "0"))
    return wav, ls, le, sampled


def _minmax(mono: np.ndarray, cols: int):
    """Per-column min/max for a waveform overview."""
    n = len(mono)
    edges = np.linspace(0, n, cols + 1).astype(int)
    mn = np.zeros(cols, dtype=np.float32)
    mx = np.zeros(cols, dtype=np.float32)
    for i in range(cols):
        seg = mono[edges[i]:edges[i + 1]]
        if len(seg):
            mn[i] = float(seg.min())
            mx[i] = float(seg.max())
    return mn.tolist(), mx.tolist()


def _build_playback(data: np.ndarray, ls, le, loops: int) -> np.ndarray:
    if ls is None or le is None or le <= ls:
        return data
    return np.concatenate([data[:ls]] + [data[ls:le]] * loops + [data[le:]])


def _b64_wav(playback: np.ndarray, sr: int) -> str:
    buf = io.BytesIO()
    sf.write(buf, playback, sr, format="WAV", subtype="PCM_16")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _seam(mono: np.ndarray, ls, le):
    """The waveform actually heard across one wrap: the tail before loop_end then the
    head after loop_start. A clean loop joins smoothly at the centre; a phase/shape
    mismatch shows as a kink right at the seam line."""
    if ls is None or le is None or le <= ls:
        return None
    z = int(min(1000, ls, (le - ls) // 2, len(mono) - le))
    if z < 8:
        return None
    pre = mono[le - z:le]
    post = mono[ls:ls + z]
    return {"z": z, "wrap": np.concatenate([pre, post]).tolist()}


_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>loop audition — {title}</title>
<style>
 body{{background:#111;color:#ddd;font:13px/1.4 system-ui,sans-serif;margin:0;padding:16px}}
 h1{{font-size:15px;margin:0 0 8px}} .meta{{color:#9ad;margin-bottom:10px}}
 canvas{{display:block;background:#181818;border:1px solid #333;width:100%}}
 .lbl{{margin:10px 0 4px;color:#888}} #read{{font-variant-numeric:tabular-nums}}
 b.g{{color:#5e5}} b.r{{color:#e66}} b.o{{color:#fb6}}
 audio{{width:100%;margin-top:12px}}
</style></head><body>
<h1>{title}</h1>
<div class="meta">{meta}</div>
<div id="read">—</div>
<div class="lbl">full sample &mdash; <b class="g">loop start</b> / <b class="r">loop end</b> / <b class="o">playhead</b> (loops in place)</div>
<canvas id="wave" height="220"></canvas>
<div class="lbl">seam &mdash; samples heard across the wrap (loop_end &rarr; loop_start); join at the centre line</div>
<canvas id="seam" height="180"></canvas>
<audio id="au" controls src="data:audio/wav;base64,{audio}"></audio>
<script>
const D = {data};
const au = document.getElementById('au');
function fit(c){{ c.width = c.clientWidth * devicePixelRatio; c.height = c.height * devicePixelRatio; }}
const wave = document.getElementById('wave'), seam = document.getElementById('seam');
fit(wave); fit(seam);
const wc = wave.getContext('2d'), sc = seam.getContext('2d');

function xOfSrc(p){{ return p / D.n * wave.width; }}

function drawWave(playSrc, passTxt){{
  const W = wave.width, H = wave.height, mid = H/2;
  wc.clearRect(0,0,W,H);
  // loop region shading
  if (D.ls != null) {{
    wc.fillStyle = 'rgba(90,160,90,0.12)';
    wc.fillRect(xOfSrc(D.ls), 0, xOfSrc(D.le)-xOfSrc(D.ls), H);
  }}
  // waveform min/max bars
  wc.strokeStyle = '#4a90d9'; wc.beginPath();
  for (let i=0;i<D.mn.length;i++){{
    const x = i/D.mn.length*W;
    wc.moveTo(x, mid - D.mx[i]*mid*0.95);
    wc.lineTo(x, mid - D.mn[i]*mid*0.95);
  }}
  wc.stroke();
  // loop boundary lines
  if (D.ls != null){{
    wc.strokeStyle='#5e5'; wc.beginPath(); wc.moveTo(xOfSrc(D.ls),0); wc.lineTo(xOfSrc(D.ls),H); wc.stroke();
    wc.strokeStyle='#e66'; wc.beginPath(); wc.moveTo(xOfSrc(D.le),0); wc.lineTo(xOfSrc(D.le),H); wc.stroke();
  }}
  // playhead
  if (playSrc != null){{
    const x = xOfSrc(playSrc);
    wc.strokeStyle='#fb6'; wc.lineWidth=2*devicePixelRatio;
    wc.beginPath(); wc.moveTo(x,0); wc.lineTo(x,H); wc.stroke(); wc.lineWidth=1;
  }}
}}

function drawSeam(){{
  const W = seam.width, H = seam.height, mid = H/2;
  sc.clearRect(0,0,W,H);
  if (!D.seam){{ sc.fillStyle='#666'; sc.fillText('no loop',10,20); return; }}
  const w = D.seam.wrap, n = w.length;
  let amp=1e-9; for (const v of w) amp = Math.max(amp, Math.abs(v));
  sc.strokeStyle='#3a3'; sc.beginPath(); sc.moveTo(0,mid); sc.lineTo(W,mid); sc.stroke();
  sc.strokeStyle='#ccd'; sc.beginPath();
  for (let i=0;i<n;i++){{ const x=i/n*W, y=mid - (w[i]/amp)*mid*0.9; i?sc.lineTo(x,y):sc.moveTo(x,y); }}
  sc.stroke();
  const sx = D.seam.z/n*W;  // the wrap point
  sc.strokeStyle='#fb6'; sc.beginPath(); sc.moveTo(sx,0); sc.lineTo(sx,H); sc.stroke();
}}

// map audio playback time -> source sample position + which pass we're on
function srcPos(t){{
  const p = t * D.sr;
  if (D.ls == null) return [p, 'one-shot'];
  const loopLen = D.le - D.ls;
  if (p < D.ls) return [p, 'intro'];
  const rel = p - D.ls, total = D.loops * loopLen;
  if (rel < total){{ const pass = Math.floor(rel/loopLen)+1; return [D.ls + (rel % loopLen), 'loop '+pass+'/'+D.loops]; }}
  return [D.le + (rel - total), 'release tail'];
}}

function fmt(s){{ return s.toFixed(3)+'s'; }}
let last=-1;
function frame(){{
  const [sp, pass] = srcPos(au.currentTime);
  drawWave(sp, pass);
  document.getElementById('read').innerHTML =
    'play '+fmt(au.currentTime)+' &nbsp; src '+Math.round(sp)+' ('+fmt(sp/D.sr)+') &nbsp; <b class="o">'+pass+'</b>'+
    (D.ls!=null ? ' &nbsp; loop ['+D.ls+', '+D.le+'] = '+fmt((D.le-D.ls)/D.sr) : '');
  requestAnimationFrame(frame);
}}
drawSeam(); drawWave(null,''); requestAnimationFrame(frame);
window.addEventListener('resize',()=>{{wave.width=wave.clientWidth*devicePixelRatio;seam.width=seam.clientWidth*devicePixelRatio;drawSeam();}});
</script></body></html>"""


def main() -> None:
    ap = argparse.ArgumentParser(description="Audition a Deluge loop in the browser (audio + looping playhead + seam).")
    ap.add_argument("xml", nargs="?", type=Path, help="Deluge multisample XML")
    ap.add_argument("--note", type=int, default=60, help="MIDI note to select from the XML (default 60)")
    ap.add_argument("--wav", type=Path, help="audition a WAV directly instead of an XML")
    ap.add_argument("--loop-start", type=int, help="loop start sample (with --wav)")
    ap.add_argument("--loop-end", type=int, help="loop end sample, exclusive (with --wav)")
    ap.add_argument("--loops", type=int, default=6, help="loop repeats baked into the audio (default 6)")
    ap.add_argument("--out", type=Path, help="output HTML path (default: temp file)")
    ap.add_argument("--no-open", action="store_true", help="don't open a browser, just write the file")
    args = ap.parse_args()

    if args.wav:
        wav_path, ls, le, sampled = args.wav, args.loop_start, args.loop_end, None
    elif args.xml:
        wav_path, ls, le, sampled = _range_for_note(args.xml, args.note)
        if sampled is not None and sampled != args.note:
            print(f"note {args.note} is off the capture grid -> snapped to nearest captured note {sampled}")
    else:
        ap.error("give an XML (with --note) or --wav")

    if not wav_path.exists():
        raise SystemExit(f"WAV not found: {wav_path}")

    data, sr = sf.read(str(wav_path), dtype="float32", always_2d=True)  # (N, ch)
    n = data.shape[0]
    mono = data.mean(axis=1)
    if ls is not None and (ls < 0 or le is None or le > n or le <= ls):
        raise SystemExit(f"bad loop points for {wav_path.name}: start={ls} end={le} (n={n})")

    playback = _build_playback(data, ls, le, args.loops)
    payload = {
        "sr": sr, "n": n, "ls": ls, "le": le, "loops": args.loops,
        "mn": _minmax(mono, WAVE_W)[0], "mx": _minmax(mono, WAVE_W)[1],
        "seam": _seam(mono, ls, le),
    }
    loop_txt = (f"loop [{ls}, {le}] = {(le - ls) / sr:.3f}s ({le - ls} samp)"
                if ls is not None else "no loop (one-shot)")
    meta = f"{wav_path.name} &middot; {sr} Hz &middot; {n / sr:.2f}s" + \
           (f" &middot; recorded note {sampled}" if sampled is not None else "") + \
           f" &middot; {loop_txt}"
    html = _HTML.format(
        title=wav_path.name, meta=meta,
        audio=_b64_wav(playback, sr), data=json.dumps(payload),
    )

    out = args.out or Path(__import__("tempfile").mktemp(suffix="_loop.html"))
    out.write_text(html, encoding="utf-8")
    print(f"{loop_txt}")
    print(f"wrote {out}  ({len(html) // 1024} KB)")
    if not args.no_open:
        try:
            webbrowser.open(out.resolve().as_uri())
        except Exception as e:  # headless / no browser — just leave the file
            print(f"(could not open a browser: {e})", file=sys.stderr)


if __name__ == "__main__":
    main()
