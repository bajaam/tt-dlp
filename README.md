# tt-dlp

`tt-dlp` is a standalone, queue-based TikTok downloader for public profiles,
individual videos, and photo posts. It can optionally load your own exported
TikTok cookies when authenticated access is needed.

It does **not** import, install, or run `yt-dlp` or `gallery-dl`. Its runtime
uses only Python's standard library.

## Features

- Runs on Windows, Linux, and macOS
- Installs a terminal command named `tt-dlp`
- Accepts multiple profiles and post URLs in one command
- Creates and reads a persistent `config.json` and `queue.txt`
- Scans each complete profile before downloading its media
- Retries transient empty profile responses instead of counting a zero page
- Downloads videos and full photo carousels
- Skips files that already exist
- Retries the current file until it succeeds or you press `Ctrl+C`
- Supports an optional Netscape-format cookies file
- Falls back to a public scan when cookies hide a public profile's posts
- Uses `.part` files to avoid treating interrupted downloads as complete

Example filenames:

```text
7671697231396998408 example description.mp4
7671697231396998408_01 example photo post.jpg
7671697231396998408_02 example photo post.jpg
```

## Requirements

- Python 3.10 or newer
- An internet connection

There are no runtime packages to install. Python packaging tools are used only
to register the `tt-dlp` command.

## Install the `tt-dlp` command

Download or clone this repository, open a terminal inside it, and run:

### Windows

```powershell
py -m pip install .
```

### Linux or macOS

```bash
python3 -m pip install --user .
```

You can then run:

```bash
tt-dlp --help
```

After installation it can also be run through Python directly:

```bash
python3 -m tt_dlp --help
```

## Quick commands

One profile:

```bash
tt-dlp username
```

Multiple profiles/posts in one queue:

```bash
tt-dlp profile_one profile_two "https://www.tiktok.com/@name/video/1234567890"
```

Use a specific config:

```bash
tt-dlp --config config.json
```

Use a queue file without a config:

```bash
tt-dlp --queue-file queue.txt
```

## Persistent queue and config

Create your personal config and queue once:

```bash
tt-dlp --init-config
```

This creates `config.json` and `queue.txt` in the current directory. Edit
`queue.txt` and add one target per line:

```text
profile_one
https://www.tiktok.com/@profile_two
https://www.tiktok.com/@someone/video/1234567890
```

Blank lines and lines beginning with `#` are ignored. A plain `tt-dlp` command
automatically checks the current directory and `~/.config/tt-dlp/config.json`.
It also supports `%APPDATA%/tt-dlp/config.json` on Windows,
`$XDG_CONFIG_HOME/tt-dlp/config.json` on Linux, and the user Application
Support folder on macOS. Set `TT_DLP_CONFIG` to choose another automatic path.
If no config is found and no targets are supplied, `tt-dlp` asks for its path.
Clean examples are provided under [`examples/`](examples/).

Available `config.json` settings:

```json
{
  "output": "~/Downloads/TikTok",
  "cookies_file": "",
  "queue_file": "queue.txt",
  "queue": [],
  "sleep": 2.0,
  "limit": 0,
  "overwrite": false,
  "dry_run": false
}
```

- `output`: base folder; each profile gets its own subfolder
- `cookies_file`: optional path to an exported Netscape `cookies.txt`
- `queue_file`: optional persistent text queue
- `queue`: targets stored directly inside the JSON config
- `sleep`: minimum delay between media downloads
- `limit`: maximum posts per profile after the complete scan; `0` means all
- `overwrite`: replace existing completed files when `true`
- `dry_run`: scan and show filenames without downloading when `true`

Relative paths in a config are resolved from the config file's directory.
Command-line targets, the JSON queue, and the text queue are combined in order;
duplicates are removed.

## Cookies and private profiles

Cookies are optional for public content. For authenticated access:

1. Sign in to TikTok in your browser.
2. Export TikTok cookies in Netscape `cookies.txt` format.
3. Set `cookies_file` in `config.json`, or run:

```bash
tt-dlp --cookies /path/to/cookies.txt username
```

For a private profile, the cookies must belong to an account that is already
allowed to view that profile. Cookies cannot bypass privacy controls, approval,
age gates, regional restrictions, or removed content. TikTok may still block
authenticated web requests, so private-profile support cannot be guaranteed.

Cookie files contain sign-in secrets. Never share or commit them. Common cookie
filenames are excluded by this project's `.gitignore`.

## Other options

```text
-c, --config FILE       Load JSON configuration
--init-config [FILE]    Create a config and queue
-o, --output FOLDER     Set the base output folder
--cookies FILE          Load Netscape-format TikTok cookies
--queue-file FILE       Add targets from a text file
--limit NUMBER          Limit posts per profile after scanning (0 = all)
--sleep SECONDS         Minimum delay between media downloads
--overwrite             Replace existing files
--dry-run               Scan and show filenames without downloading
```

## How it works

`tt-dlp` reads TikTok's web embed/profile data to identify a creator, collects
posts from TikTok's creator item-list response, and downloads the media URLs
provided for those posts. TikTok can change these undocumented responses at
any time, which may require this project to be updated.

## Project layout

```text
tt-dlp/
├── examples/           Safe example config and queue
├── src/tt_dlp/         Installable Python package
├── tests/              Standard-library unit tests
├── LICENSE
├── README.md
└── pyproject.toml      Package metadata and tt-dlp command
```

Run the test suite from the repository root:

```bash
python3 -m unittest discover -s tests -v
```

## Responsible use

Download only content you own or have permission to save. You are responsible
for following applicable copyright rules, privacy requirements, and TikTok's
terms. This project is not affiliated with TikTok.

## License

MIT. See [LICENSE](LICENSE).
