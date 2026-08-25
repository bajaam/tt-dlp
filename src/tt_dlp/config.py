"""Configuration discovery, validation, and persistent queue loading."""

from __future__ import annotations

import json
import os
import sys
from argparse import Namespace
from pathlib import Path

from .errors import TikTokError
from .models import Settings


DEFAULT_CONFIG = {
    "output": "~/Downloads/TikTok",
    "cookies_file": "",
    "queue_file": "queue.txt",
    "identity_file": "profiles.json",
    "queue": [],
    "sleep": 2.0,
    "limit": 0,
    "overwrite": False,
    "dry_run": False,
    "stories": True,
}
CONFIG_KEYS = frozenset(DEFAULT_CONFIG)


def user_config_directory() -> Path:
    configured = os.environ.get("XDG_CONFIG_HOME")
    if configured:
        return Path(configured).expanduser() / "tt-dlp"
    return Path.home() / ".config" / "tt-dlp"


def discover_config(args: Namespace) -> Path | None:
    if args.config:
        return Path(args.config).expanduser()
    configured = os.environ.get("TT_DLP_CONFIG")
    if configured:
        return Path(configured).expanduser()

    candidates = [
        Path.cwd() / "config.json",
        user_config_directory() / "config.json",
    ]
    app_data = os.environ.get("APPDATA")
    if app_data:
        candidates.append(Path(app_data) / "tt-dlp" / "config.json")
    if sys.platform == "darwin":
        candidates.append(
            Path.home() / "Library" / "Application Support"
            / "tt-dlp" / "config.json"
        )
    return next((path for path in candidates if path.is_file()), None)


def _load_config(path: Path | None, *, required: bool) -> dict:
    if path is None:
        if required:
            raise TikTokError(
                "No config file or queue targets were found. Run "
                "'tt-dlp --init-config ~/.config/tt-dlp/config.json'."
            )
        return {}
    path = path.expanduser().resolve()
    if not path.is_file():
        raise TikTokError(f"Config file not found: {path}")
    try:
        with path.open("r", encoding="utf-8") as file:
            config = json.load(file)
    except (OSError, ValueError) as exc:
        raise TikTokError(f"Could not read config file {path}: {exc}") from exc
    if not isinstance(config, dict):
        raise TikTokError("The config file must contain one JSON object")
    unknown = sorted(set(config) - CONFIG_KEYS)
    if unknown:
        print(
            "Warning: ignoring unknown config "
            f"{'key' if len(unknown) == 1 else 'keys'}: {', '.join(unknown)}",
            file=sys.stderr,
        )
    print(f"Using config: {path}")
    return {key: value for key, value in config.items() if key in CONFIG_KEYS}


def _config_bool(config: dict, name: str, default: bool) -> bool:
    value = config.get(name, default)
    if type(value) is not bool:
        raise TikTokError(f"Config value {name!r} must be true or false")
    return value


def _config_number(config: dict, name: str, default, number_type):
    value = config.get(name, default)
    if isinstance(value, bool):
        raise TikTokError(f"Config value {name!r} must be a number")
    try:
        return number_type(value)
    except (TypeError, ValueError) as exc:
        raise TikTokError(f"Config value {name!r} must be a number") from exc


def _path_value(value, *, base: Path, name: str) -> Path | None:
    if value in (None, ""):
        return None
    if not isinstance(value, (str, os.PathLike)):
        raise TikTokError(f"Config value {name!r} must be a path string")
    path = Path(value).expanduser()
    return path if path.is_absolute() else base / path


def _read_queue(path: Path) -> list[str]:
    try:
        with path.open("r", encoding="utf-8-sig") as file:
            return [
                line.strip()
                for line in file
                if line.strip() and not line.lstrip().startswith("#")
            ]
    except OSError as exc:
        raise TikTokError(f"Could not read queue file {path}: {exc}") from exc


