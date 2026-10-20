# Security Policy

## Supported versions

downctl is pre-1.0, and that shapes what a security fix can look like here.

| Version | Security fixes |
|---|---|
| Latest release | **Yes** |
| Previous release | Only when the fix is a one-line backport and the latest release cannot be installed (a broken dependency, a blocked platform) |
| Anything older | No — upgrade |

There is no long-term-support line before 1.0. A fix ships as a new patch
release; the advisory names the version that contains it.

## Reporting a vulnerability

Please **do not** open a public issue for security problems. Use GitHub's
private vulnerability reporting:

https://github.com/ThzxxArt/downctl/security/advisories/new

Include the affected version (`downctl version`), a description, and the
smallest reproduction you have. A test case is worth more than a paragraph: this
project's regression tests are how a fix is shown to work.

### What to expect

| Stage | Target |
|---|---|
| Acknowledgement of the report | 3 working days |
| Initial assessment (severity, affected versions, whether a CVE is warranted) | 7 working days |
| Fix or mitigation for a high-severity issue | 30 days |
| Public advisory | After the fixed release is on PyPI |

These are targets, not a contract, and they assume a single maintainer. If a
deadline matters to you (a disclosure window, a downstream freeze), say so in
the report and it will be worked to.

### Scope

In scope:

- Credential handling — where a token, password or presigned signature is sent,
  logged, or written to disk.
- Path handling — anything that writes outside the destination, or overwrites a
  file it was told to read.
- The state files (`.downctl.*`) — anything that makes a completion claim
  unsound.
- Signature verification (SigV4) and checksum comparison.

Out of scope (still welcome as a normal bug report):

- A malicious *source* server returning wrong bytes; `--checksum`/`--checksums`
  is the answer to that, and the tool says what it verified.
- Running untrusted plugins. Installing a plugin is running its code — see the
  trust model below.
- Reports that require the attacker to already control the user's config file or
  command line.

## Plugin trust model

- Plugins installed as packages (advertised via `downctl.*` entry-points) are
  loaded automatically. Installing a plugin is equivalent to running its code.
- Plugins loaded from local directories execute arbitrary code. Only enable
  directories you trust (`./downctl_plugins`,
  `~/.config/downctl/plugins`), and only with `--local-plugins`. They are never
  loaded implicitly — including by `downctl plugin list`/`info`.
- A hook's `exec` command runs with your privileges. Treat `[plugins.exec]` the
  same way you would treat a shell alias.

## What the tool does not protect against

Being explicit about the boundary is part of the policy:

- A compromised *mirror* can serve different bytes. A `mirror` is a third party;
  the HF token is withheld from it by default for that reason.
- An HTTP (not HTTPS) target can be rewritten in transit.
- A file that was never checksummed and whose server offers no version witness
  cannot be proven correct after the fact. downctl reports that case as
  `unverifiable` rather than claiming success.
