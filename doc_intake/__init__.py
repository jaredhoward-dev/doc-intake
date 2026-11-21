"""doc-intake engine: screen, fingerprint and read documents for an assistant without it seeing their contents.

The generic half of the document intake. It holds no folder layout, no vendor list and no bookkeeping: a host
program configures it (config.configure) with its records root, inbox, profiles and routing hook, and keeps its
own CLI. See docs/engine.md. Stdlib only, plus optional system tools (pdftotext, pdftoppm, tesseract,
ImageMagick 7, zxing-cpp); it imports nothing outside this package.
"""
from .config import CFG, configure  # noqa: F401
