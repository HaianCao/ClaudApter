# Harness compatibility profiles

Translated Chat/Responses routes use `claudapter.harness` to identify the selected
Claude CLI release. Version 2.1.308 retains the existing restrictions on beta
features, structured outputs, prompt caching and dynamic tool search. Thinking
policy remains managed by the launcher/route configuration.

The `--version` query uses an isolated scratch profile, filtered environment and
three-second timeout. Unknown, malformed or failed queries warn and keep the
existing compatibility controls. They do not block native Messages routes or
automatically enable new features. No raw CLI diagnostics are copied into the
version warning.

Version 2.1.308 was checked with help and a synthetic Chat-upstream source-launcher
smoke on 2026-11-03. This is not subagent, real-provider or all-feature acceptance.
## Agent model policy

New providers created in setup use `agent_models = { policy = "selected" }`.
Older configurations without the field and programmatic Providers retain
`inherit`. The Advanced editor and pre-Run summary expose the effective choice:

- `inherit`: leave inherited auxiliary/subagent model settings as before.
- `selected`: pin Haiku/Sonnet/Opus/Fable aliases and the subagent model to the
  exact model chosen at launch, with `CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1`.
  This explicitly overrides custom agent model definitions.
- `custom`: set any of `default`, `haiku`, `sonnet`, `opus`, `fable` to exact
  model IDs. Unspecified roles use the launch model. FORCE is explicitly `0`;
  per-invocation choices and agent definitions retain normal precedence.

Managed model values are applied in the child environment and session
`--settings` env overlay. An inherited FORCE or tier model cannot silently win
over this overlay. Extra user CLI settings and managed organization restrictions
remain outside the wrapper's enforcement. Family aliases are not accepted as
exact IDs in these policies, preventing self-referencing alias definitions.

Non-inherited policies currently require observed Claude Code 2.1.308. Unknown
versions fail the explicit override at preflight; choose inherit to retain the
existing harness behavior. Native routes query the version only when requesting
this additional policy. Model IDs are not rewritten into a fake Claude family.

An agent-only effort override is not exposed: setting the global effort env would
also change the main session. Thinking/effort remain subject to the normal route
policy and native agent configuration. Model capability metadata declared in
user settings is not recertified or inferred from these role mappings.

The selection is fixed at launch and does not follow later `/model` changes.
Actual subagent execution and route evidence for auxiliary models remain separate
gates; config validation does not certify model availability or tool support.

No persistent Claude configuration is written. Existing user CLI overrides are
still user-controlled; the compatibility policy is not an enforcement boundary.
