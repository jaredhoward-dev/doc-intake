"""Text fingerprints for near-duplicate documents: a 64-bit simhash and a bottom-k MinHash sketch.

Only the fingerprints are kept (hashes of words and word shingles); the text itself is never stored, printed or
logged. Stdlib only.

Normalization: lowercase, words are runs of letters and digits (digits kept, so two monthly statements with the
same boilerplate but different amounts and dates stay apart), whitespace and punctuation dropped.

Rule (see is_near): a pair is a possible duplicate when the estimated Jaccard similarity of their word 3-shingles is
at least JACCARD_MIN (0.9), or when it is at least JACCARD_WITH_SIMHASH (0.75) and the simhashes differ in at most
SIMHASH_MAX_BITS (3) of 64 bits.
- 0.9 on 3-shingles: one changed word removes up to 3 shingles, so a 300-word page keeps J >= 0.9 with about 5
  changed words (a re-download with a new footer or print date) but drops well below it for a different statement
  of the same kind, where every amount and date line differs.
- OCR noise in a rescan changes scattered words, which costs shingles fast; the simhash (over words, weighted by
  count) barely moves under that noise, so a close simhash plus J >= 0.75 also flags.
- MINHASH_K = 64 values: the bottom-k estimate's standard error at J = 0.9 is about sqrt(0.9 * 0.1 / 64) = 0.04.
Documents with fewer than MIN_WORDS words get no fingerprint: too little text to compare.
"""
import hashlib
import heapq
import re

MINHASH_K = 64
SHINGLE = 3
MIN_WORDS = 20
JACCARD_MIN = 0.9
JACCARD_WITH_SIMHASH = 0.75
SIMHASH_MAX_BITS = 3
_MASK = (1 << 64) - 1


def words(text):
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def _h64(s):
    return int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=8, person=b"doc-intake-fp").digest(), "big")


def simhash(ws):
    counts = {}
    for w in ws:
        counts[w] = counts.get(w, 0) + 1
    v = [0] * 64
    for w, n in counts.items():
        h = _h64("w:" + w)
        for i in range(64):
            v[i] += n if (h >> i) & 1 else -n
    return sum(1 << i for i in range(64) if v[i] > 0)


def minhash(ws, k=MINHASH_K):
    """The k smallest 64-bit hashes of the word shingles, sorted (a bottom-k sketch)."""
    n = SHINGLE if len(ws) >= SHINGLE else len(ws)
    shingles = {" ".join(ws[i:i + n]) for i in range(len(ws) - n + 1)}
    return sorted(_h64("s:" + s) for s in shingles)[:k]


class Fingerprinter:
    """Builds the same fingerprint as fingerprint() from text fed a piece at a time (pages, or chunks cut at
    whitespace), so a long document is never held as one string or one word list. Memory: the word counts (one
    entry per distinct word), the last SHINGLE words and the MINHASH_K smallest shingle hashes. Holds no text
    beyond those words."""

    _WORD = re.compile(r"[a-z0-9]+")

    def __init__(self, k=MINHASH_K):
        self.k = k
        self.counts = {}
        self.n = 0
        self.tail = []
        self._heap = []      # negated hashes: a max-heap of the k smallest
        self._held = set()

    def feed(self, text):
        for m in self._WORD.finditer((text or "").lower()):
            self._word(m.group())

    def _word(self, w):
        self.counts[w] = self.counts.get(w, 0) + 1
        self.n += 1
        self.tail.append(w)
        if len(self.tail) > SHINGLE:
            del self.tail[0]
        if len(self.tail) == SHINGLE:
            self._shingle(" ".join(self.tail))

    def _shingle(self, s):
        h = _h64("s:" + s)
        if h in self._held:
            return
        if len(self._heap) < self.k:
            heapq.heappush(self._heap, -h)
            self._held.add(h)
        elif h < -self._heap[0]:
            self._held.discard(-heapq.heapreplace(self._heap, -h))
            self._held.add(h)

    def result(self):
        """{"simhash": 16 hex digits, "minhash": [ints]} or None when fewer than MIN_WORDS words were fed."""
        if self.n < MIN_WORDS:
            return None
        v = [0] * 64
        for w, c in self.counts.items():
            h = _h64("w:" + w)
            for i in range(64):
                v[i] += c if (h >> i) & 1 else -c
        sim = sum(1 << i for i in range(64) if v[i] > 0)
        return {"simhash": f"{sim:016x}", "minhash": sorted(-x for x in self._heap)}


def fingerprint(text):
    """{"simhash": 16 hex digits, "minhash": [ints]} or None when the text is too short. Holds no text."""
    f = Fingerprinter()
    f.feed(text)
    return f.result()


def jaccard(a, b, k=MINHASH_K):
    """Estimated Jaccard similarity of two bottom-k sketches."""
    sa, sb = set(a), set(b)
    union = sorted(sa | sb)[:k]
    if not union:
        return 0.0
    return sum(1 for x in union if x in sa and x in sb) / len(union)


def hamming(a, b):
    return bin((int(a, 16) ^ int(b, 16)) & _MASK).count("1")


def similarity(fa, fb):
    """(jaccard, simhash distance) or None when either has no fingerprint."""
    if not fa or not fb or not fa.get("minhash") or not fb.get("minhash"):
        return None
    return jaccard(fa["minhash"], fb["minhash"]), hamming(fa["simhash"], fb["simhash"])


def is_near(fa, fb):
    """The estimated Jaccard similarity when the pair is a possible duplicate, else None."""
    s = similarity(fa, fb)
    if s is None:
        return None
    j, d = s
    if j >= JACCARD_MIN or (j >= JACCARD_WITH_SIMHASH and d <= SIMHASH_MAX_BITS):
        return j
    return None


def best_match(fp, candidates):
    """(path, jaccard) of the most similar candidate that is a possible duplicate, or None.
    candidates: iterable of (path, fingerprint)."""
    best = None
    for path, other in candidates:
        j = is_near(fp, other)
        if j is not None and (best is None or j > best[1]):
            best = (path, j)
    return best
