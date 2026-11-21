"""Optional second pass through a LOCAL model (Ollama on 127.0.0.1:11434 only). Best-effort and nondeterministic:
the regex and checksum detectors stay the primary layer, and this pass only adds flags or fills profile fields.

Ollama gives every model a 4096-token context by default and truncates silently past it, so every request sets
num_ctx explicitly and text goes in chunks of at most CHUNK_CHARS characters, which fit with room for the prompt
and the answer.
"""
import json
import re
import urllib.error
import urllib.request

from .config import CFG

OLLAMA_HOST = "127.0.0.1"  # never another host: the text sent is private even after redaction
OLLAMA_PORT = 11434        # tests point this at a local mock server
NUM_CTX = 8192
CHUNK_CHARS = 6000         # about 1,500-2,000 tokens of English, 3,000 of OCR noise; well inside NUM_CTX
TIMEOUT = 300
FLAG_KINDS = ("name", "address", "id-number", "phone", "email", "other")


def not_reachable():
    """The note printed when the local model server is down; the host says how to start it."""
    return f"Ollama is not reachable on 127.0.0.1:11434: {CFG.start_hint}"


class LLMError(Exception):
    pass


def _url(path):
    if OLLAMA_HOST != "127.0.0.1":
        raise LLMError("the model pass only talks to 127.0.0.1")
    return f"http://{OLLAMA_HOST}:{int(OLLAMA_PORT)}{path}"


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # no proxy, ever


def reachable():
    try:
        with _OPENER.open(_url("/api/tags"), timeout=3) as r:
            return r.status == 200
    except (OSError, urllib.error.URLError, ValueError):
        return False


def generate_json(model, system, prompt):
    """One non-streamed /api/generate call with format=json. Returns the parsed object or raises LLMError."""
    body = json.dumps({"model": model, "system": system, "prompt": prompt, "format": "json", "stream": False,
                       "options": {"num_ctx": NUM_CTX, "temperature": 0}}).encode()
    req = urllib.request.Request(_url("/api/generate"), data=body, headers={"Content-Type": "application/json"})
    try:
        with _OPENER.open(req, timeout=TIMEOUT) as r:
            outer = json.loads(r.read().decode("utf-8", "replace"))
    except (OSError, urllib.error.URLError, ValueError) as e:
        raise LLMError(f"request failed: {type(e).__name__}") from None
    try:
        obj = json.loads(outer.get("response", ""))
    except (ValueError, AttributeError):
        raise LLMError("the model did not return JSON") from None
    if not isinstance(obj, dict):
        raise LLMError("the model did not return a JSON object")
    return obj


def chunks(text, size=CHUNK_CHARS):
    """Split on line boundaries into pieces of at most size characters (a longer line is cut)."""
    out, cur = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > size:
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:size])
            line = line[size:]
        if len(cur) + len(line) > size:
            out.append(cur)
            cur = ""
        cur += line
    if cur:
        out.append(cur)
    return out


FLAG_SYSTEM = (
    "You find personal information left in a document after automatic redaction. Bracketed tags such as [SSN] "
    "are already redacted; ignore them. Report only text copied exactly from the input that is: a person's name, "
    "a street address, an ID number with no label, a phone number or an email address. Answer with JSON only: "
    '{"spans": [{"text": "<exact text>", "kind": "name|address|id-number|phone|email"}]}. '
    'If there is nothing, answer {"spans": []}.')


def _kind(k):
    k = re.sub(r"[^a-z-]", "", str(k).lower())
    return k if k in FLAG_KINDS else "other"


