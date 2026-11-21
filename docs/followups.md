# Follow-ups

Known gaps, each with what was seen. Fake data only in any example.

- **Confirm two extraction fixes on real layouts.** Both were built and tested on synthetic text
  only, by design:
  - `box_row` (a row of Truth in Lending boxes under wrapped headers) gives nothing, never a
    neighbour's figure, when a box is blank, a figure sits on a column edge, or two figures share a
    column. Limits seen on paper: a blank line inside the header block cuts it short, and OCR
    leaving a single space between figures merges them into one cell.
  - A signature line's date beside an e-signature stamp: the stamp pushes the date onto the line
    above or below the label; the nearest date within two lines is taken, never past another
    signature line. This fits the symptom seen on a real contract (the seller's date taken), but
    that contract was never re-run.
