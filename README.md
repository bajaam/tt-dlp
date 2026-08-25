# tt-dlp

[![Tests](https://github.com/bajaam/tt-dlp/actions/workflows/tests.yml/badge.svg)](https://github.com/bajaam/tt-dlp/actions/workflows/tests.yml)

`tt-dlp` is a queue-based TikTok downloader for public profiles, individual
videos, photo carousels, and active stories. It can remember a creator's stable
TikTok identity, so an existing queue and output folder continue to work after
the creator changes their username.

The application is written entirely with Python's standard library. It does
not install, import, or run `yt-dlp` or `gallery-dl`.

## Features

- Downloads videos, full photo carousels, and active stories
- Accepts profiles, media URLs, short links, and rename-safe profile IDs
- Scans a complete profile before starting its download queue
- Reads multiple targets from the command line, JSON config, or a text queue
- Remembers username changes without splitting a creator's output directory
- Skips completed files and uses `.part` files for interrupted downloads
- Holds and retries the current media file instead of silently moving past it
- Supports authenticated access through an optional Netscape cookies file
- Runs on Windows, Linux, and macOS with Python 3.10 or newer

TikTok's web responses are undocumented and can change without notice. A
future TikTok change may require an update to `tt-dlp`.

## Installation

Install the current GitHub version directly.

### Windows

```powershell
py -m pip install "git+https://github.com/bajaam/tt-dlp.git"
```

### Linux or macOS

```bash
python3 -m pip install --user "git+https://github.com/bajaam/tt-dlp.git"
```

Verify the command:

```bash
tt-dlp --version
tt-dlp --help
```

If the command is not on your shell's `PATH`, use the module form:

```powershell
py -m tt_dlp --help
```

```bash
python3 -m tt_dlp --help
```

To install a local clone for development instead, open a terminal in the
repository and run `python -m pip install -e .`.

## Quick start

Download a public profile:

```bash
tt-dlp @profile_name
```

Download one video, photo post, or active story:

```bash
tt-dlp "https://www.tiktok.com/@profile_name/video/1234567890123456789"
```

Preview filenames without downloading:

```bash
tt-dlp --dry-run @profile_name
```

Active stories are included automatically when a usable TikTok cookies file is
configured. TikTok requires current cookies for profile Story lookup, even when
the profile is public:

```bash
tt-dlp --cookies /path/to/cookies.txt @profile_name
```

Use `--no-stories` to download only regular posts. Individual shared Story
links work directly and are recognized from TikTok's Story query markers.

## Rename-safe profile targets

TikTok usernames can change. `tt-dlp` records a profile's numeric user ID,
opaque `secUid`, current username, previous aliases, and original output folder
in a small `profiles.json` file. Future runs can follow the same creator while
keeping all downloads together.

Generate the recommended stable target:

```bash
tt-dlp --identify @profile_name
```

The command prints a value that can be placed directly in `queue.txt`:

```text
ttid:1234567890123456789:MS4wLjABAAAAExampleStableIdentifier1234567890
```

Supported stable forms:

| Form | Use |
| --- | --- |
| `ttid:<userId>:<secUid>` | Recommended. Carries both IDs needed for posts and stories. |
| `secuid:<secUid>` | Rename-safe post lookup when the opaque ID is already known. |
| `userid:<userId>` | Works only when that numeric ID is already mapped in `profiles.json`. |

TikTok's profile-post endpoint does not resolve an unknown numeric user ID by
itself, which is why `ttid:` is preferred over `userid:`. The profile store is
not a cookie or password file, but it is machine-managed and normally should
not be edited by hand. Deleting it resets remembered aliases and folder names;
downloaded media is not removed.

By default, the profile store is `profiles.json` beside an active config file,
or in the per-user `tt-dlp` config directory when no config is loaded. Choose a
different file with:

```bash
tt-dlp --profile-store /path/to/profiles.json @profile_name
```

The equivalent config setting is `"identity_file": "profiles.json"`.

## Persistent config and queue

Create a starter `config.json` and `queue.txt` in the standard per-user
`tt-dlp` config folder:

```bash
tt-dlp --init-config
```

You can also choose any config location, including the current directory:

```bash
tt-dlp --init-config ./config.json
```

Example configuration:

```json
{
  "output": "~/Downloads/TikTok",
  "cookies_file": "",
  "queue_file": "queue.txt",
  "identity_file": "profiles.json",
  "queue": [],
  "sleep": 2.0,
  "limit": 0,
  "overwrite": false,
  "dry_run": false,
  "stories": true
}
```

| Setting | Meaning |
| --- | --- |
| `output` | Base download directory; each creator gets a stable subdirectory. |
| `cookies_file` | Optional Netscape-format TikTok cookies file. |
| `queue_file` | Text file containing one target per line. |
| `identity_file` | Machine-managed profile identity and rename store. |
| `queue` | Optional JSON list of targets. |
| `sleep` | Minimum delay in seconds between media downloads. |
| `limit` | Maximum regular posts per profile after scanning; `0` means all. |
| `overwrite` | Replace completed files when `true`. |
| `dry_run` | Show planned filenames without downloading when `true`. |
| `stories` | Scan active stories when `true` (default); skipped if no usable cookies are loaded. |

Relative paths inside the config resolve from the config file's directory.
Relative paths passed on the command line resolve from the current directory.
Invalid setting types are rejected, and unknown setting names produce a
warning instead of being silently applied.

With no `--config`, `tt-dlp` checks the current directory and normal per-user
config locations, including `~/.config/tt-dlp`, `%APPDATA%/tt-dlp` on Windows,
and Application Support on macOS. Set `TT_DLP_CONFIG` to choose the automatic
config path explicitly.

Explicit command-line targets still inherit settings such as output, cookies,
sleep, and stories from the auto-discovered config. They replace the config's
persistent queue for that run. An explicit `--queue-file` behaves the same way.
When there are no explicit targets, the JSON `queue` and configured
`queue_file` are combined in order and duplicates are removed.

## Queue format

Add one target per line. Blank lines and lines beginning with `#` are ignored.

```text
# Usernames and profiles
@profile_one
https://www.tiktok.com/@profile_two

# Individual media
https://www.tiktok.com/@profile_three/video/1234567890123456789
https://www.tiktok.com/@profile_three/photo/1234567890123456789
https://www.tiktok.com/@profile_three/story/1234567890123456789

# Rename-safe identity
ttid:1234567890123456789:MS4wLjABAAAAExampleStableIdentifier1234567890
```

Run an explicit queue file with:

```bash
tt-dlp --queue-file /path/to/queue.txt
```

See [`examples/queue.example.txt`](examples/queue.example.txt) and
[`examples/config.example.json`](examples/config.example.json) for clean
templates.

## Cookies and private profiles

Cookies are optional for ordinary public content. For authenticated access:

1. Sign in to TikTok in a browser.
2. Export TikTok cookies in Netscape `cookies.txt` format.
3. Set `cookies_file` in the config or pass `--cookies`.

```bash
tt-dlp --cookies /path/to/cookies.txt @profile_name
```

For a private profile, the cookies must belong to an account that TikTok has
already allowed to view it. Cookies cannot bypass privacy controls, approval,
age gates, regional restrictions, or removed content. Private-profile access
is not guaranteed because TikTok may reject authenticated web requests.

Cookie files contain sign-in secrets. Never share or commit them. Common cookie
filenames are excluded by this repository's `.gitignore`.

## Output and filenames

The default output root is `~/Downloads/TikTok`. Regular posts are placed in a
stable creator directory and stories in its `stories/` subdirectory. A saved
directory does not change when the creator changes username.

```text
TikTok/
└── profile_name/
    ├── 7671697231396998408 example description.mp4
    ├── 7671697231396998409_01 example photo post.jpg
    ├── 7671697231396998409_02 example photo post.jpg
    └── stories/
        └── 7671697231396998410 story.mp4
```

Existing completed filenames are skipped unless `--overwrite` is used.
Interrupted transfers remain as `.part` files and are not treated as complete.

## Command reference

```text
-c, --config FILE        Load a JSON config file
--init-config [FILE]     Create a config and queue
-o, --output DIRECTORY  Set the base output directory
--cookies FILE           Load Netscape-format TikTok cookies
--queue-file FILE        Use targets from a text file
--profile-store FILE     Set the profile identity store
--identify               Print a canonical stable target
--limit NUMBER           Limit regular posts after scanning (0 = all)
--sleep SECONDS          Set the minimum inter-download delay
--overwrite              Replace completed files
--dry-run                Show planned filenames without downloading
--stories                Include active stories for profile targets (default)
--no-stories             Download regular posts without scanning stories
--version                Show the installed version
```

Run `tt-dlp --help` for the authoritative help text for your installed version.

## Development

The project uses a standard `src/` package layout and has no runtime
dependencies.

```bash
python -m unittest discover -s tests -v
python -m pip wheel --no-deps --wheel-dir dist .
```

Tests run on Python 3.10 through 3.14, with Windows and macOS smoke coverage in
GitHub Actions. Release history is recorded in [`CHANGELOG.md`](CHANGELOG.md).

## Responsible use

Download only content you own or have permission to save. You are responsible
for applicable copyright rules, privacy requirements, and TikTok's terms. This
project is not affiliated with TikTok.

## License

MIT. See [`LICENSE`](LICENSE).
