# Changelog

All notable changes to `tt-dlp` are documented here. The project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.4.0] - 2026-08-26

### Added

- Automatic active-Story scanning for normal profile and queued runs when a
  usable cookies file is loaded.
- Recognition of shared Story URLs that use `/video/<id>` with TikTok's
  `story_type` or `aweme_type` query markers, plus `www.tiktok.com/t/...`
  short links.
- Pagination through TikTok's current `/api/story/item_list/` response.

### Changed

- Profile Story scanning now defaults to enabled. Existing configs that
  explicitly set `"stories": false` remain disabled and receive a visible
  skip message; remove the setting or change it to `true` to enable scanning.
- Direct media uses the embed author's stable identity and skips unrelated
  creator, post-list, and Story-list requests when that identity is complete.

### Fixed

- Canonical profile photo metadata now replaces lightweight creator-embed
  placeholders, preventing carousels from being dispatched as MP4 files.
- Declared photo posts ignore misleading video addresses, and a failed MP4
  refresh that reveals a carousel immediately switches to image filenames.
- Individual Story URLs no longer scan or depend on the creator's full post and
  Story feeds before downloading the requested item.

## [1.3.1] - 2026-08-26

### Fixed

- Carousel retries now recognize TikTok embed refresh metadata under
  `imagePostInfo.displayImages`, preventing valid photo posts from entering an
  empty-media retry loop after an expired or rejected first URL.

## [1.3.0] - 2026-08-26

### Added

- Rename-safe `ttid:`, `secuid:`, and stored `userid:` queue targets.
- Automatic profile identity storage with username alias history and stable
  output directories.
- `--identify` for generating a canonical `ttid:<userId>:<secUid>` target.
- `--profile-store` and the `identity_file` config setting for choosing the
  profile store location.
- Strict target parsing, typed configuration validation, cross-platform CI,
  and package-build checks.
- Media-link refresh after failed CDN rounds, response-content validation,
  atomic partial files, and ID-based duplicate detection.

### Changed

- Explicit command-line targets now inherit auto-discovered config settings
  while replacing the persistent queue for that run.
- Configuration, target parsing, profile state, and download outcomes are
  separated into focused modules.
- Relative command-line paths resolve from the current directory; relative
  config paths resolve from the config file's directory.
- Profile scans are bounded after repeated empty pages and can retry public
  enumeration without authenticated cookies.

## 1.2.0 - 2026-08-26

### Added

- Optional authenticated downloads for active video and photo stories.

## [1.1.4] - 2026-08-13

### Fixed

- Empty authenticated scans of public profiles retry without cookies.

[Unreleased]: https://github.com/bajaam/tt-dlp/compare/v1.4.0...HEAD
[1.4.0]: https://github.com/bajaam/tt-dlp/compare/v1.3.1...v1.4.0
[1.3.1]: https://github.com/bajaam/tt-dlp/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/bajaam/tt-dlp/compare/v1.1.4...v1.3.0
[1.1.4]: https://github.com/bajaam/tt-dlp/releases/tag/v1.1.4