def prepare_run(args: Namespace) -> tuple[Settings, list[str]]:
    config_path = discover_config(args)
    explicit_targets = bool(args.targets or args.queue_file)
    config = _load_config(config_path, required=not explicit_targets)
    config_dir = config_path.resolve().parent if config_path else Path.cwd()

    if args.output is not None:
        output = _path_value(args.output, base=Path.cwd(), name="output")
    else:
        output = _path_value(
            config.get("output", DEFAULT_CONFIG["output"]),
            base=config_dir,
            name="output",
        )
    if output is None:
        raise TikTokError("Output directory cannot be empty")

    if args.cookies is not None:
        cookies = _path_value(args.cookies, base=Path.cwd(), name="cookies_file")
    else:
        cookies = _path_value(
            config.get("cookies_file", ""),
            base=config_dir,
            name="cookies_file",
        )

    if args.profile_store is not None:
        profile_store = _path_value(
            args.profile_store, base=Path.cwd(), name="identity_file"
        )
    elif config_path:
        profile_store = _path_value(
            config.get("identity_file", DEFAULT_CONFIG["identity_file"]),
            base=config_dir,
            name="identity_file",
        )
    else:
        profile_store = user_config_directory() / "profiles.json"

    limit = (
        args.limit
        if args.limit is not None
        else _config_number(config, "limit", DEFAULT_CONFIG["limit"], int)
    )
    sleep = (
        args.sleep
        if args.sleep is not None
        else _config_number(config, "sleep", DEFAULT_CONFIG["sleep"], float)
    )
    if limit < 0 or sleep < 0:
        raise TikTokError("Values for limit and sleep cannot be negative")

    overwrite = (
        args.overwrite
        if args.overwrite is not None
        else _config_bool(config, "overwrite", False)
    )
    dry_run = (
        args.dry_run
        if args.dry_run is not None
        else _config_bool(config, "dry_run", False)
    )
    stories = (
        args.stories
        if args.stories is not None
        else _config_bool(
            config, "stories", DEFAULT_CONFIG["stories"]
        )
    )

    targets = [str(value).strip() for value in args.targets if str(value).strip()]
    if args.queue_file:
        queue_path = _path_value(
            args.queue_file, base=Path.cwd(), name="queue_file"
        )
        targets.extend(_read_queue(queue_path))
    elif not targets:
        configured_queue = config.get("queue", [])
        if isinstance(configured_queue, str):
            configured_queue = [configured_queue]
        if not isinstance(configured_queue, list) or not all(
            isinstance(value, str) for value in configured_queue
        ):
            raise TikTokError("Config value 'queue' must be a list of strings")
        targets.extend(value.strip() for value in configured_queue if value.strip())
        configured_file = config.get("queue_file", "")
        queue_path = _path_value(
            configured_file, base=config_dir, name="queue_file"
        )
        if queue_path:
            targets.extend(_read_queue(queue_path))

    targets = list(dict.fromkeys(targets))
    if not targets:
        raise TikTokError(
            "The queue is empty. Add targets to config.json, queue.txt, "
            "or the command line."
        )

    settings = Settings(
        output=output,
        cookies=cookies,
        profile_store=profile_store,
        limit=int(limit),
        sleep=float(sleep),
        overwrite=bool(overwrite),
        dry_run=bool(dry_run),
        stories=bool(stories),
        identify=bool(args.identify),
    )
    return settings, targets


def initialize_config(filename: str | os.PathLike) -> int:
    config_path = Path(filename).expanduser().resolve()
    queue_path = config_path.parent / "queue.txt"
    if config_path.exists():
        raise TikTokError(f"Refusing to overwrite existing config: {config_path}")

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config = dict(DEFAULT_CONFIG)
    with config_path.open("x", encoding="utf-8") as file:
        json.dump(config, file, indent=2)
        file.write("\n")
    if not queue_path.exists():
        with queue_path.open("x", encoding="utf-8") as file:
            file.write(
                "# Add one TikTok target per line.\n"
                "# @profile_name\n"
                "# ttid:1234567890123456789:MS4wLjABAAAA...\n"
            )
    print(f"Created config: {config_path}")
    print(f"Created queue:  {queue_path}")
    print("Add profiles to the queue, then run: tt-dlp")
    return 0
