# Security Policy

## Supported versions

downctl is pre-1.0; only the latest release receives security fixes.

## Reporting a vulnerability

Please **do not** open a public issue for security problems. Instead, use
GitHub's private vulnerability reporting:

https://github.com/ThzxxArt/downctl/security/advisories/new

Include a description, reproduction steps and the affected version. We aim to
acknowledge reports within a few days.

## Plugin trust model

- Plugins installed as packages (advertised via `downctl.*` entry-points) are
  loaded automatically. Installing a plugin is equivalent to running its code.
- Plugins loaded from local directories execute arbitrary code. Only enable
  directories you trust (`./downctl_plugins`,
  `~/.config/downctl/plugins`).
