# Windows installation transactions

The installer prepares a fresh environment under the dedicated adapter root:

```text
bin/<command>.exe
bin/launcher.json                   # launcher format and integrity record
current.txt                         # active 32-character generation ID
environments/<generation>/Scripts/  # immutable after publication
install.lock
venv/                              # retained if upgrading a legacy installation
```

CodexApter uses `%LOCALAPPDATA%/Programs/codex-ca`; ClaudApter uses
`%LOCALAPPDATA%/Programs/claudapter`. The installed command is `codex-ca` or
`claude-ca`. PATH points to `bin`, not to a particular generation.

## Preparation and publication

1. Acquire the exclusive installation lock. Concurrent installers fail before
   changing an environment. The operating system releases the lock if its owner
   is killed.
2. Resolve the active interpreter, or the legacy `venv` interpreter, to preserve
   the opt-in gateway component. Invalid/missing active pointers are reported;
   they are not silently overwritten.
3. Create a new venv at its **final** unique path. Windows pip console entry points
   contain absolute paths, so staging a venv and renaming it would be incorrect.
4. Install the selected package and gateway wheel, run `pip check`, run the actual
   package's `.cli --help`, and check gateway import when requested. Existing
   environments are never passed to pip for modification.
5. Prepare the stable EXE launcher using pip's bundled Windows launcher builder.
   Its stdlib dispatcher validates the generation ID and starts the generation's
   isolated Python with `-m <package>.cli`, preserves argv (including empty,
   quoted, Unicode and shell-special values) and returns the child's exit code.
   It does not open setup. The launcher/manifest directory is published together;
   later installs verify its format/hash and leave it unchanged. Its bootstrap
   interpreter is the first successfully prepared environment, which must remain
   present even when another generation becomes active.
6. Flush a temporary pointer file, then publish with same-volume `File.Replace`
   (or `File.Move` for the first installation). Never delete the old pointer
   before replacement. Only after publication does the installer advertise `bin`
   on PATH. Existing launchers are not rewritten on every update.

Failed pip/health checks or a failed pointer replacement leave the old pointer
and interpreter intact. Killing preparation leaves an unpublished directory;
rerunning the installer builds a new generation. A process already using the
previous interpreter can keep using its files after a successful update.

Old, failed and interrupted generations are deliberately retained until wrapper
uninstall. This costs disk space but avoids deleting files still used by another
session. The uninstaller removes the dedicated root and both new/legacy wrapper
PATH entries; normal provider-data retention rules are unchanged.

## Deliberate rollback

There is no automatic downgrade or new end-user rollback flag. For maintenance,
record the old `current.txt` value before an update. Verify that the recorded
generation's Python still passes `-I -m <package>.cli --help`, dot-source
`scripts/install-environment.ps1`, acquire `Open-AdapterInstallLock ROOT`, and
call `Set-AdapterCurrentEnvironment ROOT OLD_GENERATION` under that lock. Dispose
the lock in `finally`. This replaces only the pointer; it does not restore config
or sessions. A legacy venv is retained but is not a generation ID.

## Validation and limits

`tests/test_install_environment.py` uses real venv/pip with synthetic offline
wheels and temporary profiles. It checks failed package/entry-point preparation,
blocked pointer replacement, concurrency, killing a lock holder/candidate,
retry, deliberate pointer rollback, legacy preservation, exact executable arguments
and exit codes. It never installs into the real user's directories or calls a
native coding CLI. `tests/test_uninstall.py` mocks environment writes and covers
both wrapper PATH layouts.

The ecosystem's `scripts/smoke_windows_install.py` separately exercises the full
local installers, update and uninstall with a matching local gateway wheel,
`-NoPathChanges`, temporary profiles and skipped native CLI installation.
For unpublished previews, pass the matching `-ProtovertaWheel` on each local
installer run. Without that argument, gateway preservation resolves the pinned
release URL; an unavailable release fails preparation and preserves the old
generation rather than silently dropping gateway support.

The transaction covers wrapper package publication, not registry/PATH writes,
provider-data migration, Python/official CLI installation, concurrent uninstall,
filesystem hardware failures or power loss. Close other installers/uninstallers
before maintenance. No live process or full-machine rollback is promised. The
file replacement must be supported by the installation filesystem; failures are
reported without deleting the active pointer. Release artifacts still need
separate verification before publishing a preview that references them.
