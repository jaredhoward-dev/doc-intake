"""The engine's configuration: where the records live, and the hooks a host program plugs in.

The engine knows no folder layout, no vendor and no bookkeeping. A host program (a CLI that wraps the engine) calls
configure() once at start-up with:

- root: a function returning the records root (read on every call, so a test or a changed environment is seen);
- inbox: a function returning the inbox folder's name under the root;
- state: the state folder's name under the root (default ".doc-intake");
- profiles_dir and senders_file: the folder of field profiles and the known-senders map (fields.py); the map may
  sit in the profiles folder under any name, and is never listed or loaded as a profile;
- route(path, ftype, redacted_text, title_fn=None, **opts): where a screened document would go, as a dict with
  category, profile, dest, name, note, date_from_file and name_from_model (see scan.screen);
- model: a function returning the local model's name for the optional model pass, or "";
- privacy_terms: a function returning lower-cased terms a model-made title must never contain;
- the review page's hooks (preview.py): summary_extras(values) adds keys to a document's summary;
  preview_checks(task, name, source, values) -> (warnings, notes) for a receipt or invoice; money_card(summary) ->
  (goes-to text, file name, what approving does) for one; money_sections(plan) -> extra HTML; preview_intro(n) ->
  the page's "what approving does" line; preview_css_extra: style rules for the host's own sections;
- vendor_key(name) -> the key pairs.py compares vendors by;
- the wording of a few messages that name the host's own setup (where to set the model, what to do with held
  secret material, the command the user runs).

Every hook has a default that works with no host: an "inbox" folder, no routing, no model, no extra preview text.
Nothing here reads the environment or a file.
"""
import os


def _no_route(path, ftype, redacted, title_fn=None, **opts):
    return {"category": "unknown", "profile": None, "dest": None, "name": os.path.basename(path), "note": "",
            "date_from_file": False, "name_from_model": False}


class Config:
    def __init__(self):
        self.root = None                     # callable -> path; required before any command runs
        self.inbox = lambda: "inbox"         # callable -> folder name under root
        self.state = ".doc-intake"
        self.profiles_dir = None
        self.senders_file = None
        self.route = _no_route
        self.model = lambda: ""
        self.privacy_terms = lambda: []
        self.cli = "doc-intake"              # the command the user types, for the hints the engine prints
        self.root_name = "the records root"  # how errors name the root (a host may name its own variable)
        self.records_name = "the records"   # how messages name what the root holds
        self.assistant = "the assistant"     # who reads approved fields, in messages
        self.root_missing = "the records folder does not exist"
        self.filed_statuses = ("filed",)   # index statuses that mean a copy was filed: a new one is a duplicate
        self.held_note = "secret material: move it to a password manager, then delete"
        self.approve_question = "Approve these fields for the assistant to read?"   # approval.py's dialog
        self.approve_question_extract = "Approve this redacted extract for the assistant to read?"
        self.summary_extras = lambda values: {}
        self.preview_checks = lambda task, name, src, vals: ([], [])
        self.money_card = None
        self.money_sections = lambda plan: ""
        self.preview_intro = lambda n: (f"Approving lets {self.assistant} read the values of these {n} documents; "
                                        "nothing is filed or moved.")
        self.preview_css_extra = ""
        self.vendor_key = None
        self.model_hint = "configure a model or pass --model"   # how to set the local model
        self.start_hint = "start Ollama"     # how to start the local model server


CFG = Config()
KEYS = set(vars(Config()))


def configure(**kw):
    """Set configuration values; unknown names are an error, so a typo can't silently keep a default."""
    bad = set(kw) - KEYS
    if bad:
        raise TypeError(f"unknown engine settings: {', '.join(sorted(bad))}")
    for k, v in kw.items():
        setattr(CFG, k, v)
    if "profiles_dir" in kw or "senders_file" in kw:
        from . import fields
        fields.PROFILE_DIR, fields.VENDORS_FILE = CFG.profiles_dir, CFG.senders_file
    from . import approval
    approval.QUESTION, approval.QUESTION_EXTRACT = CFG.approve_question, CFG.approve_question_extract
    return CFG
