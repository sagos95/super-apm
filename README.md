# super-apm

One command for shared and Git-ignored local [Microsoft APM](https://github.com/microsoft/apm) dependencies.
APM downloads packages, resolves dependencies, maintains lockfiles and performs its own audits.
super-apm coordinates two ordinary installations without importing APM internals.

## Install

Requires Python 3.11+, Git, and APM on PATH. Tested with APM 0.32.0.

```sh
uv tool install 'git+https://github.com/sagos95/super-apm.git'
super-apm --help
```

For development, clone this repository and run `uv tool install --editable .`.

## Use

```sh
super-apm install                              # both configured scopes
super-apm install owner/package                # add a shared dependency
super-apm install owner/package --local        # add a local dependency
super-apm update --yes                         # update both scopes
super-apm update example-skill --yes            # route by installed package
super-apm list                                 # unified scope/package/commit table
super-apm audit --ci                           # audit both installations
super-apm status                               # paths and link health
super-apm sync                                 # reconcile local links and ignore rules
```

Use `--shared` or `--local` to choose one scope. Shared uses the project's existing
APM manifest, lockfile and deployment policy. Local uses `.apm-local/` with its own
manifest, lockfile and downloaded dependencies. The local installation and its
project-facing skill links are excluded through a managed `.gitignore` block.
APM commands continue to work normally on the shared scope.

Tracked files, foreign links and overlapping shared/local skill names are rejected.
Only links recorded in the local ownership receipt may be retired. Updates are not
an atomic transaction across the two installations; a failed later step returns
failure even when an earlier step completed. No Git commits or pushes are automatic.

## Current scope

Version 0.1 supports local skills for Claude and Codex. Local MCP/LSP/config merging
and hooks are not yet supported. Global `-g` calls pass through to APM. A custom
`--root` is rejected because it would bypass scope isolation. `install/update
--dry-run` do not modify project files. Native uninstall lifecycle behavior is
preserved, including pre-uninstall scripts during an upstream dry-run.

Before a local uninstall, the previous local installation is preserved in system
Trash. Stale owned links also go to Trash. On macOS the system Trash is used;
without an available Trash implementation, retirement fails instead of permanently
deleting files. Trash is never emptied automatically.

A legacy `.git/apm-personal` installation can be moved to `.apm-local` using
`super-apm migrate-local`; existing local data and verified link targets are retained.

## Development

```sh
python -m pip install -e .
python -m unittest discover -s tests -v
```

Tests use a fake APM executable and temporary Git projects. They do not require
private repositories, credentials or machine-specific configuration. No secrets,
project manifests, installed package caches or runtime configuration belong here.

## License

MIT. Microsoft APM remains a separate MIT-licensed dependency.
