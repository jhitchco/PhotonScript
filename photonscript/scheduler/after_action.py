"""PS-254: the after-action report, one per night, with pictures.

Jeremy, 2026-11-12: "I want the images and the text": the morning report
card (PS-166) is text only and Pushover cuts it to about 1,000 characters.
This builds the whole night as one page and sends it once after the dawn
shutdown:

  text         the PS-166 card, every line (morning_report.report_card)
  timeline     the night's timeline bars (night_timeline rows + the subs,
               kept / rejected) drawn server-side as a PNG with Pillow
  thumbnails   a few, not all: the best kept sub per rig and target (lowest
               HFR) and one typical reject per rig, labelled with its reason
  links        every goal's links (the progression contact sheets) from the
               project store

Where it goes (after_action_mode):
  push   (default) one Pushover with the timeline attached and the report
         page link, after_action_after_shutdown_min after the dawn shutdown
  email  the full HTML (images inline) by SMTP; Gmail works with an app
         password (smtp.gmail.com:587, user = the Gmail address, password =
         a Google app password, entered on the System page)
  both | off

GET /report/{date} is the page itself (images inline, works on the phone
over Tailscale); GET /api/runs/{date}/timeline.png the timeline alone;
POST /api/report/{date}/send sends it now (manual, ignores the once-a-night
latch). Reads only; never touches NINA, the mount or a setting.
"""
from __future__ import annotations

import asyncio
import base64
import html
import io
import json
import logging
import smtplib
from collections import Counter
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

logger = logging.getLogger(__name__)

MODES = ("push", "email", "both", "off")
THUMB_W = 360
TICK_S = 300

STATE_COLORS = {
    "exposing": (46, 160, 90), "kept": (46, 160, 90), "rejected": (205, 70, 60),
    "slewing": (232, 150, 40), "centering": (230, 200, 60),
    "autofocus": (150, 100, 210), "meridian flip": (220, 60, 140),
    "parked": (120, 125, 135), "waiting": (80, 86, 98), "idle": (70, 76, 88),
    "tracking": (60, 120, 200), "guiding": (60, 150, 210),
    "calibrating": (90, 180, 200), "cooling": (70, 180, 190),
    "flats": (230, 230, 230), "darks": (40, 40, 48), "unsafe": (190, 60, 60),
    "dither": (160, 160, 90), "guider": (60, 150, 210), "other": (110, 110, 120),
}
BG, FG, GRID, MUTED = (21, 25, 32), (226, 230, 236), (48, 55, 66), (150, 158, 170)


# ------------------------------------------------------------------ config

def mode(cfg) -> str:
    m = str(getattr(cfg, "after_action_mode", "push") or "push").strip().lower()
    return m if m in MODES else "push"


def after_min(cfg) -> float:
    try:
        return max(0.0, float(getattr(cfg, "after_action_after_shutdown_min", 40.0)))
    except (TypeError, ValueError):
        return 40.0


def report_url(cfg, date: str) -> str:
    base = str(getattr(cfg, "after_action_base_url", "") or "").rstrip("/")
    return f"{base}/report/{date}" if base else f"/report/{date}"


# ------------------------------------------------------------------ helpers

def _dt(s):
    if isinstance(s, datetime):
        return s
    try:
        return datetime.fromisoformat(str(s).rstrip("Z"))
    except (TypeError, ValueError):
        return None


def _local(cfg, dt: datetime) -> datetime:
    try:
        from photonscript.shared.localtime import to_local
        return to_local(cfg, dt)
    except Exception:  # noqa: BLE001
        return dt - timedelta(hours=7)


