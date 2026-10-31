"""Morning summary of the desktop processing night (autointegrate).

One line per goal: what was stacked (integrate runs), finished (Finish
folders: `photonscript finish`, the finish-only retry, mono color) and
blended (Blend folders) since `since`, with links (file:/// URIs) to the
final JPGs. autointegrate writes it to <staging>/Summaries/<date>.txt and
Pushovers the short form (JPG names, the file's path) once a day after
autointegrate_summary_hour; `photonscript autointegrate-summary` prints it.
Read-only: it only reads ledgers and lists JPGs.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

PUSH_MAX = 1000


def _when(iso: str) -> datetime | None:
    try:
        return datetime.strptime(str(iso)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _load(f: Path):
    from photonscript.shared import ledger as L
    try:
        return L.load(f)
    except Exception:  # noqa: BLE001 - half-written or hand-edited
        return None


def _group(goals: list[dict], campaign: str, named: bool = False) -> dict:
    """The goal group of a campaign (aliases match: M31 = Andromeda Galaxy);
    named = this is an integrate ledger, whose campaign is the goal's own
    name, so the group takes it."""
    from photonscript.integration.ledger import same_campaign
    for g in goals:
        if same_campaign(g["goal"], campaign):
            if named and not g.get("named"):
                g["goal"], g["named"] = campaign, True
            return g
    g = {"goal": campaign, "named": named, "events": [], "jpgs": []}
    goals.append(g)
    return g


def _add_jpgs(g: dict, jpgs) -> None:
    have = {str(p).lower() for p in g["jpgs"]}
    for p in jpgs:
        if str(p).lower() not in have:
            have.add(str(p).lower())
            g["jpgs"].append(Path(p))


def collect(roots, since: datetime) -> list[dict]:
    """[{goal, events: [text], jpgs: [Path]}] for ledgers created at or
    after `since` (UTC) under every root: <root>/*/ledger.json (integrate),
    <root>/Finish/*/ledger.json (finish), <root>/Blend/*/ledger.json (blend)."""
    from photonscript.integration import autointegrate as ai
    from photonscript.integration import finish_retry as fr
    from photonscript.integration.blend import BLEND_DIR, FINISH_DIR
    goals: list[dict] = []
    seen: set[str] = set()
    items = []
    for root in roots:
        root = Path(root)
        for pat in ("*/ledger.json", f"{FINISH_DIR}/*/ledger.json", f"{BLEND_DIR}/*/ledger.json"):
            for f in sorted(root.glob(pat)):
                if f.parent.name.lower() in seen:
                    continue
                led = _load(f)
                t = _when(getattr(led, "created_at", "")) if led else None
                if led is None or t is None or t < since:
                    continue
                seen.add(f.parent.name.lower())
                items.append((t, f, led))
    for _t, f, led in sorted(items, key=lambda x: x[0]):
        run_dir = f.parent
        m = led.machine or {}
        g = _group(goals, led.campaign, named=(led.kind == "integration"))
        if led.kind == "integration":
            integ = (m.get("integration") or {}).get("ok")
            fin = (m.get("finish") or {}).get("ok")
            jpgs = fr.run_jpgs(run_dir, roots)
            retry = m.get(fr.RETRY_KEY) or {}
            state = ("integration FAILED" if integ is False else "not integrated" if integ is None
                     else "finish ok" if fin and jpgs
                     else f"finish FAILED, finished by retry {len(retry.get('attempts') or [])}"
                     if jpgs and retry.get("ok")
                     else "finish FAILED, re-finished" if jpgs
                     else "finish FAILED, no final image" if fin is False else "no final image")
            if retry.get("attempts") and not jpgs:
                state += f" ({len(retry['attempts'])} finish retr{'y' if len(retry['attempts']) == 1 else 'ies'})"
            g["events"].append(f"stacked {led.rig} v{led.version} ({led.hours:.1f} h, {state})")
            _add_jpgs(g, jpgs)
        elif led.kind == "finish":
            inp = m.get("input") or {}
            src = Path(str(inp.get("run") or inp.get("mono_run") or "")).name
            ok = (m.get("finish") or {}).get("ok")
            if ok is None:
                ok = (m.get("color") or {}).get("ok")
            jpgs = sorted((run_dir / "out" / "final").glob("*.jpg"))
            what = "colored" if m.get("color") else "finished"
            g["events"].append(f"{what} {led.rig}" + (f" v{inp.get('run_version')}" if inp.get("run_version")
                                                      else "")
                               + (f" from {src}" if src else "") + (" FAILED" if ok is False else "")
                               + f" ({run_dir.name})")
            _add_jpgs(g, [p for p in jpgs if not p.name.lower().endswith("review.jpg")])
        elif led.kind == "blend":
            ok = (m.get("blend") or {}).get("ok")
            g["events"].append(f"blended{'' if ok else ' FAILED'} ({run_dir.name})")
            _add_jpgs(g, ai.finals(run_dir, blend=True))
    return goals


def _uri(p: Path) -> str:
    try:
        return Path(p).resolve().as_uri()
    except (OSError, ValueError):
        return str(p)


def render(goals: list[dict], day: str) -> str:
    """The full summary: one line per goal with file:/// links."""
    head = f"PhotonScript processing night of {day}"
    if not goals:
        return head + ": nothing stacked, finished or blended."
    lines = [head + ":"]
    for g in goals:
        lines.append(f"{g['goal']}: " + "; ".join(g["events"])
                     + (". JPGs: " + " ".join(_uri(p) for p in g["jpgs"]) if g["jpgs"] else ". No JPG."))
    return "\n".join(lines)


def short(goals: list[dict], day: str, path: Path | None = None) -> str:
    """The Pushover form: one line per goal, JPG names only, at most
    PUSH_MAX characters, the summary file's path at the end."""
    lines = [f"Night {day}:"]
    for g in goals:
        names = ", ".join(Path(p).name for p in g["jpgs"][:4]) + (" ..." if len(g["jpgs"]) > 4 else "")
        lines.append(f"{g['goal']}: " + "; ".join(g["events"]) + (f" [{names}]" if names else ""))
    tail = f"\nLinks: {path}" if path else ""
    text = "\n".join(lines)
    if len(text) + len(tail) > PUSH_MAX:
        text = text[: PUSH_MAX - len(tail) - 3] + "..."
    return text + tail
