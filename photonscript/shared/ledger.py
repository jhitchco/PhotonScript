"""PS-33: the integrator ledger, one record per processing version.

The desktop `photonscript integrate` run writes <run>/ledger.json and posts
it to the scheduler (POST /api/integrations, scheduler/routers/
integrations.py), which keeps one file per version under
<data_dir>/ledgers/<project_id>/. The same schema is read on both machines,
so it lives in shared/.

Shape (schema "photonscript.ledger/0.2"):

    campaign      target name as the desktop knows it (resolved to a goal
                  by the scheduler, catalog aliases included)
    rig           "piggyback" | "rc16"
    run           run folder name (unique per run; the store's key)
    version       1, 2, ... per goal + rig (0 = let the scheduler pick)
    created_at    ISO UTC
    machine       everything the scripts know: acquisition (nights, hours,
                  subs per filter), subs [{file, night, filter, exp_s,
                  used}], calibration, qa, integration, finish, outputs,
                  astrobin {packet, csv}, timing, trigger (PS-31)
    review        the human half: verdict, notes, asks[], decided_by_jeremy.
                  Never written by a script; a re-post with an empty review
                  keeps the stored one.
    reported      the desktop's copy: true once the scheduler took it
    reported_at

Asks (structured so the planner can use them later): more_hours {rig,
filter, hours}, need_calibration {rig, what[]}, short_subs {rig, filter,
exposure_s, count}, reframe {driving_rig, offset_arcmin}, rest {},
fix_blocker {ticket}. Each has an id and a status: open | approved |
declined | applied. Asks never change goals by themselves.

load() accepts the hand-written 0.1 files (Staging/M31_OSC3/ledger.json)
and upgrades them: every top-level block that is not campaign / run /
version / rig / review / publish moves under `machine`.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA = "photonscript.ledger/0.2"
SCHEMA_V01 = "photonscript.ledger/0.1"
ASK_TYPES = ("more_hours", "need_calibration", "short_subs", "reframe", "rest",
             "fix_blocker")
ASK_STATUSES = ("open", "approved", "declined", "applied")
VERDICTS = ("", "keep", "redo_processing", "publish", "needs_more_data")
_TOP_KEEP = {"schema", "campaign", "run", "version", "rig", "review", "publish",
             "created_at", "reported", "reported_at", "machine"}


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Ask(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str = ""
    type: str
    status: str = "open"
    rig: Optional[str] = None
    filter: Optional[str] = None
    hours: Optional[float] = None
    what: Optional[list[str]] = None
    exposure_s: Optional[float] = None
    count: Optional[int] = None
    driving_rig: Optional[str] = None
    offset_arcmin: Optional[float] = None
    ticket: Optional[str] = None
    note: str = ""

    @field_validator("type")
    @classmethod
    def _known_type(cls, v: str) -> str:
        if v not in ASK_TYPES:
            raise ValueError(f"unknown ask type {v!r} (one of {', '.join(ASK_TYPES)})")
        return v

    @field_validator("status")
    @classmethod
    def _known_status(cls, v: str) -> str:
        if v not in ASK_STATUSES:
            raise ValueError(f"unknown ask status {v!r}")
        return v


class Review(BaseModel):
    model_config = ConfigDict(extra="allow")
    verdict: str = ""
    notes: str = ""
    asks: list[Ask] = Field(default_factory=list)
    decided_by_jeremy: bool = False

    def is_empty(self) -> bool:
        return not (self.verdict or self.notes or self.asks or self.decided_by_jeremy)


class Ledger(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)
    schema_: str = Field(default=SCHEMA, alias="schema")
    campaign: str
    rig: str = "piggyback"
    run: str
    version: int = 0
    created_at: str = Field(default_factory=now_iso)
    machine: dict[str, Any] = Field(default_factory=dict)
    review: Review = Field(default_factory=Review)
    publish: dict[str, Any] = Field(default_factory=dict)
    reported: bool = False
    reported_at: str = ""

    @field_validator("campaign", "run")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not str(v or "").strip():
            raise ValueError("must not be blank")
        return str(v).strip()

    @field_validator("rig")
    @classmethod
    def _rig(cls, v: str) -> str:
        v = str(v or "").strip().lower()
        if v not in ("piggyback", "rc16"):
            raise ValueError(f"unknown rig {v!r}")
        return v

    def dump(self) -> dict:
        return self.model_dump(by_alias=True, mode="json")

    # --- convenience reads (used by the summary and the watcher) --------------
    @property
    def hours(self) -> float:
        return float((self.machine.get("acquisition") or {}).get("hours") or 0.0)

    @property
    def sub_files(self) -> set[str]:
        return {Path(str(s.get("file") or "")).name
                for s in (self.machine.get("subs") or []) if s.get("file")}

    def open_asks(self) -> list[Ask]:
        return [a for a in self.review.asks if a.status == "open"]


def _ask_from_v01(a: Any) -> dict:
    if isinstance(a, str):
        return {"type": a}
    d = dict(a)
    if "type" not in d:
        d["type"] = d.pop("kind", None) or d.pop("ask", None) or "fix_blocker"
    return d


def assign_ask_ids(review: Review) -> None:
    for a in review.asks:
        if not a.id:
            a.id = uuid.uuid4().hex[:10]


def upgrade(data: dict) -> dict:
    """A 0.1 ledger dict as 0.2 (a 0.2 dict comes back unchanged)."""
    d = dict(data)
    if d.get("schema", SCHEMA_V01) == SCHEMA and "machine" in d:
        return d
    was_v01 = d.get("schema") == SCHEMA_V01
    machine = dict(d.get("machine") or {})
    if "created" in d and "created_at" not in d:
        d["created_at"] = d.pop("created")
    for k in list(d):
        if k not in _TOP_KEEP:
            machine[k] = d.pop(k)
    rig = d.get("rig")
    if isinstance(rig, dict):
        machine["rig_detail"] = rig
        name = " ".join(str(rig.get(k) or "") for k in ("id", "name", "rig")).lower()
        rig = "rc16" if "rc16" in name or "rc 16" in name else "piggyback"
    review = dict(d.get("review") or {})
    review["asks"] = [_ask_from_v01(a) for a in (review.get("asks") or [])]
    if isinstance(review.get("notes"), list):
        review["notes"] = "\n".join(str(n) for n in review["notes"])
    if isinstance(review.get("verdict"), list):
        review["verdict"] = ", ".join(str(n) for n in review["verdict"])
    ver = d.get("version")
    if isinstance(ver, str):
        ver = int("".join(c for c in ver if c.isdigit()) or 0)
    d.update(schema=SCHEMA, machine=machine, rig=rig or "piggyback", review=review,
             version=ver or 0)
    if was_v01:
        d.setdefault("run", d.get("campaign") or "")
    return d


def parse(data: dict) -> Ledger:
    """Validate a ledger dict (0.1 or 0.2). Raises pydantic.ValidationError."""
    led = Ledger.model_validate(upgrade(data))
    assign_ask_ids(led.review)
    return led


def load(path: Path) -> Ledger:
    return parse(json.loads(Path(path).read_text(encoding="utf-8")))


def save(path: Path, led: Ledger) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(led.dump(), indent=1, ensure_ascii=True), encoding="ascii")
    tmp.replace(path)
    return path


def headline(led: Ledger) -> str:
    """'Integrated 9.4 h on 2026-10-05 (v2), packet ready' (ASCII)."""
    when = (led.created_at or "")[:10]
    packet = (led.machine.get("astrobin") or {}).get("packet")
    ok = (led.machine.get("integration") or {}).get("ok")
    head = f"Integrated {led.hours:.1f} h on {when} (v{led.version})"
    if ok is False:
        head = f"Integration FAILED on {when} (v{led.version})"
    elif ok is None:
        head = f"Staged {led.hours:.1f} h on {when} (v{led.version}, not integrated)"
    return head + (", packet ready" if packet else "")