def _font(size: int):
    from PIL import ImageFont
    for name in ("DejaVuSans.ttf", "arial.ttf", "segoeui.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _subs(cfg, date: str) -> list[dict]:
    try:
        from photonscript.scheduler.runs import _load_subs
        return [s for s in _load_subs(cfg, date)
                if not s.get("test") and "tracking test" not in str(s.get("target", "")).lower()]
    except Exception as e:  # noqa: BLE001
        logger.debug("after-action subs unavailable: %s", e)
        return []


def _kept(s: dict) -> bool:
    return bool(s.get("passed_qa"))


# ------------------------------------------------------------------ timeline

def timeline_png(cfg, date: str, width: int = 1400, tl: dict | None = None,
                 subs: list[dict] | None = None) -> bytes | None:
    """The night's timeline bars as a PNG (None when there is no data)."""
    from PIL import Image, ImageDraw
    if tl is None:
        from photonscript.scheduler.night_timeline import timeline
        tl = timeline(cfg, date)
    if not tl or not tl.get("ok"):
        return None
    subs = _subs(cfg, date) if subs is None else subs
    t0, t1 = _dt(tl.get("t0")), _dt(tl.get("t1"))
    try:
        from photonscript.scheduler.night_timeline import _night_window
        win = _night_window(cfg, date) or {}
        ss, sr = _dt(win.get("sunset")), _dt(win.get("sunrise"))
        if ss and sr:
            t0, t1 = ss - timedelta(minutes=30), sr + timedelta(minutes=30)
    except Exception:  # noqa: BLE001
        pass
    if not t0 or not t1 or t1 <= t0:
        return None
    rows = [(r.get("label") or r.get("id"), r.get("segments") or [])
            for r in tl.get("rows") or []]
    by_rig: dict[str, list] = {}
    for s in subs:
        a, b = _dt(s.get("start_utc")), _dt(s.get("end_utc"))
        if a and b:
            by_rig.setdefault(s.get("rig") or "rc16", []).append(
                {"start": a, "end": b, "state": "kept" if _kept(s) else "rejected"})
    for rig, segs in sorted(by_rig.items()):
        rows.append((f"{'RC16' if rig == 'rc16' else 'Piggy-600'} subs", segs))
    left, top, row_h, gap, right = 150, 46, 26, 8, 20
    height = top + len(rows) * (row_h + gap) + 60
    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)
    f_lab, f_small = _font(15), _font(12)
    span = (t1 - t0).total_seconds()
    x_of = lambda t: left + (width - left - right) * max(0.0, min(1.0, (t - t0).total_seconds() / span))  # noqa: E731
    d.text((left, 12), f"Night of {date} (local time)", fill=FG, font=f_lab)
    h = _local(cfg, t0).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    off = _local(cfg, t0) - t0
    while h - off < t1:
        x = x_of(h - off)
        d.line([(x, top - 6), (x, height - 40)], fill=GRID, width=1)
        d.text((x - 10, height - 34), h.strftime("%H"), fill=MUTED, font=f_small)
        h += timedelta(hours=1)
    for i, (label, segs) in enumerate(rows):
        y = top + i * (row_h + gap)
        d.text((10, y + 4), str(label)[:18], fill=FG, font=f_lab)
        d.rectangle([left, y, width - right, y + row_h], fill=(30, 35, 44))
        for s in segs:
            a, b = _dt(s.get("start")), _dt(s.get("end"))
            if not a or not b or b <= t0 or a >= t1:
                continue
            col = STATE_COLORS.get(str(s.get("state") or "other").lower(), STATE_COLORS["other"])
            xa, xb = x_of(a), x_of(b)
            d.rectangle([xa, y + 2, max(xb, xa + 1), y + row_h - 2], fill=col)
    lx, ly = left, height - 18
    for name in ("exposing", "kept", "rejected", "slewing", "centering", "autofocus",
                 "meridian flip", "parked", "waiting", "unsafe"):
        d.rectangle([lx, ly, lx + 10, ly + 10], fill=STATE_COLORS[name])
        d.text((lx + 14, ly - 2), name, fill=MUTED, font=f_small)
        lx += 24 + int(d.textlength(name, font=f_small)) if hasattr(d, "textlength") else 90
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


# ------------------------------------------------------------------ thumbnails

