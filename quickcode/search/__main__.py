"""``python -m quickcode.search`` -- inspect providers and store a key.

The encrypted store is the right place for a search key and there is no way to
reach it from a text editor, so this is the way to put one there until the
Settings pane grows a field. Three commands:

    python -m quickcode.search list             # providers, env vars, signup
    python -m quickcode.search status           # what is configured right now
    python -m quickcode.search set-key brave    # prompts, never echoes
    python -m quickcode.search clear-key brave

The key is read with ``getpass`` and never printed back, not even truncated.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from quickcode.search.resolve import (
    PROVIDERS,
    chosen_provider,
    provider_names,
    resolve_credentials,
    secret_name,
)


def _settings():
    from quickcode.config import Config

    return Config.load().search


def cmd_list() -> int:
    for name, cls in PROVIDERS.items():
        info = cls.info
        print(f"{name}")
        # Plain ASCII: a legacy Windows console codepage cannot encode a dash
        # that is not a hyphen, and this command exists to be readable.
        print(f"    {info.label} - {info.signup_url}")
        if info.needs_key:
            print(f"    key:      {info.api_key_env}  (or: set-key {name})")
        if info.needs_base_url:
            print(f"    base url: {info.base_url_env}")
        for key, var, label in info.extra_fields:
            print(f"    {key}:       {var}  - {label}")
        if info.free_tier:
            print(f"    free tier: {info.free_tier}")
    return 0


def cmd_status() -> int:
    settings = _settings()
    active = chosen_provider(settings)
    for name, cls in PROVIDERS.items():
        _, missing = resolve_credentials(cls.info, settings)
        mark = "*" if name == active else " "
        state = "ready" if not missing else f"missing {', '.join(missing)}"
        print(f"{mark} {name:<12} {state}")
    print("\n(* = the provider web_search would use)")
    return 0


def cmd_set_key(provider: str, from_stdin: bool) -> int:
    if provider not in PROVIDERS:
        print(f"unknown provider {provider!r}; known: {', '.join(provider_names())}")
        return 2
    info = PROVIDERS[provider].info
    if not info.needs_key:
        print(f"{info.label} takes no API key.")
        return 2

    key = sys.stdin.readline().strip() if from_stdin else getpass.getpass(
        f"{info.label} API key (input hidden): "
    ).strip()
    if not key:
        print("no key entered; nothing saved.")
        return 1

    from quickcode.secrets import save_secret

    save_secret(secret_name(provider), key)
    print(f"saved, encrypted at rest, for {info.label}.")
    return 0


def cmd_clear_key(provider: str) -> int:
    if provider not in PROVIDERS:
        print(f"unknown provider {provider!r}; known: {', '.join(provider_names())}")
        return 2
    from quickcode.secrets import clear_secret

    clear_secret(secret_name(provider))
    print(f"cleared any stored key for {provider}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m quickcode.search", description=__doc__)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("list", help="every provider, its env var and its signup page")
    sub.add_parser("status", help="what is configured, and which provider is active")
    set_key = sub.add_parser("set-key", help="store a provider's key, encrypted")
    set_key.add_argument("provider")
    set_key.add_argument("--stdin", action="store_true", help="read the key from stdin")
    clear = sub.add_parser("clear-key", help="delete a provider's stored key")
    clear.add_argument("provider")

    args = parser.parse_args(argv)
    if args.command == "status":
        return cmd_status()
    if args.command == "set-key":
        return cmd_set_key(args.provider, args.stdin)
    if args.command == "clear-key":
        return cmd_clear_key(args.provider)
    return cmd_list()


if __name__ == "__main__":
    raise SystemExit(main())
