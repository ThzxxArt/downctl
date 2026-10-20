# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.5] - 2026-10-20

"Carry": a decision a run makes now reaches the line that acts on it. The three
previous releases fixed *what a claim rests on*; this one fixes the journey —
which credential goes to which host, which proxy may reach which target, and
which header a version actually came from. Most of the list below is one defect
in different clothes: a decision was made correctly and then dropped, shadowed or
re-derived one layer further down.

Three defaults changed (the HuggingFace token and a mirror, a guessed proxy and a
local target, an evidence hook and an unverified completion). All three are in
`docs/upgrading.md`.

### Fixed

- **A credentialed target is now probed with its own headers (0.1.4 regression).**
  `_probe_task` called `probe_metadata` without `task.headers`, and the probe had
  no parameter to receive them. Private HuggingFace repositories, private GitHub
  assets and any source that authenticates through `DownloadTask.headers`
  answered `401` to the probe — classified permanent — while the same URL
  answered `200` to the transfer, so not one byte was fetched. 0.1.3 was immune
  only because it skipped probing whenever a size was known. The probe and both
  recovery probes (`_witness_changed_again`, `_reprobe`) now carry the headers,
  and a probe is bounded by `PROBE_TIMEOUT` (30 s) so a hung socket cannot park
  the phase.
- **A guessed WSL proxy no longer carries a request for this machine.** The host
  proxy that `detect_wsl_proxy` fills in was installed as the session-wide proxy,
  so a download from `127.0.0.1` was routed through the Windows host — where
  `127.0.0.1` is a different machine — and every remote download failed when the
  host was not listening on the guessed port. The proxy now travels per request:
  a *guessed* one applies only to targets off this machine and never to a
  `no_proxy` match; an explicit `-p`, a config `proxy` or an environment proxy
  keeps its full reach.
- **A `file://` copy whose destination is its own source is refused.** With
  `--force` the destination was truncated before the transfer read it, so the
  only copy of those bytes was destroyed and the retry loop chased a source it
  was itself emptying. `--dry-run` now predicts the refusal too.
- **Objects stored with `Content-Encoding` download correctly.** `Accept-Encoding:
  identity` is a request, not a guarantee: an object served with
  `Content-Encoding: gzip` was inflated by aiohttp while `Content-Length` kept
  counting stored bytes, so every range failed a length check — and a range of a
  gzip stream is not decodable at all. The transport and the probe now take the
  body undecompressed.
- **A failing hook is reported.** Four hooks dropped their background task with
  `add_done_callback(set.discard)`, discarding the exception with it: an encrypted
  archive, a corrupt tarball or `timeout = "300"` produced no output and a zero
  exit code. `HookTasks` keeps the task, reads its outcome, and counts it;
  `[core] strict_hooks` turns that count into a non-zero exit.
- **`events = "on_finish"` no longer fires on the wrong event.** Hook options
  were applied with `setattr`, so a string replaced the list and the containment
  test became a substring test (`"on_finish" in "on_task_finish"` is true). Hook
  options are now declared (`OPTIONS`) and validated: a single event name is
  normalised to a list, an unknown event name or an unknown key is refused with a
  message naming what exists.
- **Evidence hooks no longer vouch for a completion they cannot prove.** The
  `checksum` and `manifest` hooks wrote their artifacts for any `ok` completion,
  including a skip whose `witness` was `none`. Both artifacts are consumed by
  `--checksums`, so the next run would have been handed a proof nobody earned.
  Unverified completions now produce neither.
- **S3: four boundaries.** A presigned URL that expires mid-download is re-signed
  during the run instead of failing with `403` (bounded, `403` only); an
  `endpoint` mounted under a path keeps that prefix in both the signature and the
  URL; a listing that repeats its continuation token is refused instead of
  looping, and one that never finishes is bounded; a rejected `HEAD` is reported
  as a credential/region/endpoint problem instead of "size unknown".
- **`--force` together with `--skip-existing` is documented as impossible**
  rather than discovered by the user. (It was already refused at parse time.)
- **The README links to the repository, not to itself.** The long description
  resolves relative links against the PyPI project page, where they 404.
- **`cookbook` claimed a checksum mismatch re-downloads twice.** It re-downloads
  once (`CHECKSUM_FAIL_MAX = 2` counts the failures, not the retries).

### Changed

- **The HuggingFace token is not sent to a configured `mirror` by default.**
  Opt in with `[plugins.huggingface] token_to_mirror = true`; a `401` caused by
  the withheld token names the key.
- **`probe --json` reports `validator_kind`, not `witness`.** One word for two
  vocabularies was how readers came to compare "where the version came from" with
  "what a completion rests on".
- **`[core] strict_hooks`** (default `false`): a hook failure is always reported
  and counted; this decides whether it also changes the exit code.
- **`doctor` reports where each token came from** (`config` or `env`, and which
  key).
- **Quality gates that cannot silently stop gating.** `coverage>=7.10` is pinned
  (the `patch = ["subprocess"]` setting is accepted and ignored before 7.6), CI
  asserts the setting is live, `ruff` gains the `S` and `ASYNC` rule families,
  and `mypy` now checks `tests/` — which is what turns a stale test double into a
  build failure instead of a runtime surprise.
- **The release workflow verifies before it builds**: the tag is on `master`, the
  package version matches it, the changelog has an entry, and the CI workflow
  passed on that exact commit. Publishing depends on that verification.
- **The sdist can build its own documentation**: `mkdocs.yml` and the governance
  files are included, and CI installs from the tarball and runs
  `mkdocs build --strict` on it.
