# super-apm

**English** · [Русский](README.ru.md)

A wrapper for [Microsoft APM](https://github.com/microsoft/apm) to manage shared and local skills and MCP servers in Claude Code and Codex.

- **Shared:** the package list in `apm.yml`, stored in Git with your project.
- **Local:** packages in `.apm-local/`, automatically excluded from Git along with their generated links and configuration.

## Install

Requires Python 3.11+, Git, uv, and APM on your PATH. Tested with APM 0.32.0.

```sh
uv tool install 'git+https://github.com/sagos95/super-apm.git@v0.2.1'
```

## Use

Run these from your project:

```sh
super-apm install owner/package          # add a shared package
super-apm install owner/package --local  # add a local package
super-apm install                        # install both scopes
super-apm update --yes                   # update both scopes
super-apm list                           # see all packages
super-apm status                         # check links and background refresh
```

Add `--shared` or `--local` to work with just one scope. No Git commits or pushes happen automatically.

## Automatic updates

```sh
super-apm hooks install --shared-policy daily
```

At the start of an agent session, packages refresh in the background once per day. Use `--shared-policy pinned` to keep shared versions fixed; local packages still update daily.

Approve the changed hooks in Codex and restart your agent session.

## More

[Advanced guide](docs/advanced.md): MCP setup, additional commands, recovery, and development. Local MCP requires Git-ignored project configuration files.

To upgrade super-apm itself, repeat the install command with `--force` and the desired version tag.

[MIT license](LICENSE).
