# Privacy notice

Last updated: 2026-08-23

YouTube Deep Analyzer is a local-first open-source plugin. The repository publisher does not operate a backend service and does not receive video URLs, captions, media, transcripts, or generated artifacts through this software.

## Data processed

The plugin may process:

- public YouTube URLs, metadata, captions, audio ranges, and video ranges;
- locally generated OCR and selective STT results;
- local Markdown summaries, explanations, and transcripts;
- local diagnostic records needed to classify download failures.

## Where data goes

- YouTube receives normal requests needed to retrieve public metadata, captions, and selected media.
- PyPI is contacted on first dependency setup and for bounded `yt-dlp` update checks.
- Hugging Face is contacted only if selective local STT needs the configured model and it is not already cached.
- The installed AI host processes MCP tool results and user prompts under that host's own privacy terms.

## Local retention

Dependencies, the verified `yt-dlp` overlay, and the optional STT model are cached locally. Per-video media is stored in a temporary local job directory and should be deleted after requested artifacts are written unless the user requests retention. Generated Markdown artifacts remain where the user saves them.

## Credentials and restricted content

The plugin does not request or store YouTube credentials and does not implement cookie injection, login bypass, DRM bypass, paywall bypass, or restriction evasion.

## Operator notice

If a third party hosts a modified remote version of this plugin, that operator must publish a privacy notice describing its own collection, retention, subprocessors, security, and deletion process. This local privacy notice does not cover such a service.
