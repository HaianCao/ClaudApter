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
Auxiliary-model environment policy is a separate pending item: the version
profile alone does not force every agent to use the selected main model.

No persistent Claude configuration is written. Existing user CLI overrides are
still user-controlled; the compatibility policy is not an enforcement boundary.
