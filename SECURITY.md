# Security policy

## Supported version

Security fixes are applied to the latest tagged release.

## Reporting a vulnerability

Do not disclose an exploitable vulnerability in a public issue. Use GitHub's private vulnerability reporting feature after the repository is published. Until a repository owner enables that feature, contact the publisher through a private channel listed in the GitHub profile.

Include the affected version, environment, reproduction steps, impact, and any proposed mitigation. Do not include credentials, private video data, or copyrighted media samples that you are not authorized to share.

## Dependency and download controls

- Python dependencies are fully version-pinned and installed into a user cache.
- The `yt-dlp` overlay accepts a stable universal wheel from approved PyPI hosts and verifies the PyPI-provided SHA-256 digest.
- The optional STT model is accepted only from its configured HTTPS location and verified against a pinned SHA-256 digest.
- The plugin intentionally contains no authentication or access-control bypass features.
