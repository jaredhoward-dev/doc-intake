"""The detectors stay near-linear on huge pages, and the fast paths find exactly what the plain versions found.

The timing bounds are loose on purpose: the old code took well over the bound at these sizes (a regex retrying an
e-mail's local part from every start, and every candidate checked against every span already taken), the new code
a small fraction of it, so a slow CI runner still passes and a quadratic regression still fails.
"""
import random
import time

from doc_intake import detect

BOUND_S = 3.0


def _timed(fn, *args):
    t0 = time.perf_counter()
    out = fn(*args)
    return out, time.perf_counter() - t0


def test_email_on_a_long_run_without_an_at_sign_is_fast():
    # 40k dash-joined digit groups: the old regex made find_spans take about 9 s here (about a minute at 100k)
    page = "Ref " + "-".join("1234" for _ in range(40000)) + " end"
    spans, took = _timed(detect.find_spans, page)
    assert "email" not in detect.counts(spans)
    assert took < BOUND_S, f"find_spans took {took:.1f}s"
    glued = page[:-4] + "@example.com"  # the same run as one huge local part: still one e-mail, found once
    spans, took = _timed(detect._email_spans, glued)
    assert spans == [m.span() for m in detect._EMAIL.finditer(glued)] and len(spans) == 1
    assert took < BOUND_S


def test_many_hits_on_one_page_are_fast():
    # 40k e-mail lines: the old overlap check made find_spans take about 9 s here
    n = 40000
    page = "\n".join(f"user{i}@example.com" for i in range(n))
    spans, took = _timed(detect.find_spans, page)
    assert detect.counts(spans) == {"email": n}
    assert took < BOUND_S, f"find_spans took {took:.1f}s"
    (red, c), took = _timed(detect.redact, page)
    assert c == {"email": n} and red == "\n".join(["[EMAIL]"] * n)
    assert took < 2 * BOUND_S, f"redact took {took:.1f}s"


def test_email_scan_matches_the_regex():
    texts = ["someone@example.com", "Mail someone@example.com, other.one+tag@mail.example.org.",
             "a.b@c.de.fg", "..x@y.co", "-@a.bc", "_x@y.zz", "é1@x.co", "x@y", "x@@y.co", "x@y.co@z.org",
             "Total $1,234.56 on 2030-01-05", "a@b.com.x@y.org", "a@b.co-c@d.org", "1234-5678@example.com", ""]
    rng = random.Random(20301121)
    alpha = "ab1Z9._%+-@ é\n:x"
    texts += ["".join(rng.choice(alpha) for _ in range(rng.randint(1, 40))) for _ in range(5000)]
    for t in texts:
        assert detect._email_spans(t) == [m.span() for m in detect._EMAIL.finditer(t)], repr(t)


def test_overlap_index_matches_brute_force():
    rng = random.Random(4111)
    for _ in range(300):
        index, held = detect._Disjoint(), []
        for _ in range(rng.randint(0, 40)):
            s = rng.randint(0, 200)
            e = s + rng.choice([0, 1, 2, 5, 9, 20])
            brute = sorted((ts, te) for ts, te in held if s < te and ts < e)
            assert index.overlapping(s, e) == brute, (held, s, e)
            if not brute:
                index.add(s, e)
                held.append((s, e))
            if rng.random() < 0.2:
                index.settle()


def test_ordinary_text_is_unchanged():
    # pinned on the code before the speed-up: the same spans, kinds and tags
    text = ("Card 4111 1111 1111 1111, mail someone@example.com, call 212-555-0142\n"
            "Account 12345678901 routing 111111118 Total $1,234.56 on 2030-01-05")
    assert [(s, e, k, tag) for s, e, k, tag in detect.find_spans(text)] == [
        (5, 24, "card", "[CARD-last4:1111]"), (31, 50, "email", "[EMAIL]"), (57, 69, "phone", "[PHONE]"),
        (78, 89, "account", "[ACCOUNT-last4:8901]"), (98, 107, "routing", "[ROUTING]")]