def pick_thumbs(subs: list[dict], max_kept: int = 4) -> list[dict]:
    """The best kept sub per (rig, target) by HFR (up to max_kept), then one
    typical reject per rig (its most common reason)."""
    best: dict[tuple, dict] = {}
    for s in subs:
        if not _kept(s) or s.get("hfr") is None:
            continue
        k = (s.get("rig"), s.get("target"))
        if k not in best or float(s["hfr"]) < float(best[k]["hfr"]):
            best[k] = s
    kept = sorted(best.values(), key=lambda s: (str(s.get("rig")), str(s.get("target"))))[:max_kept]
    out = [{"kind": "kept", "sub": s, "why": f"best {s.get('filter')} sub, HFR {float(s['hfr']):.1f} px"}
           for s in kept]
    for rig in sorted({s.get("rig") for s in subs}):
        rej = [s for s in subs if s.get("rig") == rig and not _kept(s)]
        if not rej:
            continue
        first = lambda s: (str(s.get("reason") or "").split(";")[0].strip()  # noqa: E731
                           or "rejected")
        top = Counter(first(s).split(" ")[0].lower() for s in rej).most_common(1)[0][0]
        pick = next(s for s in rej if first(s).split(" ")[0].lower() == top)
        out.append({"kind": "rejected", "sub": pick,
                    "why": f"typical reject ({len(rej)} total): {first(pick)[:90]}"})
    return out


def thumb_bytes(cfg, date: str, sub: dict) -> bytes | None:
    try:
        from photonscript.scheduler.runs import thumbnail
        p = thumbnail(cfg, date, sub.get("file"), width=THUMB_W)
        return Path(p).read_bytes() if p else None
    except Exception as e:  # noqa: BLE001
        logger.debug("thumbnail failed for %s: %s", sub.get("file"), e)
        return None


# ------------------------------------------------------------------ links

def progression_links(cfg) -> list[dict]:
    """[{target, label, url}] from every goal's links, de-duplicated by URL."""
    out, seen = [], set()
    try:
        from photonscript.scheduler.app import get_store
        projects = list(get_store().projects.values())
    except Exception:  # noqa: BLE001
        projects = []
    for p in projects:
        name = getattr(getattr(p, "target", None), "name", None) or getattr(p, "name", "")
        for ln in getattr(p, "links", None) or []:
            url = ln.get("url") if isinstance(ln, dict) else getattr(ln, "url", None)
            label = ln.get("label") if isinstance(ln, dict) else getattr(ln, "label", "")
            if url and url not in seen:
                seen.add(url)
                out.append({"target": name, "label": label or "link", "url": url})
    return out


# ------------------------------------------------------------------ build

def build(cfg, date: str) -> dict:
    """Everything the page / push / email need (images as bytes)."""
    from photonscript.scheduler import morning_report as mr
    try:
        card = mr.report_card(cfg, date)
        lines = list(card.get("lines") or [])
    except Exception as e:  # noqa: BLE001
        logger.warning("after-action card failed: %s", e)
        lines = [f"Morning report unavailable: {e}"]
    subs = _subs(cfg, date)
    kept = [s for s in subs if _kept(s)]
    hours = sum(float(s.get("exp_s") or 0) for s in kept) / 3600.0
    thumbs = []
    for t in pick_thumbs(subs):
        b = thumb_bytes(cfg, date, t["sub"])
        if b:
            s = t["sub"]
            thumbs.append({"kind": t["kind"], "why": t["why"], "bytes": b,
                           "caption": f"{'RC16' if s.get('rig') == 'rc16' else 'Piggy-600'} "
                                      f"{s.get('target')} {s.get('filter')} {int(float(s.get('exp_s') or 0))} s"})
    try:
        png = timeline_png(cfg, date, subs=subs)
    except Exception as e:  # noqa: BLE001
        logger.warning("after-action timeline failed: %s", e)
        png = None
    title = f"AARO night {date}: {len(kept)} kept subs, {hours:.1f} h"
    return {"date": date, "title": title, "lines": lines, "kept": len(kept),
            "rejected": len(subs) - len(kept), "hours": round(hours, 2),
            "timeline_png": png, "thumbs": thumbs, "links": progression_links(cfg),
            "url": report_url(cfg, date)}


