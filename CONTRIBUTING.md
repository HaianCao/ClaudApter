# Contributing to ClaudApter

Thanks for helping. ClaudApter launches Claude Code with the provider you choose,
either directly or through the optional
[Protoverta](https://github.com/HaianCao/Protoverta) gateway.

## Development setup

Windows, Python 3.11 or newer.

```powershell
python -m pip install -e ".[test]"
python -m pytest
python -m ruff check src tests scripts
python -m ruff format --check .
```

Gateway tests need the pinned Protoverta release:
`python -m pip install -e ".[test,gateway]"`.

## Pull requests

- Add tests for new behaviour. Tests must not call real providers or read your
  real configuration; redirect `APPDATA` and `LOCALAPPDATA` for installer
  checks.
- Never commit API keys or `.env` files.
- Six modules are shared verbatim with the other launcher (CodexApter and
  ClaudApter). Change both copies together; Protoverta's
  `scripts/check_launcher_sync.py` checks them.
- Protocol conversion belongs in Protoverta, not in the launcher.
- Update `CHANGELOG.md` under "Unreleased".
