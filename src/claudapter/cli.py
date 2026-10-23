from __future__ import annotations

import argparse
import sys

from .catalog import fetch_models, probe_messages
from .config import Config, EFFORTS, load
from .errors import ClaudapterError
from .launcher import launch


def _provider(config: Config, provider_id: str):
    provider = config.providers.get(provider_id)
    if not provider:
        raise ClaudapterError("Choose a configured provider with -p, or run claude-ca setup.")
    return provider


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="claude-ca", description="ClaudApter — configure providers and launch Claude Code")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("setup", help="Open the local configuration page")
    for name in ("run", "print", "resume", "continue"):
        cmd = sub.add_parser(name)
        cmd.add_argument("-p", "--provider")
        cmd.add_argument("-m", "--model")
        cmd.add_argument("-e", "--effort", choices=sorted(EFFORTS))
        cmd.add_argument("args", nargs=argparse.REMAINDER, help="Claude Code arguments after --")
    sub.add_parser("providers", help="List configured providers")
    models = sub.add_parser("models", help="List provider models")
    models.add_argument("-p", "--provider", required=True)
    doctor = sub.add_parser("doctor", help="Probe an Anthropic Messages endpoint (may incur API cost)")
    doctor.add_argument("-p", "--provider", required=True)
    doctor.add_argument("-m", "--model", required=True)
    sub.add_parser("validate", help="Validate ClaudApter configuration")
    args = parser.parse_args(argv)
    try:
        config = load()
        if args.command == "setup":
            from .web import serve
            return serve(config)
        if args.command == "validate":
            print("Configuration is valid.")
            return 0
        if args.command == "providers":
            for provider in config.providers.values():
                print(f"{provider.id}\t{provider.display_name}\t{provider.base_url}")
            return 0
        if args.command == "models":
            for item in fetch_models(config, _provider(config, args.provider)):
                print(f"{item['id']}\t{item['display_name']}")
            return 0
        if args.command == "doctor":
            probe_messages(config, _provider(config, args.provider), args.model)
            print("Messages probe passed. Streaming and tool calls are not yet verified.")
            return 0
        mode = args.command or "run"
        provider_id = getattr(args, "provider", None) or config.default_provider
        provider = _provider(config, provider_id)
        model = getattr(args, "model", None) or config.default_model or provider.default_model
        effort = getattr(args, "effort", None) or config.default_effort
        extra = getattr(args, "args", [])
        if extra and extra[0] == "--":
            extra = extra[1:]
        return launch(config, provider, model, effort, extra, mode)
    except ClaudapterError as exc:
        print(f"claude-ca: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
