# Changelog

All notable changes to ClaudApter are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

The `main` branch is the stable launcher that connects Claude Code directly to
Anthropic Messages providers. The `integration/protoverta` branch is the gateway preview
**0.2.0a2**, which adds the optional [Protoverta](https://github.com/HaianCao/Protoverta)
gateway; it will be merged into `main` once it has been fully tested.

## [Unreleased] - gateway preview 0.2.0a2 (`integration/protoverta`)

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

See the [commit history of `main`](https://github.com/HaianCao/ClaudApter/commits/main).
