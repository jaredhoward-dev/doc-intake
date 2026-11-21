# Security

Report a vulnerability privately through GitHub's "Report a vulnerability" (Security tab →
Advisories). Please don't open a public issue for it.

In scope: anything that lets document text or an unapproved value reach output, logs or the
assistant; a detector that misses a value class it claims to catch; a way to approve, or to mark a
value wrong (`wrong`), without the user; a path that writes outside the configured folders. Include synthetic data only.

Each file is read in a forked child under a timeout and, on Linux, a memory cap. macOS refuses the
memory cap, so there a read relies on the timeout, the OCR pixel cap and ImageMagick's limits.
