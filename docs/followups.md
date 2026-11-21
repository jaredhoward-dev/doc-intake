# Follow-ups

Known gaps, each with what was seen. Fake data only in any example.

- **Side-by-side OCR boxes.** On a scanned retail installment contract, the five Truth in Lending
  figures sit in a row of boxes; OCR's columns don't line up with the headers, so the value-below
  rule finds nothing. The local model (`--llm`) fills them; values from it carry no page location,
  so the review page shows no crop for them.
- **A signature line with an e-signature stamp.** `["buyer signs", "line"]` matched on a fake
  contract but not on a real one, where the seller's line matched instead (same date). The stamp's
  text between the label and the date is the likely cause; not confirmed.
- **Memory-cap flakes in tests.** Reads run in a forked child under a memory cap; on a loaded
  machine a small file was occasionally reported "too large", which failed a near-duplicate test
  and a bounded-read test about 1 run in 3. Not reproduced in isolation.
- **macOS: every bounded read fails.** `reader.run_bounded` lowers `RLIMIT_AS` in the forked child; macOS
  raises `ValueError`, the child exits, and the read is reported "too large". So on a Mac no PDF or image is
  read at all. Linux is unaffected. The tests work around it (`macos_rlimit` in `tests/conftest.py`); the fix
  is to treat that `ValueError` as "no address-space cap here" (and keep the timeout).
- **macOS: peak memory reads about 1024 times too high.** `ru_maxrss` is bytes on macOS and KB on Linux;
  `run_bounded`'s `peak_rss_mb` and `fingerprints._mb` (`scan --profile-mem`) divide by 1024 either way.
- **The senders map is skipped as a profile only when it is named `vendors.json`.** `fields.NOT_PROFILES`
  is `{"vendors"}`; a host whose map has another name (the tests use `senders.json`) sees it listed as a
  profile. Fix: skip the file `CFG.senders_file` names. `tests/test_review.py::test_senders_json_is_not_a_profile`
  is a strict xfail until then.
- **Fixture dates near "today".** Fields dated more than 60 days ahead are flagged; the golden fixtures use
  2030 dates, so the golden expected files change once 2030 is within 60 days. Regenerate then
  (`GOLDEN_UPDATE=1`), or let a test set the engine's "today".
- **fork() after a thread.** The local-model tests run a fake server thread, then the reader forks;
  Python 3.12+ warns that this can deadlock. Seen only in tests (the CLI has no threads).