def render_html(rep: dict, src) -> str:
    """The report as HTML. src(name, bytes, mime) -> the img src to use
    (a data: URI for the page, a cid: for the email)."""
    e = html.escape
    parts = ["<div style=\"font-family:Segoe UI,Arial,sans-serif;background:#151920;"
             "color:#e2e6ec;padding:16px;max-width:1400px\">",
             f"<h2 style=\"margin:0 0 6px\">{e(rep['title'])}</h2>",
             f"<p style=\"color:#969eaa;margin:0 0 14px\">{rep['kept']} kept, "
             f"{rep['rejected']} rejected. Full page: <a style=\"color:#7fa8e6\" "
             f"href=\"{e(rep['url'])}\">{e(rep['url'])}</a></p>"]
    if rep.get("timeline_png"):
        parts.append(f"<img alt=\"Night timeline\" style=\"max-width:100%;border-radius:6px\" "
                     f"src=\"{src('timeline', rep['timeline_png'], 'image/png')}\">")
    if rep.get("thumbs"):
        parts.append("<h3>Subs</h3><div>")
        for i, t in enumerate(rep["thumbs"]):
            col = "#2ea05a" if t["kind"] == "kept" else "#cd463c"
            parts.append(
                "<div style=\"display:inline-block;vertical-align:top;margin:0 10px 12px 0;"
                f"width:{THUMB_W}px\"><img style=\"width:100%;border:2px solid {col};"
                f"border-radius:4px\" src=\"{src(f'thumb{i}', t['bytes'], 'image/png')}\">"
                f"<div style=\"font-size:13px\">{e(t['caption'])}</div>"
                f"<div style=\"font-size:12px;color:#969eaa\">{e(t['why'])}</div></div>")
        parts.append("</div>")
    if rep.get("links"):
        parts.append("<h3>Integration contact sheets</h3><ul>")
        for ln in rep["links"]:
            parts.append(f"<li>{e(str(ln['target']))}: <a style=\"color:#7fa8e6\" "
                         f"href=\"{e(ln['url'])}\">{e(ln['label'])}</a></li>")
        parts.append("</ul>")
    parts.append("<h3>Report card</h3><div style=\"font-size:14px;line-height:1.45\">")
    parts += [f"<p style=\"margin:0 0 8px\">{e(ln)}</p>" for ln in rep["lines"]]
    parts.append("</div></div>")
    return "".join(parts)


def page_html(rep: dict) -> str:
    def data_uri(_name, b, mime):
        return f"data:{mime};base64," + base64.b64encode(b).decode("ascii")
    body = render_html(rep, data_uri)
    return ("<!doctype html><html><head><meta charset=\"utf-8\"><meta name=\"viewport\" "
            "content=\"width=device-width,initial-scale=1\"><title>"
            f"{html.escape(rep['title'])}</title></head><body style=\"margin:0;"
            f"background:#151920\">{body}</body></html>")


# ------------------------------------------------------------------ send

def _smtp_ready(cfg) -> bool:
    return all(str(getattr(cfg, k, "") or "").strip() for k in
               ("after_action_smtp_host", "after_action_smtp_user",
                "after_action_smtp_password", "after_action_email_to"))


def email_message(cfg, rep: dict) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = rep["title"]
    msg["From"] = str(getattr(cfg, "after_action_smtp_user", ""))
    msg["To"] = str(getattr(cfg, "after_action_email_to", ""))
    msg.set_content("\n".join([rep["title"], rep["url"], ""] + rep["lines"]
                              + [""] + [f"{x['target']}: {x['url']}" for x in rep["links"]]))
    imgs: list[tuple[str, bytes, str]] = []

    def cid(name, b, mime):
        imgs.append((name, b, mime))
        return f"cid:{name}"
    msg.add_alternative(render_html(rep, cid), subtype="html")
    html_part = msg.get_payload()[-1]
    for name, b, mime in imgs:
        main, sub = mime.split("/", 1)
        html_part.add_related(b, maintype=main, subtype=sub, cid=f"<{name}>")
    return msg


