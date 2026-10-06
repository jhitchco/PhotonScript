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
