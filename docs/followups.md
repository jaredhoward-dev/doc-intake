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
- **Fixture dates near "today".** Fields dated more than 60 days ahead are flagged; the golden fixtures use
  2030 dates, so the golden expected files change once 2030 is within 60 days. Regenerate then
  (`GOLDEN_UPDATE=1`), or let a test set the engine's "today".
- **fork() after a thread.** The local-model tests run a fake server thread, then the reader forks;
  Python 3.12+ warns that this can deadlock. Seen only in tests (the CLI has no threads).
