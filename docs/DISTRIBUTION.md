# Distribution guide

## GitHub-first release

This repository contains two marketplace catalogs that point to the same self-contained plugin directory:

```text
repository root
├── .agents/plugins/marketplace.json   # ChatGPT desktop / Codex
├── .claude-plugin/marketplace.json    # Claude Code
└── plugins/youtube-deep-analyzer/     # shared plugin implementation
    ├── .codex-plugin/plugin.json
    ├── .claude-plugin/plugin.json
    ├── .mcp.json
    ├── skills/
    └── scripts/
```

Before publishing:

1. Create a GitHub repository and copy this directory into its root.
2. Replace generic contributor names with the publisher's public name if desired.
3. Review `docs/PRIVACY.md` and `docs/TERMS.md`; obtain legal review before an official marketplace submission.
4. Run the local validator, unit tests, Codex plugin validator, and Claude plugin validator.
5. Push a signed tag such as `v1.3.1` and attach a source archive generated from that tag.
6. Test installation from a separate user account or clean machine before announcing the release.

## ChatGPT and Codex

GitHub repository marketplaces are suitable for development, teams, and direct distribution. Public discovery in the universal ChatGPT/Codex directory is a separate review process.

The current MCP server uses local `stdio`, local media files, and local native tools. A public directory submission therefore needs an additional cloud architecture:

```text
ChatGPT
   |
   | HTTPS streamable MCP
   v
public MCP service
   |
   +--> isolated job storage
   +--> downloader / caption worker
   +--> optional STT / OCR workers
   +--> retention and deletion controls
```

That cloud service also requires an HTTPS endpoint, production authentication where appropriate, a public privacy policy and terms URL, support contacts, monitoring, abuse controls, and marketplace review. It is a separate deployment project, not a manifest-only conversion.

## Claude Code

Claude Code can run the bundled local MCP server directly. Users can install it from GitHub through the included `.claude-plugin/marketplace.json`. Third-party marketplaces can be shared immediately; the official Anthropic marketplace requires a separate submission and review.

## MCP startup compatibility

Keep the shared `.mcp.json` relative to the installed plugin directory for Codex. Claude Code merges the inline `mcpServers` map from `.claude-plugin/plugin.json` over the default file. Its launcher uses `${CLAUDE_PLUGIN_ROOT}` directly in `args`, without `sh -c`, a shell default expression, or a session-dependent `cwd`.

Run `scripts/smoke_test_mcp.py --host codex` and `--host claude` with the prepared Python runtime before release. Also test the installed release in a new Claude session and verify a `youtube_probe` call.

## Versioning

Keep these versions synchronized for each release:

- `plugins/youtube-deep-analyzer/.codex-plugin/plugin.json`;
- `plugins/youtube-deep-analyzer/.claude-plugin/plugin.json`;
- `.claude-plugin/marketplace.json`;
- `CHANGELOG.md`;
- Git tag.

The Python dependency cache is keyed by the SHA-256 digest of `requirements.lock`, so dependency changes automatically create a new isolated runtime. `yt-dlp` may then use a newer checksum-verified overlay without rewriting plugin files.
