# Contributing

1. Open an issue describing the behavior, evidence, and intended compatibility impact.
2. Keep caption-first behavior, selective STT, targeted visual inspection, and restriction-bypass prohibitions intact.
3. Add or update tests for every transport, transcript, STT, OCR, or filtering change.
4. Run `python3 scripts/validate_distribution.py` and the full unit test suite.
5. Keep both manifests and both marketplace catalogs synchronized.

Do not commit downloaded media, transcripts containing personal information, model files, dependency caches, cookies, credentials, or a local `vendor` directory.
