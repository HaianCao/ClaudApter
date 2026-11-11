# Changelog

All notable changes to ClaudApter are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- Linux and macOS support: `scripts/install.sh` / `scripts/uninstall.sh` install
  claude-ca for the current user (no sudo) with the same pinned, hash-verified
  Protoverta wheel as Windows, and keep the previous environment on update.
  Settings follow `$XDG_CONFIG_HOME` / `$XDG_CACHE_HOME`. CI runs the tests and
  the install lifecycle on Ubuntu and macOS.

### Fixed
- A provider that accepts only the official Claude Code (AgentRouter) is now saved with
  the Anthropic Messages API and Direct mode whatever API was chosen; before, choosing another
  API left it on the local gateway and every request failed with
  `401 unauthorized client detected`.
- Setup no longer mistakes a provider's website for its API (an address
  missing `/v1`): it says so instead of reporting the key as accepted.

## [0.3.0] - 2026-11-11

Requires Protoverta 0.4.0; the installer pins and verifies its wheel.

### Changed
- Gateway model discovery skips catalog entries the provider reports with
  invalid data instead of failing, and says how many it skipped. Malformed
  token limits are dropped rather than retried without limits.
- Error messages and setup hints no longer mention a "preview" core.
- README: setup page screenshot and a connection diagram.
- Base URL rules and the loopback proxy bypass live in one module shared
  verbatim with the other launcher (checked for drift in CI). Invalid base
  URLs now get a message naming the exact problem.
- Type-checked with mypy in CI; the deadline transport keeps its own TLS
  context instead of reading a private `urllib` attribute.

### Removed
- Fallbacks for Protoverta builds older than the pinned version.

## [0.2.0] - 2026-11-10

Merges the gateway preview into `main`.

### Added
- Setup needs only the base URL, the API key and the API the provider speaks.
  The name and ID come from the address, authentication is detected (Bearer,
  or `x-api-key` when the provider needs it) and the key is checked without
  generating anything. Rejected keys, missing endpoints, providers without a
  model list and providers that accept only official CLIs are explained.
- Running `claude-ca` with no provider opens setup.
- Automatic retry by error class (rate limit, overloaded, server, network,
  stream interrupted, backend mismatch, unknown), configurable in `[retry]`:
  Protoverta retries in gateway mode, a local relay in direct mode.
  Deterministic errors (rejected key, no credit, unknown model, context too
  long, content policy, invalid request) are shown at once. Decisions are
  logged to a bounded `retry.log`.
- AgentRouter preset (direct, official-client only).
- Troubleshooting and "How ClaudApter connects" sections in the README.

### Changed
- The local Protoverta gateway (0.3.0) is installed with every install and
  is the default for new providers; providers saved by 0.1 keep their mode.
  The installer verifies the gateway wheel's SHA-256 before installing it.
  `-WithGateway` is still accepted and changes nothing.
- Provider errors on model refresh are shown as one actionable sentence.
- The CLI version probe waits up to 15 s (a cold first start of the native CLI
  could exceed 3 s and silently disable version-gated features).

- With the inherit agent policy, global `ANTHROPIC_DEFAULT_*_MODEL`,
  `ANTHROPIC_SMALL_FAST_MODEL` and `CLAUDE_CODE_SUBAGENT_MODEL` values are kept and
  a notice explains that background tasks and subagents use them.

### Fixed
- Chat Completions and Responses providers failed with "only low, medium, and
  high effort are portable": with the effort left at *Model default*, Claude
  Code 2.1.319 sends `xhigh`. Translated sessions now set Claude Code's
  `maxEffortLevel` to `high`, which Claude Code applies to every effort source.

### Security
- The setup page cannot be framed (`frame-ancestors 'none'`, `X-Frame-Options: DENY`).

## Gateway preview (0.2.0a1, 0.2.0a2)

### Added
- Setup page presets for tested providers (OpenRouter, NVIDIA NIM and TokenHarbor): the address,
  protocol and connection mode are filled in, so a new user only pastes an API
  key. The provider ID is derived from the display name.
- Model limits are taken automatically from the selected model's maximums when
  the provider reports them in its model list (context window and maximum output tokens (output capped at half the window)); values set on the
  provider still win. The setup page shows the limits a session will use.
- Optional local Protoverta gateway: Chat Completions and Responses providers, pinned to Protoverta 0.3.0a11.
- Automatic reasoning policy that works with providers whose reasoning cannot
  be turned off (reasoning a route cannot carry is dropped, never invented).
- Model limits in setup (context window and maximum output tokens).
- Session-scoped auxiliary and child-model policies, verified subagent subset,
  route checks (`claude-ca doctor`), session diagnostics and capability registries.
- README badges, Protoverta links, package metadata, contributor guide and
  issue templates.

### Changed
- Runs newer Claude Code releases with verified settings and a warning instead of
  failing; verified on Claude Code 2.1.308, 2.1.318 and 2.1.319.
- Ctrl+C in non-interactive runs is left to Claude Code, so it can shut down
  cleanly.
- CI tests Python 3.11, 3.13 and 3.14; GitHub Actions use Node 24 releases.

### Fixed
- The setup page script is syntax-checked in tests (a syntax error disables the
  whole page).
- Saved configurations containing a DEL character (U+007F) could no longer be
  loaded.
- Provider model catalogs nested deeper than 64 levels are rejected on every
  Python version (Python 3.14 no longer raised RecursionError for them).
- Setup and discovery boundaries: invalid API prefixes, stale cache writes,
  deeply nested setup requests, credential collisions and proxy handling for
  the local gateway.
- `CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK=1` is set for sessions, so a flagged request
  is not switched to a built-in Claude model the provider does not serve.

## Stable launcher (`main`)

### 0.1.1 - 2026-11-09
- Failed provider requests are retried through a local relay (up to 10 times,
  60 s apart, configurable in `[retry]`).

See the [commit history of `main`](https://github.com/HaianCao/ClaudApter/commits/main).
