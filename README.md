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
- **Upstream snapshots:** a folder that the whole project uses from Git, refreshed only on your machine from a private source. Where the source lives stays in the repository's Git directory, so collaborators see the folder but not where it comes from.
- **Hidden dependencies:** skills that a package needs but no agent may discover, kept in the Git-ignored `.apm-hidden/` under a different file name.

## Install

Requires Python 3.11+, Git, uv, and APM on your PATH. Tested with APM 0.32.0.

```sh
uv tool install 'git+https://github.com/sagos95/super-apm.git@v0.9.0'
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

`--source` takes a local Git checkout or a Git URL. Only committed changes are copied. Commit the snapshot folder as usual; the source path and URL stay in `.git/super-apm/`, shared by every worktree of the repository and never tracked, and each worktree keeps its sync state in the Git-ignored `.apm-local/`. With session hooks installed, your snapshots refresh before each background install. A snapshot edited in the project is never overwritten.

## Hidden dependencies for router skills

A router skill chooses one of many methods and reads only that one. Installed as skills, the methods would add dozens of descriptions to the agent's skill list, which Claude Code and Codex cap at 1–2% of the context window. Instead, the router declares them in its own `apm.yml`:

```yaml
name: ux-router
version: 1.0.0
super-apm:
  hidden:
    destination: references/methods            # folder inside .apm-hidden/ux-router/
    rename: {SKILL.md: METHOD.md}              # the default; no file may stay SKILL.md
    dependencies:
      - emilkowalski/skills/skills/animate#64eb3de
      - {package: owner/repo/plugins/ux-strategy/skills/frame-problem, as: ux-strategy/frame-problem}
      - path: ../ux-check                      # relative to the package source
```

`super-apm install` places every method at `.apm-hidden/ux-router/references/methods/<name>/METHOD.md`; the rest of its folder stays as published. `update` and the daily session hooks refresh them together with ordinary packages, `list` shows their commits, `status` checks them, and uninstalling the router moves them to Trash. Entries use the usual APM formats, including private hosts and pinned `#ref`s; `as` picks the folder name, which defaults to the skill's own.

The router finds its methods by walking up from its own folder; a global install (`super-apm install -g`) keeps them in `~/.apm/.apm-hidden/`:

```python
from pathlib import Path

def hidden_dir(package: str) -> Path | None:
    for folder in Path(__file__).resolve().parents:
        if (folder / ".apm-hidden" / package).is_dir():
            return folder / ".apm-hidden" / package
    global_dir = Path.home() / ".apm" / ".apm-hidden" / package  # super-apm install -g
    return global_dir if global_dir.is_dir() else None
```

Why it works this way:

- **The list lives in the package's `apm.yml`.** APM 0.32 ignores the `super-apm` key, so the package keeps a single manifest and still installs with plain APM. The list is deliberately outside `dependencies.apm`, because APM would install those as visible skills.
- **The files go to `.apm-hidden/`, not into the installed package.** APM rewrites `.claude/skills/<package>` on every `apm install` and deletes any file it did not put there.
- **APM still downloads them.** super-apm runs `apm lock` in a private workspace: it resolves, downloads and pins packages without deploying anything into agent folders. The lockfile stays in `.apm-hidden/.super-apm/<package>/`.
- **Nothing from the package runs.** super-apm only downloads, copies and renames files.

Without super-apm, the router installs without its methods. The lock is per machine, so pin `#ref`s when every machine must get identical versions. [The advanced guide](docs/advanced.md#hidden-dependencies) lists the remaining rules.

## Automatic updates

```sh
super-apm hooks install --shared-policy daily
```

At the start of an agent session, packages and their hidden dependencies refresh in the background once per day. Use `--shared-policy pinned` to keep shared versions fixed; local packages still update daily.

Approve the changed hooks in Codex and restart your agent session.

## More

[Advanced guide](docs/advanced.md): MCP and hook setup, additional commands, recovery, and development. Local MCP and hooks require Git-ignored project configuration files.

To upgrade super-apm itself, repeat the install command with `--force` and the desired version tag.

[MIT license](LICENSE).
