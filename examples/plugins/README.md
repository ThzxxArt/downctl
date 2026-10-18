# Plugin examples

`example_source.py` is a minimal plugin that handles `example://` targets.

## Try it locally

```bash
mkdir -p downctl_plugins
cp examples/plugins/example_source.py downctl_plugins/
downctl plugin list --local-plugins   # should include 'example' under sources
```

## Package it

A distributable plugin is a normal Python package that advertises an
entry-point in one of the `downctl.*` groups:

```toml
[project.entry-points."downctl.sources"]
example = "my_package.source:ExampleSource"
```

Groups: `downctl.sources`, `downctl.transports`, `downctl.verifiers`,
`downctl.reporters`, `downctl.routers`, `downctl.hooks`.

> The `downctl.sinks` group existed up to 0.1.3 and was retired in 0.1.4: the
> engine always wrote through `core/transfer.py`, so a sink plugin never took
> effect. A package that still advertises it gets a warning at startup instead
> of being ignored silently.

Install it (`pip install .`) and `downctl` picks it up automatically.

> **Security:** plugins run arbitrary code. Only install or enable plugins you
> trust.
