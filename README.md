<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/ClaudApter_black.png">
    <img src="assets/ClaudApter_white.png" width="224" alt="ClaudApter logo">
  </picture>
  <p><strong>A clean, focused way to connect Claude Code to the model provider you choose.</strong></p>
  <p>Manage providers, find models, choose an effort level, and launch Claude Code from one local setup studio.</p>
  <p>
    <a href="LICENSE"><img alt="MIT License" src="https://img.shields.io/badge/license-MIT-b5653a?style=flat-square"></a>
    <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-3776ab?style=flat-square&logo=python&logoColor=white">
    <img alt="Windows" src="https://img.shields.io/badge/platform-Windows-0078d4?style=flat-square&logo=windows&logoColor=white">
    <img alt="pytest" src="https://img.shields.io/badge/tests-pytest-a54c70?style=flat-square">
  </p>
  <p><a href="#quick-start">Quick start</a> &middot; <a href="#configure-a-provider">Providers</a> &middot; <a href="#use-claudapter">CLI</a> &middot; <a href="#privacy-and-security">Security</a></p>
</div>

---

ClaudApter is a lightweight launcher and configuration studio for the official Claude Code CLI. It lets you connect compatible API gateways, choose a model and effort level, and start Claude Code with those settings without replacing or modifying Claude Code itself.

## What you can do

- Save and switch between multiple providers in a local browser-based setup page.
- Search a provider's model catalog as you type, or enter a model ID manually.
- Choose a default provider, model, and effort level for future sessions.
- Add, edit, and remove provider connections, and switch between light and dark themes.
- Launch Claude Code in the current terminal, or select a different provider/model for one run.
- Keep API keys local and separate from the provider configuration file.

Connect native Anthropic Messages providers directly, or translate Chat Completions and Responses providers through the optional local Protoverta gateway. Model discovery is optional; you can enter model IDs yourself when a provider does not offer it. A catalog entry does not guarantee Claude Code compatibility; [Anthropic does not support routing Claude Code to non-Claude models through third-party gateways](https://code.claude.com/docs/en/llm-gateway).

## Quick start

### Windows

In PowerShell, run the installer directly from GitHub. No repository clone is needed:

```powershell
& ([scriptblock]::Create((irm "https://raw.githubusercontent.com/HaianCao/ClaudApter/main/scripts/install.ps1")))
```

The installer sets up ClaudApter for your Windows account and can install the official Claude Code CLI if it is missing. It does not require administrator access. When installation finishes, open a new terminal and start setup:

```powershell
claude-ca setup
```

The setup page opens in your browser. Add a provider, enter its API key, refresh the model list or type a model ID, then choose your default model and effort level. Setup is opened only when you run `claude-ca setup`.

Windows supports native CLI executables and standard npm global or project installations. Custom batch-wrapper behavior is not supported; use an official CLI installation if the launcher cannot resolve it safely.

## Configure a provider

In the setup page:

1. Select **Add**, enter a provider ID and display name, and choose the upstream protocol. For Messages, enter `https://gateway.example.com/v1` or the URL without `/v1`; the suffix is joined exactly once. For Chat or Responses, enter the exact API prefix, such as `https://gateway.example.com/v1` or `/api/v4`, without the endpoint suffix; gateway mode is required.
2. Choose **Bearer token** (recommended for gateways) for `Authorization: Bearer ...`. Choose **API key** for `x-api-key` only if your provider requires it, then enter the key without a `Bearer ` prefix.
3. Select **Refresh models** to query the provider's model catalog, or type a model ID directly.
4. Choose an effort level and select **Save as default** or **Run Claude Code**.

When you choose **Run Claude Code**, the setup page stops and Claude Code takes over the terminal where you launched `claude-ca setup`. Direct mode passes the provider key through the child environment; gateway mode keeps it in the local gateway and gives Claude Code a temporary local credential.

**Run Claude Code** uses the current selection for this session. Choose **Save as default** explicitly to change wrapper defaults. On Claude Code 2.1.308 or newer 2.1.x gateway sessions (verified on 2.1.308 and 2.1.318), `/model` lists the selected main and configured auxiliary models without renaming their IDs. Press **s** in the native picker to apply a model only to this session; **Enter** or `/model MODEL_ID` follows Claude Code's explicit save-default behavior. Discovery is not capability certification, and manual model IDs remain available.

### Optional local gateway

Reasoning compatibility is automatic for new providers. Native Messages keeps
the provider's reasoning behavior. On translated Chat/Responses connections the
model keeps its default reasoning and the gateway explicitly omits reasoning
output that Claude Code's Messages protocol cannot represent (it is never shown
as answer text, given a fake signature or sent elsewhere). Many current models
reason on every turn and reject or ignore a "reasoning off" request, so this is
what keeps them usable. Choose a manual **disabled** policy under **Advanced:
reasoning settings** to request less reasoning compute where a model honours it.