- **Actions are pinned to commit SHAs**, with Dependabot to keep them current.

### Added

- **`downctl/plugin/credentials.py`** — one credential resolver (config beats
  environment, and the source is reportable) plus the environment-variable names
  themselves, so no source invents its own pair.
- **`downctl/plugin/matching.py`** — one `--include`/`--exclude` rule; five
  sources had five copies.
- **`downctl/digest.py`** — one chunked file digest; five call sites had five
  copies with two different chunk sizes.
- **`HookTasks`** — one owner for a hook's background work and its failures.
- **Published test vectors** — the SigV4 signer is now compared against AWS's
  documented example signature (`aeeed9bb…`), not just against its own shape.
- **`tests/unit/test_changelog_truth.py`** — a dated `## [x.y.z]` heading *is*
  the claim that `x.y.z` was released, so an entry may not deny its own release.
  The check reads across line wraps (the sentence it exists for broke between
  "never" and "published") and is itself tested against that historical sentence,
  so the gate has been seen to fail rather than assumed to work.

### Documentation

- **`docs/security-advisory-0.1.3-splicing.md`** (and `.zh.md`): what 0.1.0–0.1.3
  could produce, how to prove a file with `verify --checksums`, and when a
  re-download is the only answer.
- `SECURITY.md`: supported versions, a response-time table, what is in scope, and
  what the tool explicitly does not protect against.
- `RELEASING.md`: post-publish verification, what to do when a release is wrong
  (yank is not a delete), and the release-candidate process.
- `troubleshooting.md`: a failure-class table (what each class means for a
  re-run) and a platform-difference table; the `401` section no longer offers
  `--mirror` as a credential (it is a URL rewrite, and a cross-host rewrite
  carries the token with it), and the WSL proxy section now says which requests
  the guess applies to.
- `configuration.md`: concurrency semantics (`jobs` × `connections` is not a
  global cap; `rate` is).
- `cli.md`: the mutual exclusion above, `validator_kind`, and the two-vocabulary
  note.

### Notes

- `API_VERSION` stays `"1"`; no state format was bumped (`BITMAP_VERSION` stays
  `4`, `COMPLETION_VERSION` stays `2`, failed log stays `v3`).
- A state file written by a *newer* downctl is now reported and left untouched
  instead of being treated as absent, so a downgrade cannot silently destroy it.
- The 0.1.4 entry below was edited after this project published it, which it
  does not normally do. Its opening paragraph asserted the opposite of what
  happened — the tag went to PyPI on 2026-10-18, and a dated entry exists
  precisely because a version shipped — so leaving it in place would have made
  the changelog the one document that denies its own release. The paragraph as
  first written survives in the `v0.1.4` tag and in that release's sdist, and
  `tests/unit/test_changelog_truth.py` now refuses a dated entry that denies its
  own release.

## [0.1.4] - 2026-10-18

"Content truth": a ledger now knows *which version* of an object it describes,
a completion claim without a witness is reported as unproven, and a replaced
object can no longer be spliced into a file that already holds another
revision's bytes.

A pre-release regression review found four defects that each let a claim rest on
something the run had never checked — all four on paths this version introduced
— plus a few pre-existing ones it happened to cross. They are fixed below, each
with a regression that fails when the guard is removed. Where the review
contradicted a sentence already written for this section, the sentence is
corrected rather than annotated.

### Fixed

- **Silent splicing of two revisions (the 0.1.3 defect).** Resuming a download
  whose URL stayed the same but whose content was replaced produced a file made
  of the old prefix and the new suffix, exited 0 and wrote a completion marker —
  sticky corruption that only `--force` could repair. It is now impossible:
  - resume ledgers (`.downctl-pieces`) record the object's version witness; a
    ledger whose witness differs from the one just observed is discarded instead
    of resumed;
  - ranged requests carry `If-Range` when a strong witness is known, so a
    replacement *during* a transfer turns into one clean restart;
  - a `206` whose `Content-Range` does not cover exactly the requested window is
    refused before a byte is written (previously the body was trusted blindly);
  - a server whose witness changes on every response is detected and that target
    continues without one (reported as unversioned) instead of restarting
    forever.
- **Every HTTP(S) target is probed for a witness, even when its size is already
  known.** Sources that report a size (an S3 listing, a HuggingFace API
  response, a GitHub asset) used to skip the probe entirely, which left the
  transfer unconditional and produced a marker that could never detect a later
  replacement — the same defect, still reachable, for exactly the large files
  that matter most. The cost is one `HEAD` per target.
- **Completion markers record the witness**, so `--skip-existing` refuses a file
  the server has since replaced instead of reporting stale content as proven.
  A marker written by 0.1.3 keeps being trusted (reported as `unversioned`) and
  gains the witness only when the run itself obtained or compared the bytes, or
  when a skip was proven by a checksum — a run that fetched nothing has not
  completed anything, and a plain unversioned skip deliberately keeps the marker
  witness-less, because stamping today's version onto a file fetched under an
  unknown one would hide the next replacement.
- **Probing no longer misclassifies transient failures.** `probe` bypassed the
  transport's error classification, so a single `500` during the size probe was
  reported as a permanent failure after two requests. `5xx`/`429`/`408`/`425`
  and network failures are now retried (bounded, with `Retry-After` honoured),
  and probing raises `ProbeError` only after the retries are exhausted.
- **`416` and content changes are recoverable.** An unsatisfiable range (the
  object shrank) used to be a permanent failure; the engine now re-probes the
  size, resets the target once and downloads the current object.
