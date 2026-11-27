# Advanced guide

[English README](../README.md) · [README на русском](../README.ru.md)

## Commands and scope rules

```sh
super-apm install                              # both configured scopes
super-apm install owner/package                # add a shared dependency
super-apm install owner/package --local        # add a local dependency
super-apm install --mcp docs --url https://example.invalid/mcp --local
super-apm update --yes                         # update both scopes
super-apm update example-skill --yes            # route by installed package
super-apm list                                 # unified scope/package/commit table
super-apm audit --ci                           # audit both installations
super-apm status                               # paths and link health
super-apm sync                                 # reconcile local links, MCP and ignore rules
```

Use `--shared` or `--local` to choose one scope. Shared uses the project's existing
APM manifest, lockfile and deployment policy. Local uses `.apm-local/` with its own
manifest, lockfile and downloaded dependencies. The local installation and its
project-facing skill links are excluded through a managed `.gitignore` block.
APM commands continue to work normally on the shared scope.

Tracked files, foreign links and overlapping shared/local skill names are rejected.
Only entries recorded in local ownership receipts may be retired. Updates are not
an atomic transaction across the two installations; a failed later step returns
failure even when an earlier step completed. No Git commits or pushes are automatic.

## Auditing nested worktrees

Native APM 0.32 scans all of `.claude/`, including unrelated application files
inside `.claude/worktrees/`. For explicit Claude/Codex targets, super-apm audits
a private deployment snapshot when registered Git worktrees occur there. The
snapshot retains the full governed client directories, manifests, lock, package
sources, tracked-file index and Git origin used for policy discovery. Only actual
registered nested checkouts are excluded; unrecorded files and arbitrary
`node_modules` elsewhere remain in scope. Audit each nested checkout separately.
No checks, hashes or findings are suppressed. The snapshot goes to Trash afterward.
Local-path dependencies, file-based policy overrides and other targets fail with
an instruction to use native audit rather than silently narrow their scope.
`--file` stays native; `--strip` requires an explicit native invocation.

## Local MCP

Local stdio and HTTP servers are exposed to Claude through `.mcp.json` and to Codex
through `.codex/config.toml`. Native APM renders both client configurations inside
`.apm-local/`; super-apm merges only the MCP entries into the project configuration.
Existing unrelated settings and foreign servers are retained. Name collisions or
external edits to owned entries stop reconciliation rather than overwrite them.

Both project configuration files must be **untracked** when used for local MCP.
super-apm refuses tracked destinations, adds ignore rules and writes runtime files
with owner-only permissions. It never untracks an existing shared configuration.
Local HTTP headers and Codex environment values may be present in these ignored
runtime files. Receipts contain hashes, not their values. Keep credentials out of
shared manifests and tracked configuration.

Claude stdio entries use a small launcher that reads the ignored native definition,
sets the local working directory and executes the server without protocol banners.
It supports Claude's `${VAR}` and `${VAR:-default}` substitution for command, arguments
and environment. Codex uses its native `cwd` setting. HTTP transport/auth fields
remain native to each client. Project trust, MCP approval and OAuth login remain
client responsibilities. Newly installed MCP servers may require a session restart.

## Local hooks

A local package may ship Claude Code and Codex hooks (`hooks/hooks.json` or per-client
`hooks/claude-hooks.json` and `hooks/codex-hooks.json`). Native APM deploys them inside
`.apm-local/` as if it were a separate project, so super-apm re-points every handler at
the `.apm-local/` copy of its script and merges the groups into each client's ignored
project settings:

- Claude Code: `.claude/settings.local.json`. Handlers keep `${CLAUDE_PROJECT_DIR}` and
  point at `.apm-local/.claude/hooks/`.
- Codex: inline `[hooks]` tables in `.codex/config.toml`. Codex runs hook commands from
  the session directory, which may be a subdirectory, so paths are anchored at
  `$(git rev-parse --show-toplevel)/.apm-local/.codex/hooks/`.

Prompt hooks and system commands pass through unchanged. Unrelated settings and
foreign hook groups are retained; the receipt `.apm-local/super-apm-hooks.json` holds
hashes of the groups super-apm added, so a changed or removed package replaces only
its own groups. A settings file that super-apm created and that ends up empty goes to
Trash. Both destinations must be **untracked**: super-apm refuses tracked files, adds
ignore rules and writes them with owner-only permissions. Codex may ask to approve new
hooks, and both clients read hooks at session start.

