# super-apm

[![English](https://img.shields.io/badge/English-0969DA?style=flat&logo=googletranslate&logoColor=white)](README.md)
[![Русский](https://img.shields.io/badge/%D0%A0%D1%83%D1%81%D1%81%D0%BA%D0%B8%D0%B9-57606A?style=flat&logo=googletranslate&logoColor=white)](README.ru.md)

A wrapper for [Microsoft APM](https://github.com/microsoft/apm) to manage shared and local skills, MCP servers and hooks in Claude Code and Codex.

## Why use super-apm?

Have a personal skill, MCP server or hook you'd like to use in an APM (Agent Package Manager) project, but don't want it committed to Git or installed for everyone else on the project? Then Super-apm is for you!

Normally, adding an AI tool to a project's APM dependencies means it will be installed automatically for everyone working on that project.

Super-apm instead installs the tool (a skill, MCP server or hook) just for you. Your agent can use it in the project, while the tool's files and your personal settings stay out of Git.

Super-apm lets you install and update both team tools and your own additions with a single command.

- **Shared:** the package list in `apm.yml`, stored in Git with your project.
- **Local:** packages in `.apm-local/`, automatically excluded from Git along with their generated links and configuration.
- **Upstream snapshots:** a folder that the whole project uses from Git, refreshed only on your machine from a private source. Where the source lives stays in `.apm-local/`, so collaborators see the folder but not where it comes from.

## Install

Requires Python 3.11+, Git, uv, and APM on your PATH. Tested with APM 0.32.0.

```sh
uv tool install 'git+https://github.com/sagos95/super-apm.git@v0.5.0'
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

## Share a tool from a private source

Bring a skill you maintain elsewhere into the project, so every collaborator gets it from Git, while only you can refresh it:

```sh
super-apm upstream add .apm/skills/my-skill --source ~/path/to/private-repo --subpath skills/my-skill
super-apm upstream sync    # copy the source's committed version into the snapshot
```

`--source` takes a local Git checkout or a Git URL. Only committed changes are copied. Commit the snapshot folder as usual; the source path, URL and sync state stay in the Git-ignored `.apm-local/`. With session hooks installed, your snapshots refresh before each background install. A snapshot edited in the project is never overwritten.

## Automatic updates

```sh
super-apm hooks install --shared-policy daily
```

At the start of an agent session, packages refresh in the background once per day. Use `--shared-policy pinned` to keep shared versions fixed; local packages still update daily.

Approve the changed hooks in Codex and restart your agent session.

## More

[Advanced guide](docs/advanced.md): MCP and hook setup, additional commands, recovery, and development. Local MCP and hooks require Git-ignored project configuration files.

To upgrade super-apm itself, repeat the install command with `--force` and the desired version tag.

[MIT license](LICENSE).