- **`pieces._fsync_dir` never fsynced anything.** It opened the directory with
  `open(dir, "rb")`, which raises `IsADirectoryError` on POSIX — swallowed by a
  `suppress(OSError)` — so the durability step that makes a bitmap rename
  crash-safe silently did nothing. It now uses `os.open(dir, O_RDONLY)`.
- `--skip-existing` on an object whose version cannot be checked says so: the
  run warns that the skip is `unversioned` and reports
  `"witness": "none"` in the hook payload and dry-run JSON.
- **A `HEAD` rejection no longer hides a downloadable object.** An S3 presigned
  URL signs the *method*, so a GET-signed link answers `HEAD` with `403` while
  the ranged `GET` the transfer actually uses succeeds; `401`/`403`/`405`/`501`
  now fall back to a ranged `GET` instead of being reported as final.
- **The probe and the transfer agree on `Accept-Encoding`.** The probe let
  aiohttp ask for `gzip, deflate` while the transport pinned `identity`, so on a
  compressing server the probed length described a body the transfer never
  received, and every piece failed its `Content-Range` check.
- **The target lock is released on every path**, including an exception raised
  by a reporter or a third-party verifier during a skip. It used to be left
  behind as a stale `.lock`, which failed the *next* run for that target.
- **The batch disk budget bills a file once.** Two entries resolving to the same
  path (a mirror and its origin, or `--retry-failed` next to the original
  target) were summed separately, so a batch could be rejected as "does not
  fit" while it needed only the space of one copy.
- **`verify --repair` no longer deletes a file it cannot disprove.** A file whose
  *length* is unchanged but whose mtime moved was reported as a mismatch, so
  `--repair` removed a byte-identical file (reproduced: sha256 unchanged
  afterwards, directory emptied). Only a different length proves the bytes are
  not the object the marker describes; a touched file is reported
  `unverifiable` and left alone.
- **`--max-time` covers the probe stage.** The deadline was computed when the
  *transfer* started, so a probe against a server that never answers ran
  unbounded (measured: `--max-time 3` exited after 11.2s). The deadline is now
  recorded when the target starts, and the transfer inherits whatever the probe
  did not spend.

Found by the pre-release review, on paths this version introduced:

- **A completion marker could be stamped with a witness this run never
  compared.** `resolve` hands back an all-ones grid for a marker trusted by the
  lenient rule, so `transfer_file` returned `complete=True` without making a
  single request — and the engine then wrote the freshly probed witness into the
  marker. From then on `--skip-existing --require-witness` reported the stale
  file as `witness: validator`: the strictest flag in the tool certifying
  content the run never looked at. `TransferResult.witness_bound` now carries
  whether this run obtained or compared the bytes, and the marker keeps its
  recorded witness when it did neither.
- **`verify --repair` deleted a file the manifest never named.** The digest
  lookup fell back to the bare file name, so a manifest listing only `data.bin`
  was applied to `v2/data.bin` — a different object — which then read as a
  `mismatch` and was removed. Attribution is now exact: only path keys count,
  and an entry that cannot be attributed unambiguously is reported as
  `unverifiable` instead of guessed.
- **A finished file was truncated before its replacement existed.** A witness
  mismatch ran `discard()`, which truncated the data file *before* a single
  replacement byte had been requested; if the new revision could not be fetched
  (a `503`, a dropped network) a complete file was gone with nothing in its
  place and no message saying so. Untrusted state is now *abandoned*: the marker
  is removed and an all-zero ledger is pinned, so the file reads as 0 % complete
  and its bytes survive to be overwritten. The single-stream path likewise drops
  nothing until the replacement response is open.
- **A server that ignored `If-Range` while honouring `Range` still spliced two
  revisions.** Every piece arrived as a well-formed `206` with an exact
  `Content-Range`, drawn from whatever revision was current — correct total
  size, exit code 0, completion marker written. `If-Range` is a request a server
  may ignore, so the response's own validator is now checked against the witness
  this run observed; a response that names a different one raises
  `ContentChangedError`, and one that names none leaves the completion
  unversioned rather than claiming a version nothing supports.
- **`finish` records carried no `reason` for engine-stage failures.** The JSON
  reporter reads `task.meta["failure"]` while emitting the record, and the
  engine classified the failure *after* notifying it — so the documented field
  was unreachable for every engine failure, while probe failures (which classify
  first) did carry it. Classification and notification are now one step
  (`engine._failed`), and all fourteen failure sites go through it.
- **A skip reported `witness: validator` without comparing anything.** With no
  probe at all (a local or SFTP transport) the marker's recorded witness was
  presented as version proof, and the same write blanked that witness — so the
  next HTTP run, which *could* have compared it, found nothing left to compare.
  The claim now requires an actual comparison, and a recorded witness is never
  erased by a run that could not check it.
- **A recorded checksum was blanked by any run without `--checksum`.** The
  marker was rewritten from the task's own fields, so re-running a completed
  download dropped the digest the original run had verified. Existing proof is
  preserved when the new run has none of its own.
- **`verify` failed open.** A path that was not a directory, or did not exist,
  produced "nothing to verify" and exit code 0 — indistinguishable from
  "everything proven". It is now an error.
- **`verify --checksums` silently ignored manifest entries.** A manifest is an
  independent claim about what should be there, so an entry whose file is
  present is now judged whether or not it carries a completion marker (reported
  with `from_manifest: true`). Previously a corrupt file the user explicitly
  asked about was skipped and the run still exited 0.
- **`config show` hid three options.** `max_time`, `probe_jobs` and
  `require_witness` were added in 0.1.4 without ever appearing in the output a
  user checks their file against, because the list was hand-written. It is now
  derived from `CoreConfig`, so a new option cannot be invisible again.