def send_email(cfg, rep: dict) -> str:
    if not _smtp_ready(cfg):
        return "email skipped: SMTP not set (after_action_smtp_* / email_to)"
    host = str(cfg.after_action_smtp_host)
    port = int(getattr(cfg, "after_action_smtp_port", 587) or 587)
    with smtplib.SMTP(host, port, timeout=30) as s:
        s.starttls()
        s.login(str(cfg.after_action_smtp_user), str(cfg.after_action_smtp_password))
        s.send_message(email_message(cfg, rep))
    return f"email sent to {cfg.after_action_email_to}"


async def send(cfg, date: str, notify=None, smtp=None) -> dict:
    """Build and send per after_action_mode. notify / smtp injectable."""
    md = mode(cfg)
    rep = await asyncio.to_thread(build, cfg, date)
    out = {"date": date, "mode": md, "title": rep["title"], "steps": []}
    if md in ("push", "both"):
        att = None
        if rep.get("timeline_png"):
            p = Path(getattr(cfg, "data_dir", ".")) / "reports" / f"{date}_timeline.png"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(rep["timeline_png"])
            att = str(p)
        msg = "\n".join([f"Report: {rep['url']}"] + rep["lines"][:6])
        if notify is None:
            from photonscript.shared.pushover import notify as _n
            notify = _n
        try:
            await notify(cfg, msg[:1000], title=rep["title"], priority=0,
                         attachment=att, kind="info", cap_exempt=f"after_action:{date}")
            out["steps"].append("push sent")
        except Exception as e:  # noqa: BLE001
            out["steps"].append(f"push failed: {e}")
    if md in ("email", "both"):
        try:
            out["steps"].append(await asyncio.to_thread(smtp or send_email, cfg, rep))
        except Exception as e:  # noqa: BLE001
            out["steps"].append(f"email failed: {e}")
    try:
        d = Path(getattr(cfg, "data_dir", ".")) / "reports"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{date}_report.html").write_text(page_html(rep), encoding="utf-8")
    except OSError as e:
        logger.debug("report copy not written: %s", e)
    return out


# ------------------------------------------------------------------ loop

def _latch_path(cfg) -> Path:
    return Path(getattr(cfg, "data_dir", ".")) / "reports" / "sent.json"


def _sent(cfg) -> dict:
    try:
        return json.loads(_latch_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _mark(cfg, date: str, res: dict) -> None:
    s = _sent(cfg)
    s[date] = {"at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
               "steps": res.get("steps")}
    p = _latch_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(s, indent=1), encoding="utf-8")


def due(cfg, armer, now: datetime) -> str | None:
    """The night to report on, once its dawn shutdown is after_min old."""
    sh = getattr(armer, "shutdown", None) or {}
    at = _dt(sh.get("at"))
    if not at or now < at + timedelta(minutes=after_min(cfg)):
        return None
    if now - at > timedelta(hours=12):
        return None
    from photonscript.shared.phd2_store import night_of
    night = night_of(cfg, at)
    return None if night in _sent(cfg) else night


async def run_loop(get_config, get_armer, tick_s: int = TICK_S) -> None:
    """Background loop (app startup): one report per night after dawn."""
    while True:
        try:
            cfg = get_config()
            if mode(cfg) != "off":
                night = due(cfg, get_armer(), datetime.utcnow())
                if night:
                    res = await send(cfg, night)
                    _mark(cfg, night, res)
                    logger.info("after-action report %s: %s", night, res.get("steps"))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("after-action loop failed: %s", e)
        await asyncio.sleep(tick_s)