## Upstream snapshots

An upstream snapshot is a tracked folder, for example `.apm/skills/<name>`, that every
collaborator uses from Git and only its owner refreshes from a private source:

```sh
super-apm upstream add PATH --source SRC --subpath DIR [--ref REF]
super-apm upstream sync [--dry-run]
super-apm upstream list
super-apm upstream remove PATH
```

`SRC` is a local Git checkout or a Git URL; a URL is kept as a private bare mirror in
`.git/super-apm/upstream-cache/` and fetched on every sync. `REF` defaults to `HEAD`. Only
the folder as committed at `REF` is copied, so uncommitted work in the source never
reaches the project. Symlinks and submodules inside the folder are rejected.

The configuration (`upstreams.yml`) lives in `.git/super-apm/`, the Git common directory,
so every worktree of the repository refreshes the same snapshots; a worktree's own
`.apm-local/upstreams.yml` from an earlier version is merged there on the next run and
kept as `upstreams.yml.migrated`. The last copied file hashes
(`.apm-local/upstreams-state.json`) stay per worktree, because each checkout may hold
another version of the snapshot. A worktree that has never synced adopts the snapshot
as its branch committed it; uncommitted edits there are kept, and a branch without the
snapshot folder is skipped rather than given one. Nothing
in tracked files names the source, and messages never print its location or Git
output. `add` adopts an existing folder as the last synced version, so the next sync
may replace it. A snapshot changed in the project since the last sync is reported and
left alone; merge such edits into the upstream. Files that disappeared upstream go to
Trash. An interrupted copy is resumed on the next sync instead of being mistaken for
local edits.

With session hooks installed, the background worker refreshes upstream snapshots before
the shared install, so APM deploys the new version in the same run. Commit and push the
snapshot like any other change. `--pre-shared` remains for project-specific steps.

## Hidden dependencies

A package declares hidden dependencies under `super-apm.hidden` in its own `apm.yml`:

- `dependencies` (required, non-empty): a string in any APM reference format;
  `{git: URL, path: SUB, ref: REF}`, passed to APM as written; `{path: ./dir}` for a local
  folder; or `{package: REF, as: NAME}` / `{path: ..., as: NAME}` to choose the output folder.
- `destination`: a folder inside `.apm-hidden/<package>/`; empty by default.
- `rename`: a file-name mapping applied at every depth; `{SKILL.md: METHOD.md}` by default.
- `exclude`: file or folder patterns, relative to each dependency, that are not copied
  (`*.mp4`, `dist`, `.github`); shell wildcards match one path segment or the whole path.

`<package>` is the installed package name, the same as its skill folder. Besides packages in the
lockfile, the project's own skills in `.apm/skills/<name>/` may declare hidden dependencies; local
`path:` entries then resolve inside that folder. An entry lands in
`.apm-hidden/<package>/<destination>/<as or skill name>/`. Unknown keys, unsafe names, `..` in
`destination` or `as`, overlapping output folders, renames that collide and any file still
named `SKILL.md` (in any letter case) stop reconciliation before files change.

Remote entries go into a generated manifest in `.apm-hidden/.super-apm/<package>/` and are
resolved with `apm lock`, which downloads and pins them but deploys nothing. `install` reuses
that lockfile and calls APM only when the declaration changed or a download is missing;
`update` runs `apm lock --update`, and `update NAME` refreshes only the named packages. With
`--shared-policy pinned`, hidden dependencies of shared packages keep their pins as well.
Only direct entries are exposed; their own dependencies are downloaded but not copied.
`super-apm hidden [--shared|--local]` reconciles on demand.

Global packages work the same way. After `super-apm install -g` or `update -g` succeeds,
hidden dependencies of packages in `~/.apm/apm.yml` land in `~/.apm/.apm-hidden/<package>/`;
`super-apm hidden -g` reconciles them on demand, and the session worker does so after its
daily global update (`--global-updates`). No `.gitignore` is written there.