Pre-existing, crossed while reviewing:

- **A zero-length object was reported dead.** A `HEAD` answering
  `Content-Length: 0` was read as "no length", so the probe fell through to a
  ranged `GET` for `bytes=0-0` — unsatisfiable against an empty body — and the
  server's `416` became a permanent failure. An explicit zero is an answer, not
  a missing one.
- **A probe honoured an arbitrarily long `Retry-After`.** The 300 s cap was
  applied on the transfer path only, so a probe against a server asking for a
  five-minute wait slept for five minutes. Both paths now share one pacing
  policy.
- **The same URL twice in one batch failed the second copy.** Two targets
  resolving to one destination acquired the same target lock, so the second
  reported "locked by another process" — a failure about neither the network nor
  the object. Targets are now de-duplicated by `(url, destination)`, with the
  order the user gave preserved.
- A tautological `assert sys.version_info >= (3, 11)` sat in the test suite,
  asserting what `requires-python` already guarantees. Removed.
- **On Windows, a lock left by a killed process was never taken over.**
  Liveness was decided by whether `OpenProcess` succeeded, but Windows keeps a
  process object — and its pid — alive until the last handle to it is closed,
  so a killed downloader still opened cleanly and its lock looked held forever.
  The exit code is now read instead: only `STILL_ACTIVE` counts as alive. This
  is the "kill it and re-run" case the staleness rule exists for, and it did not
  work on Windows.

### Added

- **`downctl verify [DIR] [--json] [--checksums FILE] [--repair]`** — the
  offline proof/repair channel: every file that claims completion is reported as
  `ok` / `mismatch` / `unverifiable` / `missing`, together with the witness that
  proved it. Exit codes: `0` proven, `1` mismatch or missing data file, `2`
  nothing wrong but nothing provable. `--repair` deletes only what was *proven*
  bad (plus orphan markers).
- `--require-witness` / `[core] require_witness`: refuse to skip or resume state
  whose object version cannot be checked.
- `--recheck`: re-hash existing files against their recorded or source digest
  before skipping (implies `--require-witness`).
- `--max-time SECONDS` / `[core] max_time`: wall-clock cap for a single target.
- `--probe-jobs N` / `[core] probe_jobs` (default 8): targets are now expanded
  and probed with bounded concurrency instead of one round trip after another,
  while failure reporting keeps the order the targets were given in.
- **Batch disk budget**: the whole run is pre-flighted against the filesystems it
  will write to (per device, plus the 200 MiB reserve). A batch that cannot fit
  fails before the first byte with one message naming the need and the free
  space, instead of dying with `ENOSPC` half-way through.
- `Retry-After` is honoured on both the probe and the transfer path (capped at
  300 s), and `downctl probe --json` reports the version witness.
- Per-module coverage gates in CI: `core/pieces.py`, `core/transfer.py` and
  `core/engine.py` must stay at or above 95% (a global 85% hid the state machine
  that decides whether a file counts as finished), and the subprocess tests now
  measure `python -m downctl` itself.

### Changed

- **Retries are finite by default** (`[core] retries = 10`); `-r 0` still means
  "retry forever", but the probe stage stays bounded either way. An unbounded
  retry loop as the default turned a permanently failing server into a silent CI
  hang.
- **The `sink` plugin category was removed.** It was never called on the write
  path (the core always wrote through `core/transfer.py`), so it promised a
  replaceable destination that never took effect. The `downctl.sinks`
  entry-point group is retired and the loader now warns when an installed
  package still declares it, instead of ignoring it silently. `API_VERSION`
  stays `"1"`: plugins in the other six categories are unaffected.
- `--skip-existing` treats a skip as one of three states — `validator`,
  `checksum` or `none` — instead of one blanket "completion proven".
- The test suite now holds real content invariants: it compares a SHA-256 of the
  final file against the server's current revision (not a piece flag or an exit
  code), and it starts real `python -m downctl` processes to test `SIGTERM`
  (exit 130 with progress kept) and `kill -9` followed by a resuming restart.
- **Failures are classified, not merely described.** The failed log gains a
  ninth column and the JSON stream gains a `reason` field, both carrying
  `permanent` / `transient-exhausted` / `timeout`, so a machine can tell a `404`
  from a `5xx` whose retries ran out without parsing an error string. The column
  is *appended*: an 0.1.3 reader parses the first eight fields unchanged.
- **The JSON `finish` record carries the witness** (`validator` / `checksum` /
  `none`), so `ok: true` can no longer be mistaken for an unversioned skip that
  merely exited `0`.
- **`pytest-cov>=6`.** `[tool.coverage.run] patch = ["subprocess"]` merges
  child-process data into a branch-measured parent; pytest-cov 5 hands the child
  a statement-only configuration, and `pytest --cov` dies with
  `DataError: Can't combine statement coverage data with branch data`
  (reproduced on a clean virtualenv).
- Untrusted resume state no longer truncates the data file. A file whose
  replacement could not be fetched stays on disk at full size while its ledger
  reads 0 % complete; `downctl status` reports it as incomplete.
- A completion no longer claims a version when the server does not name one on
  the response that carried the bytes (for example an `ETag` on `HEAD` but not
  on the ranged `GET`). Such a file is reported as unversioned.

### Documentation

- The marker entry above was corrected in place: it had said a marker "gains the
  witness when the run actually *completes* the target", which is the defect it
  now describes — a run that fetched nothing has completed nothing.