def flag_spans(text, model, call=None):
    """Second redaction pass. Returns (text, {kind: n}, status) with status 'ok', 'failed N of M chunks' or
    'skipped (...)'. Never raises: a failure leaves the regex-redacted text as it was."""
    call = call or generate_json
    if not model:
        return text, {}, f"skipped (no model: {CFG.model_hint})"
    if not reachable():
        print(not_reachable())
        return text, {}, "skipped (Ollama not reachable)"
    flags, out, failed = {}, [], 0
    parts = chunks(text)
    for part in parts:
        try:
            obj = call(model, FLAG_SYSTEM, part)
            spans = obj.get("spans")
            if not isinstance(spans, list):
                raise LLMError("no spans list")
        except LLMError:
            failed += 1
            out.append(part)
            continue
        tags = re.findall(r"\[[^\]\n]*\]", part)
        found = []
        for sp in spans:
            if not isinstance(sp, dict) or not isinstance(sp.get("text"), str):
                continue
            t = sp["text"].strip()
            if len(t) < 3 or "[" in t or "]" in t or any(t in tag for tag in tags) or t not in part:
                continue
            found.append((t, _kind(sp.get("kind"))))
        for t, k in sorted(set(found), key=lambda x: -len(x[0])):
            n = part.count(t)
            if n:
                part = part.replace(t, f"[LLM-FLAGGED:{k}]")
                flags[k] = flags.get(k, 0) + n
        out.append(part)
    status = "ok" if not failed else f"failed {failed} of {len(parts)} chunks"
    return "".join(out), flags, f"{status} (model {model}, {len(parts)} chunks)"


TITLE_SYSTEM = (
    "You name a document for a filing system. Answer with JSON only: {\"title\": \"...\"}. The title is 2 to 6 "
    "plain words saying what kind of document it is (for example \"Home warranty contract\" or \"Water heater "
    "manual\"). Never include a person's name, an address, an account or ID number, or any number longer than 4 "
    "digits. Bracketed tags such as [SSN] are redactions; ignore them.")


def suggest_title(text, model, call=None):
    """(title or None, status) from the first chunk of redacted text. The caller checks the title; never raises."""
    call = call or generate_json
    if not model:
        return None, "skipped (no model)"
    part = chunks(text)[:1]
    if not part:
        return None, "skipped (no text)"
    try:
        obj = call(model, TITLE_SYSTEM, part[0])
    except LLMError as e:
        return None, f"failed ({e})"
    t = obj.get("title")
    return (t if isinstance(t, str) else None), "ok"


FILL_SYSTEM = (
    "You fill in missing fields from a document. Answer with JSON only: "
    '{"values": [{"group": "...", "plan": "... or null", "field": "...", "value": "..."}]}. '
    "Use only the groups, plans and fields listed. Copy each value exactly as printed (an amount, percent, date "
    "or short phrase). Leave out anything you cannot find. Never include names, ID numbers or addresses.")


def fill_fields(pages, wanted, model, call=None):
    """Ask the model for missing profile fields. wanted: [{"group", "plan", "field", "type"}]. Returns
    (candidates [(page, group, plan, field, value)], status). The caller validates every value."""
    call = call or generate_json
    if not model:
        return [], f"skipped (no model: {CFG.model_hint})"
    if not wanted:
        return [], "skipped (nothing missing)"
    if not reachable():
        print(not_reachable())
        return [], "skipped (Ollama not reachable)"
    listing = "\n".join(f"- group={w['group']} plan={w['plan'] or 'null'} field={w['field']} type={w['type']}"
                        for w in wanted)
    out, failed, n = [], 0, 0
    for pno, text in pages:
        for part in chunks(text, CHUNK_CHARS - len(listing)):
            n += 1
            try:
                obj = call(model, FILL_SYSTEM, f"Fields:\n{listing}\n\nDocument page {pno}:\n{part}")
                vals = obj.get("values")
                if not isinstance(vals, list):
                    raise LLMError("no values list")
            except LLMError:
                failed += 1
                continue
            for v in vals:
                if isinstance(v, dict) and all(isinstance(v.get(k), str) for k in ("group", "field", "value")):
                    plan = v.get("plan") if isinstance(v.get("plan"), str) and v.get("plan") != "null" else None
                    out.append((pno, v["group"], plan, v["field"], v["value"]))
    status = "ok" if not failed else f"failed {failed} of {n} chunks"
    return out, f"{status} (model {model}, {n} chunks)"
