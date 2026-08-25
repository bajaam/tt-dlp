#!/usr/bin/env python3
"""Command-line entry point for tt-dlp."""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .config import initialize_config, prepare_run, user_config_directory
from .downloader import TikTokDownloader
from .errors import TikTokError


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tt-dlp",
        description=(
            "Download TikTok videos, photo posts, and active stories using "
            "a persistent multi-profile queue"
        ),
    )
    parser.add_argument(
        "targets",
        nargs="*",
        help=(
            "@username, TikTok URL, ttid:<userId>:<secUid>, secuid:<value>, "
            "or a previously recorded userid:<digits>"
        ),
    )
    parser.add_argument(
        "-c",
        "--config",
        help="JSON config file (otherwise discovered automatically)",
    )
    parser.add_argument(
        "--init-config",
        nargs="?",
        const=str(user_config_directory() / "config.json"),
        metavar="FILE",
        help="create a config and queue in the user config folder",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="base output folder; each profile uses a stable subfolder",
    )
    parser.add_argument(
        "--cookies",
        help="Netscape-format cookies.txt file for private profiles/stories",
    )
    parser.add_argument(
        "--queue-file",
        help="text file containing one target per line",
    )
    parser.add_argument(
        "--profile-store",
        help="profiles.json location (default: beside config.json)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="download at most this many regular posts (0 means all)",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        help="minimum seconds between completed media downloads",
    )
    parser.add_argument(
        "--overwrite",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="replace media already present in the archive",
    )
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="scan and show filenames without downloading media",
    )
    parser.add_argument(
        "--stories",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "include active stories for profiles (enabled by default when "
            "cookies are available)"
        ),
    )
    parser.add_argument(
        "--identify",
        action="store_true",
        help="resolve and save identity, then print a rename-safe target",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        # Keep terminal output usable on legacy Windows consoles while leaving
        # Unicode filenames untouched on disk.
        for stream in (sys.stdout, sys.stderr):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure:
                reconfigure(errors="replace")

        args = parse_args(argv)
        if args.init_config:
            return initialize_config(args.init_config)

        settings, targets = prepare_run(args)
        downloader = TikTokDownloader(settings)
        failures = 0
        print(f"Queued targets: {len(targets)}")
        for number, target in enumerate(targets, 1):
            print()
            print(f"=== Queue {number}/{len(targets)}: {target} ===")
            try:
                failures += int(bool(downloader.run(target)))
            except TikTokError as exc:
                failures += 1
                print(f"Error for {target}: {exc}", file=sys.stderr)

        print()
        print(
            f"Queue finished: {len(targets) - failures} succeeded, "
            f"{failures} failed"
        )
        return 1 if failures else 0
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130
    except TikTokError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