- `docs/cli.md` and `docs/cli.zh.md` document the manifest's role in `verify`,
  the new `from_manifest` field, the error exit for a bad path, and the
  response-validator rule. `docs/design.md` / `docs/design.zh.md` record the
  three new invariants (witness bound to what was checked, abandonment instead
  of destruction, response-validator enforcement).
- `docs/troubleshooting.md` / `.zh.md` explain that the WSL host-proxy
  auto-detection is a guess and how to turn it off entirely
  (`WSL_HOST_IP=127.0.0.1`), and `docs/configuration.md` / `.zh.md` say the same
  next to the variable.

### Notes

- **No state-format version was bumped.** The `validator` field is additive in
  both the ledger and the marker, and older builds read the files they wrote
  without noticing it. That is deliberate: 0.1.3 refuses a marker whose version
  is newer than it knows and then truncates the *completed* file, so a version
  bump would have turned "downgrade to 0.1.3" into data loss. Downgrading is
  safe.
- Upgrading from 0.1.3: an **incomplete** ledger has no witness, so it restarts
  once whenever the server names a version (the normal case) and resumes as
  before when the server offers none. Completed files, markers and the failed
  log are untouched. See `docs/upgrading.md`.
- The failed-log format also stays as it is: the retry path always re-probes, so
  a witness stored there would only ever be stale evidence.

## [0.1.3] - 2026-10-17

"State truth": a ledger now belongs to exactly one remote object, completion
has to be provable, and failures can be retried without replaying expired
credentials.

### Added

- **URL-bound resume state (bitmap v4)**: every bitmap records a SHA-256 of the
  canonical URL (the rotating query of a presigned URL is excluded). A bitmap
  written for another URL — or by 0.1.2 or older, which has no identity — is
  discarded instead of being resumed into a different object.
- **Completion markers** (`<file>.downctl-done`, size + mtime + URL identity,
  plus the verified checksum when one was used): a finished size-known file is
  skipped without trusting its size, including on WSL DrvFS and Windows where
  holes cannot be detected. Unknown-size or non-Range single-stream downloads
  have no bitmap; `--skip-existing` can skip them by the marker. The marker is
  invalidated by an external modification; a changed source digest forces a
  re-hash instead of trusting a stale skip.
- **Periodic durable checkpoints** (`--checkpoint SECONDS`, `[core] checkpoint`,
  default 30, `0` disables): the data file is `fsync`-ed and the bitmap written
  atomically while a transfer runs, so `SIGKILL` or power loss loses at most one
  interval. `SIGTERM` is converted into a graceful interrupt.
- **Failure recovery by origin (failed log v3)**: records store the target the
  user asked for; `--retry-failed` re-resolves it, so S3 presigned URLs are
  re-signed and private HuggingFace/GitHub downloads get the current token.
- `downctl status --json` and `downctl clean --json`.
- `--reporter json` emits a `failure` record for targets that fail during
  resolve/probe, and `run_finish.failed` counts them (not just engine failures).
- Completion markers v2 also record the verified checksum, so a changed source
  digest no longer lets `--skip-existing` bless stale content.
- `clean` removes recognized orphaned completion markers whose data file no
  longer exists; a user data file that merely ends in `.downctl-done` is left
  alone (it is not a valid marker).
- Verifiers for `sha1`, `sha512` and `md5`, so every algorithm the `checksum`
  hook can write is consumable via `--checksums`. Unlabelled `*sum` files are
  matched by digest length (32/40/64/128 hex characters).
- `--checksums` fails closed: every requested target must have an entry, and
  an empty/unusable manifest is rejected before downloading.
- Resume identity is bound to the final fetched URL (after mirror routing), so
  switching a mirror invalidates old pieces/markers.
- Python 3.14 in the CI matrix and package classifiers.

### Fixed

- `--skip-existing` no longer blesses a full-size file with holes (an
  interrupted multi-connection download from an earlier run). It skips only
  when a marker, a complete bitmap or a provably hole-free file proves
  completion, and re-verifies an explicitly supplied checksum.
