"""Task files in the state folder: find one, load it, and count what it holds. Shared by tasks and preview."""
import json
import os
import re

from . import fields as fx
from .config import CFG
from .places import DocError, inside, root, state_dir


def typed_records(prof, values):
    types = fx.allowed_fields(prof)
    return [{**v, "type": types.get((v["group"], v["field"]), "")} for v in values]


def task_summary(t, prof=None):
    """Counts only: documents, fields, missing (required), flagged values and documents, complete documents."""
    try:
        prof = prof or fx.load_profile(t["task"])
    except fx.ProfileError:
        prof = {"fields": [], "groups": {}}
    required = {f["name"] for f in prof.get("fields", []) if f.get("required")}
    docs_, flagged_docs, flagged, complete, req_missing = 0, 0, 0, 0, 0
    for src in t.get("sources", []):
        docs_ += 1
        recs = typed_records(prof, [v for v in t["values"] if v.get("file") == src["file"]])
        fl = fx.document_flags(recs)
        flagged += len(fl)
        flagged_docs += bool(fl)
        have = {r["field"] for r in recs}
        miss = required - have
        req_missing += len(miss)
        complete += not miss
    return {"documents": docs_, "fields": len(t.get("values", [])), "missing": len(t.get("missing", [])),
            "missing_required": req_missing, "flagged": flagged, "flagged_documents": flagged_docs,
            "complete": complete}


def resolve_output(name):
    """A task JSON or an extract in the state folder."""
    cands = [name] if os.path.isabs(name) else [os.path.join(state_dir("tasks"), name),
                                                os.path.join(state_dir("extracts"), name),
                                                os.path.join(root(), name), name]
    for p in cands:
        if os.path.isfile(p) and inside(p, os.path.join(root(), CFG.state)) and (
                inside(p, state_dir("tasks")) or inside(p, state_dir("extracts"))):
            kind = "task" if p.endswith(".json") else "extract"
            return os.path.realpath(p), kind
    raise DocError(f"no task file or extract named {name}")


def _load_task(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _amount(v):
    return re.sub(r"[$,\s]", "", v or "")
