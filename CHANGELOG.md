# Changelog

## 1.4.0 - 2026-11-07

- Added selectable `auto`, `apple-vision`, `tesseract`, and `none` OCR backends with per-frame automatic fallback.
- Added Tesseract language-data validation, executable override, and Unicode-safe image streaming.
- Replaced shell-based MCP startup with a common Python launcher for macOS, Linux, and native Windows.
- Added three-platform CI, native OCR image checks, and launcher regressions.
- Documented OCR installation, external dependency licenses, and unbenchmarked alternatives.

## 1.3.1 - 2026-10-11

- Fixed Claude Code MCP startup from unrelated session directories by using the installed plugin root in the Claude manifest.
- Preserved Codex startup relative to its installed plugin directory.
- Added host-specific startup regression tests and MCP initialization checks in CI.
- Documented upgrading from 1.3.0 without editing installation caches.

## 1.3.0 - 2026-08-23

- Added GitHub-ready Codex and Claude Code marketplace catalogs.
- Added a self-contained first-run Python dependency bootstrap with an isolated cache.
- Removed dependence on developer-local absolute paths and the 100+ MB vendored dependency directory.
- Added release, privacy, terms, security, compatibility, and architecture documentation.
- Added distribution validation and continuous integration.
- Preserved caption-first analysis, selective local STT, targeted visual inspection, structured transport errors, and verified `yt-dlp` updates.