Local entries are copied straight from the package, so they never lag behind it. For a package
installed with `path:`, they resolve from its source folder and may name siblings (`../other`),
like APM's own local paths. A remote package may only reference folders inside itself. Those
folders also deploy with the package, so keep vendored `SKILL.md` files out of published
packages. Symlinks inside dependencies are skipped and reported.

Ownership works like local links. `.apm-hidden/.super-apm/state.json` records the hash of
every file written. A file changed or added by hand stops reconciliation for that package and
stays untouched, also when the package is removed; move it out and run `super-apm install`.
A dependency dropped from the list goes to Trash as one folder; a removed package takes its
output and workspace to Trash. An interrupted copy resumes on the next run. `.apm-hidden/`
must stay untracked and joins the managed `.gitignore` block when first created. A package may
not declare hidden dependencies in both scopes. `apm audit` does not cover them.

## Session hooks

```sh
super-apm hooks install --shared-policy daily
super-apm hooks install --shared-policy pinned
# Optional: preserve a project's preparation script and global-update policy
super-apm hooks install --shared-policy pinned --pre-shared scripts/prepare.py
super-apm hooks install --shared-policy daily --global-updates
```

This replaces old `apm-hooks` or super-apm handlers in `.claude/settings.json` and
`.codex/hooks.json`, preserving unrelated handlers and settings. Changed originals
are backed up under ignored `.apm-local/hook-backups/`. Reinstalling is idempotent.
A dependency-free gate is vendored at `.super-apm/bootstrap.py`; commit it with
the hook configurations. It runs before super-apm is installed and offers an
explicit install/skip choice for either missing tool. Installation instructions
pin the wrapper release. No packages are downloaded until the user approves.
The normal gate needs Python 3.8+ (standard library only); without Python, the
launcher provides the same setup/skip instruction to the agent instead of a
structured blocking gate. An installed wrapper does not need a system Python.
The launcher also searches the standard uv and Homebrew executable directories.
Approve the changed hooks in Codex and restart the client session to activate them.

`SessionStart` launches a quiet background worker and returns immediately.
With `daily`, shared packages update once per 24 hours; with `pinned`, shared
packages run ordinary `apm install` and retain their existing lockfile policy.
**Local packages update once per 24 hours in either mode.** Other sessions install
from the current locks. Only successful refreshes advance the timestamps; failures
retry at the next session. A shared failure does not prevent the local attempt.

A project lock serializes refreshes and interactive mutations. Duplicate session
starts do not launch overlapping workers. Global updates are opt-in and use a
separate cross-project lock. Status, timestamps and a quiet log live in
`.apm-local/`; `super-apm status` reports the last outcome. A failed refresh also
produces one notification at the next session start. The prompt hook handles either missing tool with an explicit install/skip choice.

A background update can finish after the agent has discovered tools. Restart the
session if it does not see newly added skills or MCP servers yet. Windows behavior
is implemented, but native end-to-end validation has been performed on macOS.

## Boundaries and recovery

Local skills, MCP and hooks for Claude/Codex are supported. Local LSP, executable exports
and arbitrary client configuration merging are not. Global `-g` calls pass through
to APM, followed by hidden dependency reconciliation. A custom `--root` is rejected because it bypasses scope isolation.
`install/update --dry-run` do not modify project files. Native uninstall lifecycle
behavior is preserved, including pre-uninstall scripts during an upstream dry-run.

Before a local uninstall, the previous local installation is preserved in system
Trash. Stale owned links and empty owned MCP and hook files also go to Trash. On macOS the
system Trash is used; without an available Trash implementation, retirement fails
instead of permanently deleting files. Trash is never emptied automatically.

A legacy `.git/apm-personal` installation can be moved to `.apm-local` using
`super-apm migrate-local`; existing local data and verified link targets are retained.
If reconciliation stops, repair the conflicting file or restore the ignored backup,
then run `super-apm sync`. Do not edit ownership receipts to claim foreign entries.

## Development

```sh
python -m pip install -e .
python -m unittest discover -s tests -v
```

Tests use synthetic MCP servers, fake APM executables and temporary Git projects.
They do not require private repositories, credentials or machine-specific
configuration. No secrets, project manifests, installed package caches or runtime
configuration belong here.

## License

MIT. Microsoft APM remains a separate MIT-licensed dependency.
