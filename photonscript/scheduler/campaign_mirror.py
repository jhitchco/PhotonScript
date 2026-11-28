"""PS-32: campaign mirror, a one-way copy of the campaign goals into Notion.

PhotonScript stays the source of truth; Notion only gets a read-only mirror
(one row per goal, keyed by the goal id) for the Sunday planning session.
Goals are edited on the dashboard, never in Notion.

Two ways to write the mirror, one row shape (mirror_rows):

  * A (the default, the groomed Recommendation): the Claude morning debrief
    reads GET /api/campaign/mirror (or `photonscript campaign-mirror --json`)
    and upserts the rows with its own Notion connection. No secret on the
    scope PC.
  * B (optional, OFF by default): `photonscript campaign-mirror --push`
    upserts through the Notion API itself. It needs all of
    PS_CAMPAIGN_MIRROR_ENABLED=true, PS_NOTION_TOKEN (an internal
    integration token Jeremy creates and puts in .env by hand) and
    PS_NOTION_CAMPAIGNS_DB (the database id). Without --push nothing is
    sent (dry run).

    mirror_rows(campaign)               -> one flat row per goal
    attach_versions(config, goals)      latest_version / astrobin_url on goals
    notion_properties(row)              the Notion API property payload
    push(config, rows, dry_run=True)    upsert by Goal ID (B only)

Nothing here creates the Notion database; its schema is DB_SCHEMA.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

STATUS_LABELS = {"active": "Active", "parked": "Parked",
                 "lights_done_needs_calibration": "Needs calibration",
                 "complete": "Complete"}

# Notion property name -> type. The database is made by hand to match.
DB_SCHEMA = {
    "Target": "title",
    "Goal ID": "rich_text",
    "Status": "select",       # Active | Parked | Needs calibration | Complete
    "Priority": "number",
    "Rig": "select",          # the driving rig: rc16 | piggyback
    "Rigs": "rich_text",      # per rig: done / goal h and filters
    "Hours done": "number",
    "Hours goal": "number",
    "Percent": "number",
    "BB remaining h": "number",
    "NB remaining h": "number",
    "OSC remaining h": "number",
    "Calibration": "select",  # OK | Missing | Unknown
    "Calibration missing": "rich_text",
    "ETA": "date",
    "Returns": "date",        # first night back, parked goals only
    "Visible h (14 n)": "number",
    "Latest version": "rich_text",
    "AstroBin": "url",
    "Synced": "date",
}


def _calibration_label(cal: dict | None) -> str:
    ok = (cal or {}).get("ok")
    return "OK" if ok is True else "Missing" if ok is False else "Unknown"


def _rigs_text(by_rig: dict | None) -> str:
    parts = []
    for rig, r in sorted((by_rig or {}).items()):
        filt = ",".join(dict.fromkeys(r.get("filters") or []))
        parts.append(f"{rig}: {r.get('done_h', 0):g}/{r.get('goal_h', 0):g} h"
                     + (f" ({filt})" if filt else ""))
    return "; ".join(parts)


def mirror_rows(campaign: dict, synced: str = "") -> list[dict]:
    """One flat row per goal of a GET /api/campaign result, keyed by the
    DB_SCHEMA property names (plus "id"). Pure: no I/O."""
    rows = []
    for g in campaign.get("goals") or []:
        goal_h = float(g.get("goal_hours") or 0.0)
        done_h = float(g.get("hours_done") or 0.0)
        cal = g.get("calibration") or {}
        lv = g.get("latest_version") or {}
        rows.append({
            "id": str(g.get("id", "")),
            "Target": g.get("name", ""),
            "Goal ID": str(g.get("id", "")),
            "Status": STATUS_LABELS.get(g.get("status", ""), g.get("status", "")),
            "Priority": g.get("priority"),
            "Rig": g.get("driving_rig") or "",
            "Rigs": _rigs_text(g.get("by_rig")),
            "Hours done": done_h,
            "Hours goal": goal_h,
            "Percent": round(done_h / goal_h * 100) if goal_h else 0,
            "BB remaining h": g.get("bb_remaining_h"),
            "NB remaining h": g.get("nb_remaining_h"),
            "OSC remaining h": g.get("osc_remaining_h"),
            "Calibration": _calibration_label(cal),
            "Calibration missing": "; ".join(cal.get("missing") or []),
            "ETA": g.get("eta") or None,
            "Returns": g.get("returns") or None,
            "Visible h (14 n)": g.get("visible_h_14n"),
            "Latest version": (f"v{lv['version']} {lv.get('rig', '')} "
                               f"{lv.get('date', '')}".strip()
                               if lv.get("version") else ""),
            "AstroBin": g.get("astrobin_url") or None,
            "Synced": synced or None,
        })
    return rows


def attach_versions(config, goals: list[dict]) -> None:
    """goal["latest_version"] = the newest integration ledger of the goal
    (any rig) as {version, rig, date}, goal["astrobin_url"] = the newest
    AstroBin page on any of its ledgers. Missing ledgers leave None / ""."""
    from photonscript.scheduler import integrations as integ
    for g in goals:
        lv, url = None, ""
        try:
            leds = sorted(integ.ledgers_for(config, str(g.get("id", ""))),
                          key=lambda x: x.created_at or "", reverse=True)
            if leds:
                x = leds[0]
                lv = {"version": x.version, "rig": x.rig,
                      "date": (x.created_at or "")[:10]}
            url = next((u for u in (integ.astrobin_url(x) for x in leds) if u), "")
        except Exception as e:  # noqa: BLE001 - a badge; never break the plan
            logger.debug("campaign mirror: ledgers of %s: %s", g.get("id"), e)
        g["latest_version"] = lv
        g["astrobin_url"] = url


# --- B: direct push through the Notion API (off by default) ---------------

def _text(s) -> list:
    return [{"type": "text", "text": {"content": str(s)[:2000]}}] if s else []


def notion_properties(row: dict) -> dict:
    """A mirror row as Notion API page properties (DB_SCHEMA types)."""
    out = {}
    for name, kind in DB_SCHEMA.items():
        v = row.get(name)
        if kind == "title":
            out[name] = {"title": _text(v)}
        elif kind == "rich_text":
            out[name] = {"rich_text": _text(v)}
        elif kind == "number":
            out[name] = {"number": None if v is None else float(v)}
        elif kind == "select":
            out[name] = {"select": {"name": str(v)} if v else None}
        elif kind == "date":
            out[name] = {"date": {"start": str(v)} if v else None}
        elif kind == "url":
            out[name] = {"url": v or None}
    return out


def push_settings(config) -> dict:
    """Whether a real push may run, and why not. Never returns the token."""
    enabled = bool(getattr(config, "campaign_mirror_enabled", False))
    has_token = bool(getattr(config, "notion_token", ""))
    db = str(getattr(config, "notion_campaigns_db", "") or "")
    why = ("PS_CAMPAIGN_MIRROR_ENABLED is false" if not enabled
           else "PS_NOTION_TOKEN is not set" if not has_token
           else "PS_NOTION_CAMPAIGNS_DB is not set" if not db else "")
    return {"enabled": enabled, "token_set": has_token, "database": db,
            "ready": not why, "why_not": why}


def push(config, rows: list[dict], *, dry_run: bool = True,
         client=None) -> dict:
    """Upsert rows into the Notion database by Goal ID. dry_run (the
    default) or a push that is not ready sends nothing and returns the plan.
    One direction only: Notion rows are never read back into PhotonScript
    beyond finding the page id of each goal."""
    st = push_settings(config)
    res = {"dry_run": dry_run or not st["ready"], "settings": st,
           "rows": len(rows), "created": [], "updated": [], "errors": []}
    if res["dry_run"]:
        res["would_upsert"] = [r["Goal ID"] for r in rows]
        return res
    import httpx
    headers = {"Authorization": f"Bearer {config.notion_token}",
               "Notion-Version": NOTION_VERSION,
               "Content-Type": "application/json"}
    own = client is None
    client = client or httpx.Client(timeout=30)
    try:
        for r in rows:
            gid = r["Goal ID"]
            try:
                q = client.post(f"{NOTION_API}/databases/{st['database']}/query",
                                headers=headers, json={"filter": {
                                    "property": "Goal ID",
                                    "rich_text": {"equals": gid}}})
                q.raise_for_status()
                hits = q.json().get("results") or []
                props = notion_properties(r)
                if hits:
                    u = client.patch(f"{NOTION_API}/pages/{hits[0]['id']}",
                                     headers=headers, json={"properties": props})
                    u.raise_for_status()
                    res["updated"].append(gid)
                else:
                    c = client.post(f"{NOTION_API}/pages", headers=headers, json={
                        "parent": {"database_id": st["database"]},
                        "properties": props})
                    c.raise_for_status()
                    res["created"].append(gid)
            except Exception as e:  # noqa: BLE001 - report per goal, keep going
                res["errors"].append(f"{gid}: {e}")
    finally:
        if own:
            client.close()
    return res