- `--skip-existing` also refuses a complete bitmap whose data file has been
  truncated (the ledger's durable high-water mark is checked) and refuses an
  oversized stale file instead of truncating an unproven prefix and calling it
  complete.
- A complete bitmap is only trusted when the file covers every claimed piece
  and the grid is internally consistent; corrupt/foreign ledgers (including a
  bad `piece_size`/`pieces` combination or a malformed sidecar) are reset
  rather than fabricating success. A bitmap without an identity is never
  trusted.
- `clean` no longer deletes the bitmap/marker of a transfer whose lock is still
  live; hook/plugin loading errors are passed through the URL redactor. It only
  removes recognized bitmaps, valid orphan completion markers, temp files and
  dead lock directories, so a user data file or directory whose name merely
  ends in those suffixes is never touched. Failed removals are reported and
  make `clean` exit `1` instead of being counted as success.
- A checkpoint whose data `flush`/`fsync` fails is skipped, so the ledger never
  claims bytes that did not reach disk.
- Webhook HTTP 4xx/5xx responses now produce a warning, and webhook payload
  strings (including `on_error`) are redacted recursively.
- `--user` Basic auth uses UTF-8 (matching aiohttp's helper), so non-ASCII
  credentials no longer raise.
- Malformed URLs and schemeless `user:pass@host` proxy strings are sanitized
  instead of printing credentials.
- A source checksum is re-hashed before `--skip-existing` when the proof is a
  complete bitmap or a hole scan rather than a completion marker.
- Resolve failures are recorded in `.downctl.failed` so private-repo failures
  are retryable; `-o` combined with `--checksums` matches the remote filename;
  an enabled `exec` hook no longer injects output into JSON stdout; WSL
  drive-relative/UNC paths are rejected with a clear error.
- Disk pre-flight now budgets the whole object when resume is impossible (no
  bitmap, stale bitmap, truncated/oversized file), instead of counting only
  `size - current` for a file the transfer will reset.
- `--dry-run` no longer rewrites `.downctl.failed` (which discarded unrelated
  pending failures) and no longer creates the destination directory.
  `--dry-run --reporter json` now emits one `dry_run` record per target instead
  of silence.
- `-v` no longer contaminates machine output: `get --reporter json` and
  `probe`/`status`/`clean` with `--json` keep stdout pure even with verbose
  config logging enabled.
- Webhook failure warnings, aiohttp/proxy exception messages and `config show`
  mirror keys are redacted like the rest of the credential surface; a legacy
  failed-log URL is masked by `status`.
- A periodic checkpoint that cannot write its ledger now warns once (the
  previous code ignored `save`'s failure) and the atomic write stays ordered
  with the final snapshot instead of racing a worker thread on cancellation.
- `status`/`clean`/`config`/`plugin`/`doctor` errors (and a closed stdout) are
  reported through the same clean CLI guard as `get`/`probe`, instead of a
  traceback.
- `--user` no longer calls `aiohttp.encode_basic_auth` (missing from several
  supported aiohttp releases); Basic auth is encoded directly.
- Resuming `hf://repo@v1` after switching to `@v2` (or two same-size URLs) can
  no longer splice old pieces into the new file.
- Re-running a completed size-known download on WSL DrvFS no longer re-downloads
  it from scratch: the completion marker is portable proof. Native Windows no
  longer trusts marker-less full-size files (holes are undetectable there).
- `kill -9`/power loss no longer throws away everything since the retry loop
  started (periodic checkpoint).
- Failed S3/private-repo records can be retried: fresh signatures/tokens
  instead of an expired URL. `--retry-failed` matches exact target paths; the
  basename fallback is limited to `-o` renames, so a moved/same-named object is
  not silently substituted. Legacy `file://`/`sftp://` records without an
  origin infer their transport from the stored URL instead of using HTTP.
- Presigned queries and tokens no longer leak through the JSON reporter,
  webhook payloads, `exec` environment, error messages or `config show`.
- `--reporter json` stdout is pure JSON-lines: human summaries are suppressed
  (warnings/errors stay on stderr).
- A successful run no longer leaves an empty `.downctl.failed`.
- `plugin list`/`plugin info` no longer execute local plugin directories unless
  `--local-plugins` is passed.
- Local `OSError`s are reported as I/O errors instead of "network error".
- Run summary throughput counts bytes actually transferred, not the sum of
  requested sizes (resumed/skipped files no longer inflate it).
- `--dry-run` reports `total / done / to fetch` and accounts for resume state.
- An unknown checksum algorithm fails before downloading, with the available
  algorithms listed, instead of re-downloading twice.
- `--force` with `--skip-existing` is rejected instead of silently letting one
  win; invalid numeric options (`--jobs 0`, `--stall 0`, `--retries -1`,
  `--piece-size 1`, `--limit-rate 0`) are rejected with clear messages.
- The rate limiter no longer banks idle credit: a burst cannot exceed the
  requested rate after a retry backoff or idle period.
- tqdm per-task bars allocate distinct rows, so concurrent unknown-size
  downloads no longer overwrite each other.
- `downctl probe` exits `1` for unreachable, non-HTTP(S) or HTTP >= 400
  targets, and says why (text and `--json`); a client timeout is reported as
  unreachable instead of a local I/O error.
- A server that rejects `HEAD` with 405/501 no longer breaks probing: the size
  is read from a ranged `GET` instead of the error page's `Content-Length`.
- Windows paths under WSL (`-d 'D:\models'`, `-o 'D:\x'`) are translated to
  `/mnt/d/...` instead of creating a directory with a backslash in its name.
- Bitmaps that would exceed 2,000,000 pieces (e.g. 100 GiB with 1 KiB pieces)
  are rejected instead of writing a hundreds-of-megabytes ledger.
- `doctor` reads the proxy from the config file as well as the environment.
- Missing `exec`/`checksum` entries were added to the English plugin table, and
  the docs no longer claim that `kill -9` resumes without a checkpoint.

### Changed

- The minimum aiohttp is now **3.11**. downctl uses the session-level
  `proxy` argument (added in 3.11), so the previous `>=3.9` metadata allowed
  installations that crashed when `--proxy` was configured.
- `--skip-existing` means "skip when completion is provable", not "skip when
  the size matches".
- On Windows and WSL DrvFS, a full-size file without a completion marker is
  re-downloaded (use `DOWNCTL_TRUST_PARTIAL=1` to force trust once).
- Failed-log records from 0.1.2 and older are still read; when they lack an
  origin, retry falls back to the stored URL with a warning (presigned URLs are
  likely expired).
- The failed log is written atomically with owner-only permissions where the
  platform supports it (it may contain a URL with a token, which is required
  for retrying).
- `probe` exits `1` when any target fails.
- `downctl plugin` loads local plugin directories only with `--local-plugins`.
- `downctl status`/`clean` print one JSON object with `--json`.

### Removed

- `hf-download.py` (legacy single-file script; not part of the package).
- The unused `aioresponses` development dependency.

### Documentation

- README (zh/en), CLI, configuration, hooks, sources, troubleshooting,
  upgrading and design pages updated for URL identities, completion markers,
  checkpoints, origin-based retry, redaction, JSON purity and the 0.1.3
  behaviour changes.
- Default config template now documents `checkpoint`, `piece_size`,
  `min_split_size`, `[plugins.s3]` and `[plugins.sftp]`.

## [0.1.2] - 2026-10-17

### Added

- resume-aware progress: the engine computes already-complete bytes from the
  piece bitmap and drives optional `run_resumed` / `task_resumed` reporter
  hooks, so bars and JSON no longer understate a resumed download
- bitmaps record their piece size (v3) and are **resliced** when
  `--connections` / `--piece-size` change, instead of discarding progress
- `--force` (re-download from scratch) and `--skip-existing` (skip files that
  already exist with a matching size)
- `--piece-size SIZE` and `--min-split-size SIZE` (plus matching `[core]` keys)
- `-l -` reads targets from stdin (`-l` already accepted a file)
- `downctl probe --json` (one JSON object per target)
- `downctl completions bash|zsh|fish` — dependency-free completion scripts
- `exec` hook (run a shell command on chosen events; `DOWNCTL_*` env vars) and
  `checksum` hook (write `<file>.<algo>` digest sidecars)
- a run summary with elapsed time and average throughput

### Fixed

- `--checksum` with more than one target is rejected (it previously applied the
  same digest to every file); use `--checksums FILE` for batches
- unknown-size streaming downloads now perform a disk-space pre-flight once the
  length is known (`core.disk`)
- GitHub release assets of private repositories are fetched through the API
  asset URL with `Accept: application/octet-stream`, so the token stays on the
  API host instead of leaking to the redirect/CDN host

### Changed

- `-v/--verbose` now emits real diagnostics (resolved tasks, mirror rewrites,
  resume points, effective config)
- test-coverage gate raised to 85%

### Documentation

- new bilingual **Cookbook** page (recipes); README and docs updated for the
  new options, hooks and commands

## [0.1.1] - 2026-10-17

### Fixed

- transfer: an `OSError` raised inside the download task group is unwrapped
  and re-raised, so a disk error fails only that file instead of aborting the
  whole batch
- single-stream fallback now verifies the received length against the expected
  size (`task.size`), not only the response's `Content-Length`, preventing
  silent truncation when a server returns a shorter body
- rate limiting no longer trips the stall watchdog at low limits (a heartbeat
  tracks deliberate throttle waits as activity)
- failed log preserves previous failures for targets not touched by the run
- `clean` no longer removes lock directories owned by a live process
- probe treats any HTTP >= 400 as a failure (4xx permanent, 5xx/other via
  the transport's transient classification)
- an explicit `Authorization` header now overrides configured credentials
  case-insensitively (no duplicate headers)
- source-provided absolute targets are rejected unless explicitly requested
  (`-o/--output`), completing the path-traversal fix
- HTTP transport sends `Accept-Encoding: identity` so length checks are not
  broken by transparent gzip decompression
- SFTP verifies host keys against `known_hosts` when available (warns when
  not; `known_hosts = ""`/`none`/`false` disables); URL credentials are
  percent-decoded; task URLs keep the port
- tar extraction rejects FIFOs/device nodes (also on Python 3.11)
- entry-point plugin loading catches all errors; local modules are registered
  in `sys.modules`
- mirror router matches on a host/path boundary; `webhook` honours an explicit
  empty `events` list; checksum files skip malformed digests
- `-q/-v` are accepted after nested `plugin`/`config` subcommands; the
  `filesystem` sink is implemented
- `init_console`/`safe_io` are now actually invoked (Windows VT colors and
  UTF-8-safe redirected output)
- the HuggingFace token is attached per-request to the HF host only (no
  longer sent to arbitrary download hosts); presigned S3 URLs are redacted in
  logs; `clean` only removes locks whose dead owner can be verified

### Changed

- version bumped to 0.1.1
- `-r/--retries` counts retries (total attempts = retries + 1); the retry
  backoff base is overridable via `DOWNCTL_RETRY_BASE`

### Documentation

- README (zh/en) gains an **Upgrade** section (pip/pipx/source/Docker, verify,
  what is left untouched)
- the docs site is expanded with bilingual Source, Configuration, Hooks,
  Troubleshooting and Upgrading pages (9 pages per language)

## [0.1.0] - 2026-10-17

First release.

### Added

- P1 skeleton: package layout, plugin contracts (7 categories), plugin
  registry/loader/event bus, CLI surface, TOML configuration, packaging and
  CI.
- P2 core engine:
  - resumable piece bitmap (`core.pieces`) with authoritative out-of-order
    resume, `fsync`-backed ledger, atomic save and hole-detection guards
  - multi-connection transfer (`core.transfer`) with stall watchdog, single
    stream fallback for unknown sizes and Range-less servers
  - download orchestration (`core.engine`) with retries, disk pre-flight,
    file locking, checksum verification and a concurrent task runner
  - transport contract now exposes a streaming `Response`; the HTTP transport
    maps aiohttp errors onto the downctl exception hierarchy so the core is
    HTTP-library agnostic
  - integration tests with a controllable local HTTP server (full download,
    out-of-order resume, Range fallback, unknown size, stall-then-resume)
- P4/P5 usable CLI and built-in plugins:
  - `downctl get` resolves targets through source plugins, probes sizes,
    applies mirror rewrites and runs the engine with a chosen reporter
  - `downctl probe`, `plugin list|info`, `config init|show|path`, `doctor`
    and `wizard` are implemented
  - options: `-d/-o/-j/-t/-r/-p`, `--checksum`, `--mirror`, `--source`,
    `--reporter`, `--include/--exclude`, `--dry-run`, `--retry-failed`,
    `-l/--list`, `--local-plugins`
  - failed-target log (`.downctl.failed`) with `--retry-failed`
  - session factory, TOML config wiring and `DOWNCTL_CONFIG` override
  - integration tests drive the real CLI flow against a local server
- HuggingFace source plugin:
  - expands `hf://org/name[@revision]` (and bare `org/name`) for models and
    datasets via the Hub API
  - honours `--include`/`--exclude`, `--revision` and the
    `[plugins.huggingface]` mirror/token options; verifies LFS sha256
  - integration tests against a fake Hub plus real-world verification
- Docs and repository tooling:
  - `py.typed` marker (PEP 561) and inline types
  - docs site config (`mkdocs.yml`) plus CLI and plugin guides
  - issue/PR templates, Dependabot config and `RELEASING.md`
- GitHub Releases source plugin (`gh://owner/repo[@tag]`):
  - lists release assets via the REST API, honours `--include`/`--exclude`
  - uses asset `sha256` digests for verification; `[plugins.github]` api/token
  - integration tests against a fake API plus end-to-end download
- configurable `connections` and `stall` (config and `--connections`/
  `--stall`), threaded through the engine
- S3 source plugin (`s3://bucket/key` and prefixes):
  - SigV4 query-string presigning (no extra dependency), so the HTTP
    transport fetches S3 objects incl. ranged transfers
  - prefix listing via ListObjectsV2 with pagination; `[plugins.s3]`
    endpoint/region/credentials/path_style; object size via presigned HEAD
  - tests against a fake S3 endpoint plus SigV4 unit tests
- resume and failure handling:
  - failed log v2 stores the resolved URL, target path, checksum and size,
    so `--retry-failed` restores repo subpaths and verification
  - `--continue` skips targets that fail to resolve or probe instead of
    aborting the whole run
- unified console output with `-q/--quiet` and `-v/--verbose`; quiet mode
  also switches to the `quiet` reporter; tqdm progress bar polished
- `downctl status` scans a directory for in-progress (piece bitmap) and
  failed downloads
- hook plugins can be enabled with `--hook NAME` or `[core] hooks = [...]`
- `--user-agent` for a custom HTTP User-Agent
- Dockerfile (multi-stage, wheel-based) and `.dockerignore`
- aggregate progress: the tqdm reporter shows an overall bar across files
- `archive` hook extracts zip/tar archives on completion (path-traversal and
  link-safe); `keep`/`into` options
- `--checksums FILE` verifies downloads against a `sha256sum`-style file
- SFTP source and transport (`sftp://[user@]host[:port]/path`), optional
  `asyncssh` dependency (`downctl[sftp]`); supports directory listing and
  ranged (multi-connection) transfers
- `webhook` hook POSTs selected lifecycle events as JSON
- `manifest` hook writes a `sha256sum` manifest of completed downloads
- coverage gate (75%) in the test configuration and CI
- documentation: the primary `README.md` is now a detailed Chinese guide
  (install, usage, sources, configuration, plugins, troubleshooting); the
  English version lives at `README.en.md`
- global rate limiting: the `--limit-rate` budget is now shared across all
  concurrent files (previously each file was limited independently)
- `-H/--header` (repeatable) and `-u/--user USER:PASS` for custom headers and
  HTTP Basic auth
- `downctl clean [DIR]` removes leftover bitmaps, the failed log and stale
  lock directories
- `file://` source and transport for local files/directories (recursive,
  ranged); console warnings/errors are now colored on a TTY
- concurrent downloads (job > 1) show an overall progress bar with a
  completed-files count; optional shell completion via `argcomplete`
  (`downctl[completion]`)
- docs site published to GitHub Pages (Actions-based, keeping a single
  master branch) with a generated coverage badge; CI gains a `pip-audit`
  dependency-vulnerability job

### Fixed

- webhook hook now flushes in-flight POSTs at shutdown (via an event-bus
  `aclose`), so late events such as `on_task_finish`/`on_finish` are no
  longer dropped
- the CLI reports network/library errors as clean messages instead of
  dumping a traceback (use `-v` for the traceback)
- single-stream downloads no longer cancel the read generator to poll for
  progress (which could look like a clean EOF and truncate the file); a
  watchdog task aborts instead
- reject piece bitmaps whose length does not match the piece count
- confine source-provided relative targets to the destination directory
  (mitigates `..`/percent-encoded path traversal from remote metadata)
- lock acquisition treats a missing pid file as held (avoids a takeover race)
- a file that cannot be opened for writing fails instead of retrying forever
- an unknown checksum algorithm now fails the file instead of silently passing
- checksum give-up discards the bad file so it is not trusted on the next run
- SFTP: guard connection creation with a lock, key the cache by credentials,
  evict broken connections, and never leak inline credentials across hosts
- archive hook normalizes backslashes (Windows traversal) and extracts off the
  event loop; `logfile`/SFTP key paths expand `~`; manifest uses POSIX
  separators; GitHub tag is URL-quoted and private assets carry the token;
  JSON reporter throttles progress; HTTP 408/425 are retried

### Changed

- version is now dynamic, sourced from `src/downctl/__init__.py`
- the docs site is bilingual (English + 简体中文) via `mkdocs-static-i18n`

[0.1.4]: https://github.com/ThzxxArt/downctl/releases/tag/v0.1.4
[0.1.3]: https://github.com/ThzxxArt/downctl/releases/tag/v0.1.3
[0.1.2]: https://github.com/ThzxxArt/downctl/releases/tag/v0.1.2
[0.1.1]: https://github.com/ThzxxArt/downctl/releases/tag/v0.1.1
[0.1.0]: https://github.com/ThzxxArt/downctl/releases/tag/v0.1.0
