"""PS-147: one form for a sub's `file` value (its path inside the night folder).

The subs log, the pointing sidecar (runs/<night>_pointing.jsonl) and the solve
store key a sub by `file`. The live agent and the backfill used to write the
OS separator (a backslash on the scope) while copied logs and tests hold "/",
so a lookup by `file` missed between the two. The canonical form is "/":
writers store it and every reader normalizes what it compares, so a log that
still holds backslash records (written before PS-147) matches too.
"""

from __future__ import annotations


def norm_file(f) -> str:
    """The canonical (forward slash) form of a `file` value; "" for none."""
    return str(f or "").replace("\\", "/")


def norm_record(rec: dict) -> dict:
    """Normalize `rec["file"]` in place (when the record has one); returns it."""
    if isinstance(rec, dict) and rec.get("file"):
        f = rec["file"]
        if isinstance(f, str) and "\\" in f:
            rec["file"] = norm_file(f)
    return rec


def sub_ref(ref, rig: str | None = None) -> tuple[str | None, str | None, str]:
    """(night, rig, file) from any form GET /api/subs hands out for a sub:
    its key "date|rig|file", its file (either separator), its abs_path or a
    bare file name. night / rig are None when the form does not carry them
    (an explicit rig wins over the key's)."""
    s = str(ref or "").strip()
    night = None
    if s.count("|") >= 2:
        night, krig, s = s.split("|", 2)
        rig = rig or krig
    return (night or None), (rig or None), norm_file(s).strip()


def find_sub(subs: list[dict], ref, rig: str | None = None) -> dict | None:
    """The record in `subs` (one night's subs log) that `ref` names, in any
    sub_ref form: the exact file first, then a path that ends in the file
    (abs_path, or a longer relative path), then a bare file name; within
    `rig` when given. 2026-10-14: the scorecard looked up only the exact
    file, so the key and abs_path /api/subs returns answered "sub not
    found". None when nothing (or only an ambiguous bare name) matches."""
    _night, rig, f = sub_ref(ref, rig)
    if not f:
        return None
    cands = [s for s in subs if s.get("file")
             and (not rig or (s.get("rig") or "rc16") == rig)]
    exact = [s for s in cands if norm_file(s["file"]) == f]
    if exact:
        return exact[-1]
    low = f.lower()
    tail = [s for s in cands
            if low.endswith("/" + norm_file(s["file"]).lower())
            or norm_file(s.get("abs_path")).lower() == low]
    if tail:
        return tail[-1]
    if "/" not in f:
        named = [s for s in cands if norm_file(s["file"]).rsplit("/", 1)[-1] == f]
        if len({(s.get("rig") or "rc16", norm_file(s["file"])) for s in named}) == 1:
            return named[-1]
    return None
