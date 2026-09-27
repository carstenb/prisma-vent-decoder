# Security policy

This project reads one person's medical device recordings on their own machine.
That shapes what counts as a security problem here, and it is not the usual
list.

## A wrong number is a security bug

**The most serious defect this project can have is a decode that silently
produces plausible but wrong values.** Not a crash — a crash is loud, and loud
is safe. The dangerous failure is a shifted time base, a wrong scale factor or a
misaligned channel that yields a smooth, believable waveform that is not what
the device recorded.

Someone may take such a number to a clinical appointment. That is why it belongs
here rather than in the ordinary issue tracker, and why we would rather hear a
false alarm than not hear a real one.

Report privately if you find:

- values that are wrong but look right — a channel that decodes to a plausible
  range yet disagrees with the device display or the manufacturer's report;
- a time base that is off by a constant — an hour, a day, twelve hours — since
  a night attributed to the wrong date still reads as an ordinary night;
- a session, event or alarm attributed to the wrong session or the wrong
  program;
- any case where `validate` reports `passed` on data that is in fact wrong,
  which means a check is not checking what it claims to.

**Do not attach recordings to the report.** See below.

## Ordinary security problems

Also report privately:

- anything that writes to, moves or deletes a source recording. This package
  opens device files read-only, and a path that breaks that is a data-loss bug;
- a way to make the decoder read or write outside the directory it was pointed
  at — an archive member escaping via `..`, a symlink followed during a card
  copy;
- resource exhaustion from a malformed file: an unbounded allocation, a
  decompression bomb, a loop that does not terminate;
- anything that transmits data off the machine. This package has no network code
  and no runtime dependencies, and that is a property worth defending;
- credentials or personal data appearing in output, logs or error messages that
  a user would reasonably paste into a public issue.

## What is not a vulnerability here

- A file this decoder refuses to read. Refusing is the intended behaviour when
  a file is not what it claims to be.
- The absence of clinical interpretation. Events are unnamed and no index is
  computed **on purpose**; see the README.
- The privacy guard not catching something. It is [documented as an additional
  barrier, not a scanner](scripts/pre-commit) — though a way to make it *miss a
  real device recording it should have caught* is worth reporting, since people
  rely on it.

## How to report

Use **GitHub's private vulnerability reporting** on this repository: the
*Security* tab, then *Report a vulnerability*. It is private to the maintainer
and does not create a public issue.

If that tab is not there, the feature is not enabled and this document is
wrong rather than you being in the wrong place — please say so in a public
issue with no details, as below.

If that is unavailable to you, open a public issue containing **only** the
sentence "I would like to report a decoding defect privately" and no details,
and you will be contacted.

Expect an acknowledgement within a week. This is a personal project maintained
in spare time, so please read that as a genuine estimate rather than a service
commitment.

## Never send recordings

**Do not attach `.zip`, `.wmedf`, `.tc`, `.proto` or device XML files to any
report, public or private.** They contain your health data and your device
serial number, and a private advisory is still a copy of your medical record
sitting on someone else's computer.

Nearly every decoding defect can be diagnosed without them. What helps:

- the package version (`prisma-vent --version`) and your Python version;
- the device model and, if you have it, the firmware version;
- **a synthetic reproduction** built with the fixture builder in
  `tests/synthetic.py`, which writes byte-exact files from invented data. This
  is the one thing that carries no health data at all, and it is worth more
  than everything else on this list put together;
- what you expected and what you got, described rather than quantified — "the
  airway pressure channel decodes about ten times too small against the device
  display" publishes nothing and says as much as a pair of figures.

**No output of this program is offered as safe to send.** `prisma-vent
inspect` in particular names the archive, reports session counts and durations,
and can print statistics measured from your recording.

If a defect genuinely cannot be reproduced without real data, say so and it will
be worked out with you — usually by asking you to run a command locally and
report what it printed, rather than by asking for the file.

## Scope

This policy covers the code in this repository. It does not cover the
ventilator, its firmware, or the manufacturer's software. If you believe you
have found a defect in the **device**, that is a matter for Löwenstein Medical
and, depending on where you live, your medical device regulator. This project
has no affiliation with the manufacturer and cannot forward such a report.
