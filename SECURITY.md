# Security

Report a vulnerability privately through GitHub's private vulnerability reporting:
<https://github.com/jaredhoward-dev/doc-intake/security/advisories/new> (Security tab → Advisories →
"Report a vulnerability"). Please don't open a public issue for it.

What to expect: an acknowledgement within 7 days, and a fix or a plan within 30 days of the report.
Disclosure is coordinated with you: the advisory and the fix are published together, and you are
credited unless you'd rather not be.

In scope: anything that lets document text or an unapproved value reach output, logs or the
assistant; a detector that misses a value class it claims to catch; a way to approve without the
user's click; a path that writes outside the configured folders. Include synthetic data only.

Each file is read in a forked child under a timeout and, on Linux, a memory cap. macOS refuses the
memory cap, so there a read relies on the timeout, the OCR pixel cap and ImageMagick's limits.