The launcher sets `CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK=1` for the session
(unless you set it), so Claude Code does not switch a flagged request to a
built-in Claude model the provider does not serve. For non-Anthropic endpoints
(and every gateway session) it also sets `CLAUDE_CODE_AUTO_MODE_SERVER=0`
unless you set it: from Claude Code 2.1.319 auto mode otherwise asks or denies
every call, because its server-side review cannot run behind a third-party
endpoint; `0` keeps the built-in classifier that earlier versions used. **Model limits** on a
provider set Claude Code's assumed context window and maximum output tokens
(`CLAUDE_CODE_MAX_CONTEXT_TOKENS`, `CLAUDE_CODE_MAX_OUTPUT_TOKENS`) for the
session; set them to the provider model's real limits.

Tested providers (OpenRouter, NVIDIA NIM, TokenHarbor, AgentRouter), their known limits and recommended settings: [provider compatibility](https://github.com/HaianCao/Protoverta/blob/main/docs/compatibility/providers.md).

This checkout prepares gateway preview **0.2.0a2** with Protoverta **0.3.0a10**.
Until those snapshots are published, use the local installer with
`-ProtovertaWheel PATH_TO_A8_WHEEL`; the public a6 wheel lacks the new runtime
options. Once the matching preview/core are published, install with:

```powershell
& ([scriptblock]::Create((irm "https://raw.githubusercontent.com/HaianCao/ClaudApter/integration/protoverta/scripts/install.ps1"))) -WithGateway -SourceBranch integration/protoverta
```

The gateway component is version-pinned. Select **Local Protoverta gateway** as the connection mode. Native Messages preserves beta headers and thinking/signature payloads. Chat and Responses translation supports text, URL/base64 user image inputs and client-side function tools, including streaming and multiple tool calls. Rich image tool results map Messages ↔ Responses; Chat routes and provider-owned file IDs remain unsupported. Public reasoning from Chat/Responses is not converted into fabricated Messages thinking/signatures. The regular `main` installer remains the stable, non-gateway path.

Under **Advanced: model compatibility checks**, you can select a local capability
registry file and require verified capabilities. Relative paths start at the
configuration folder. Unsupported features are blocked before API calls; the
strict option also blocks missing records. Leave this optional section empty
unless you maintain capability records for your provider's exact API prefix and
model IDs. For Messages, registry entries use the actual API prefix including
`/v1`, whether setup was given the root URL or a URL already ending in `/v1`. Basic launch capabilities are
checked before setup closes; tools and output formats are checked against each
actual request.

Translated sessions disable thinking and unsupported experimental capabilities in the child process only. Cache hints and telemetry are omitted, and tool errors become labelled text. Initial token usage is provisional and corrected at completion using real upstream counts. Effort supports default/low/medium/high; hidden reasoning replay, audio/video, hosted tools and compaction are not supported. Use a compatible native Messages provider when you need features outside the translated subset. Existing sessions containing unsupported blocks cannot be resumed through translation.

## Use ClaudApter

```powershell
claude-ca                                      # launch using saved defaults
claude-ca setup                                # open the provider setup page
claude-ca run -p PROVIDER -m MODEL_ID          # choose a provider and model for this run
claude-ca print -p PROVIDER -m MODEL_ID -- "Explain this repository"
claude-ca resume -p PROVIDER -m MODEL_ID -- SESSION_ID
claude-ca continue -p PROVIDER -m MODEL_ID
claude-ca providers
claude-ca models -p PROVIDER
claude-ca doctor -p PROVIDER -m MODEL_ID
claude-ca validate
claude-ca --help
```

After installation, `claude-ca` is available from any working directory. Re-run the installer command to update ClaudApter; your provider settings and API keys are retained. Updates are checked before activation, so a failed download or package preparation leaves the existing version usable.

## Uninstall

In PowerShell, run the uninstaller directly from GitHub:

```powershell
& ([scriptblock]::Create((irm "https://raw.githubusercontent.com/HaianCao/ClaudApter/main/scripts/uninstall.ps1")))
```

It removes the launcher and its wrapper PATH entries, not the official Claude Code CLI. Separate prompts let you reset adapter environment overrides and delete default provider settings, API keys and cache (type `DELETE`). Everything is kept unless you confirm its removal.

Environment reset clears only `CLAUDE_CA_CONFIG`, `CLAUDE_CA_CACHE` and `CLAUDE_CA_ENV_FILE` at user/process scope. It does not delete custom-path files, provider key variables, or shared `ANTHROPIC_*`/`OPENAI_*` variables. Machine-wide settings are not changed. Open a new terminal afterward. To explicitly request reset without prompts, append `-ResetEnvironment -NonInteractive`; data deletion still requires interactive confirmation.

## Advanced gateway settings

Optional advanced gateway policies support evidence-scoped provider profile
files, shared quota databases and bounded HTTP-rejection retries. Leave them
blank for existing behavior. They require the matching unpublished Protoverta
source build, not the older public alpha-6 wheel. Use the same absolute database
path in both launchers to coordinate the same upstream prefix/credential.
These policies do not change direct mode or enable model fallback.

## Privacy and security

Provider settings and API keys are stored on your computer. API keys are kept in a local `.env` file in plain text, so protect the file and never commit or share it. A key saved there takes precedence over an environment variable of the same name; the environment variable is used only when the file has no value. Provider credentials are kept separate from Claude Code's own authentication. The setup page listens only on your own computer (`127.0.0.1`) and uses a per-session access token. Direct mode does not proxy conversations; optional gateway mode starts a temporary loopback Protoverta server and stops it when Claude Code exits.

Gateway mode is experimental. Client interoperability is tested with scripted local upstreams; each real provider/model still needs its own compatibility check. Gateway mode passes a temporary local credential to Claude Code and keeps the provider key in the launcher.

ClaudApter does not edit Claude Code's own settings or session transcripts. The `doctor` command sends a small real `/v1/messages` request and may incur an API charge; it does not verify streaming or tool calls. Resuming with a different provider/model can be attempted, but equivalent reasoning state or tool behavior is not guaranteed.

If Claude Code's `/status` shows a different gateway or credential from the one selected in ClaudApter, check for conflicting `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, or `ANTHROPIC_API_KEY` values in Claude Code's [settings files](https://code.claude.com/docs/en/llm-gateway-connect). Claude Code gives settings-file environment values precedence over a launcher process's environment; ClaudApter does not overwrite those settings. In API-key mode, Claude Code may also ask you to approve the key once.

## Session tools

The integration preview has a local-fixture check for one foreground subagent
using the selected provider/model on Claude Code **2.1.308** and **2.1.318**, through native
Messages or translated Chat/Responses. It verifies a separate child request,
a successful read-only tool result and completion reported by the harness.
Choose the **selected** agent-model policy to keep the same-model default.
The **custom** policy can select an exact child model on that same provider.
Local fixtures verify model B tool use and continuation through `SendMessage`
while the parent keeps model A. Known capability/effort/budget limits are checked
against each requested model; missing evidence follows the configured verification policy.
This is harness interoperability evidence, not a guarantee for every provider.
Detached execution and closed/persisted child resume need separate checks;
custom agent definitions and explicit overrides retain Claude Code precedence.

External stdio MCP servers can be selected for one launch with
`CLAUDE_CA_MCP_CONFIG`: a JSON file with `mcpServers`, each containing an absolute
executable `command`, optional `args`, and `env_vars` names. Claude loads only
these servers for that launch. Keys stay in environment variables; the temporary
Claude configuration contains variable references and is cleaned up on exit.

## For contributors

```powershell
python -m pip install -e ".[test]"
python -m pytest
```

To test an unpublished gateway build, pass `-ProtovertaWheel PATH_TO_WHEEL` to the local installer. This enables the gateway extra and checks its pinned package version without copying Protoverta source into this project.

Installer smoke tests can pass `-NoPathChanges` to install and uninstall scripts. Redirect `APPDATA` and `LOCALAPPDATA` in a disposable child shell as well: the flag alone does not isolate installation or user data paths.

## Gateway checks and diagnostics

Gateway previews provide route checks through `claude-ca doctor -p PROVIDER -m MODEL --allow-live --check text --check streaming --report NEW_REPORT.json`.
These checks may incur provider charges; without `--allow-live`, gateway doctor makes no provider requests.
Advanced setup can enable local session diagnostics, counting policies and evidence freshness checks. Diagnostics omit prompts and keys; reopen setup to view the last session report.

## License

ClaudApter is licensed under the [MIT License](LICENSE).
