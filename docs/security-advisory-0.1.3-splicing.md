# Security advisory: spliced files (0.1.0 – 0.1.3)

**Affected:** downctl 0.1.0 through 0.1.3
**Fixed:** 0.1.4 stops producing them; 0.1.5 documents the detection path below
**Severity:** high — silent data corruption, no error surfaced
**CVE:** none requested; this is a pre-1.0 tool and the fix is a regular release

## What happened

downctl downloads a file as several independent byte ranges and tracks them in a
resume ledger. Up to and including 0.1.3 the ledger recorded *which byte ranges*
were done but not *which version of the object* they came from.

If the remote object changed while a download was in progress — a re-uploaded
model, a rebuilt release asset, a mirror that filled itself in behind the
scenes — the ranges fetched after the change came from the new object and were
written next to the ranges fetched before it. The result was a file that was
neither version: a splice. The download then reported success, and the
completion marker agreed.

0.1.3 did bind the ledger to the URL, so a *different* URL could not resume
another one's pieces. The gap was the same URL serving different bytes, which is
exactly what an object replacement looks like.

## What it means for files you already have

A file downloaded with an affected version is suspect **only if its remote
changed during the download**. That is not something the ledger can tell you
after the fact: a spliced file's marker is internally consistent, and 0.1.5 still
accepts markers written by 0.1.3 — it has no way to know the bytes were mixed.

So the question to ask is not "which version did I use" but "can I prove this
file's bytes". There are two answers.

## How to check

### 1. Against the upstream checksums (definitive)

If the project publishes `SHA256SUMS` (or any `sha256sum`-style file):

```bash
downctl verify ~/models --checksums SHA256SUMS
```

Exit codes:

| Code | Meaning |
|---|---|
| `0` | Every file matched. Your files are correct — a splice would have failed the digest |
| `1` | At least one file **mismatched**. Delete it and download it again |
| `3` | The files exist but could not be proven (no checksum for them, or none recorded). `2` is reserved for argparse usage errors |

A mismatch is the signal. A digest computed over a spliced file does not match
the upstream digest unless both halves came from identical bytes — which would
mean the object did not actually change.

### 2. Re-download (when no checksum exists)

For a file you cannot verify, fetch it again with 0.1.4 or newer:

```bash
downctl get <target> --force
```

`--force` discards the existing file and the resume state. On 0.1.4 and later the
result cannot be a splice: pieces are conditional on the object's version and a
change mid-transfer throws them away instead of keeping them.

## What changed in the fix

| Version | Change |
|---|---|
| 0.1.3 | The ledger is bound to the URL (a different URL cannot resume it) |
| 0.1.4 | The ledger and the completion marker carry the object's **version witness**; ranged requests are conditional (`If-Range`); a mid-transfer change discards the stale pieces and reports what happened. `downctl verify` proves files offline |
| 0.1.5 | The probe carries each task's own headers, so a credentialed target is measured with the same authority it is fetched with — a 0.1.4 regression that could stop a private download before it started |

## If you believe you have a spliced file

1. Run `downctl verify` as above. Keep the exit code.
2. If it reports `mismatch`, the file must be re-downloaded — the corruption is
   not repairable.
3. If it reports `unverifiable`, there is no evidence either way. Prefer
   `--force` for anything you depend on.
4. Report it if a re-download with 0.1.4+ still produces a mismatch: that would
   be a new defect, not this one.
