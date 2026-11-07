# Architecture

## Evidence workflow

```text
[video or playlist URL]
          |
          v
[metadata probe + canonical order]
          |
          v
[caption acquisition and normalization] ---- failure ----> [explicit fallback choice]
          |
          v
[model reads complete caption transcript]
          |
          +---------------------------+
          |                           |
          v                           v
[lexical ambiguity?]            [visual meaning required?]
    | no      | yes                | no       | yes
    |         v                    |          v
    | [small audio range]          |   [small video range]
    |         |                    |          |
    | [local whisper.cpp STT]      |   [cheap duplicate filtering]
    |                              |          |
    |                              |   [local OCR engine + fallback]
    |                              |          |
    |                              |   [selected frame only if unresolved]
    +---------------+--------------+
                    |
                    v
      [summary + structured explanation + transcript]
```

## Runtime and data flow

```text
GitHub plugin files (small, immutable after install)
   |
   +--> first launch: pinned Python packages --> user cache
   +--> daily check: verified yt-dlp wheel ------> user cache overlay
   +--> optional STT: verified whisper model ---> user cache
   +--> each request: captions/media -----------> temporary job directory
                                                     |
                                                     +--> Markdown artifacts
                                                     +--> cleanup after completion
```

## Token and local-resource profile

| Stage | Model tokens | Network | Local CPU / time |
|---|---:|---:|---:|
| Metadata probe | Low | Low | Low |
| Caption download/normalization | None for processing | Low | Low |
| Reading and explaining captions | Main token cost | None | Low |
| Selective local STT | None during inference | Medium for audio/model | Medium to high, limited to selected ranges |
| Frame filtering | None | Medium for selected video ranges | Medium |
| Local OCR | None | None after frame download | Medium |
| Returned image interpretation | Image tokens | None | Used only for unresolved selected frames |

The design reduces redundant image tokens by making visual inspection conditional on transcript-derived questions, then applying local duplicate filtering and OCR before returning frames to the model.

## Trust boundaries

```text
untrusted YouTube input
   |
   v
URL validation -> public-content policy -> bounded downloader -> local job storage
                                                        |
                                                        v
                                              structured tool results
                                                        |
                                                        v
                                                   model analysis
```

Remote wheels and the optional STT model are accepted only from configured HTTPS hosts and verified with published SHA-256 digests where the updater provides a digest. YouTube access errors are classified without adding authentication or restriction-bypass behavior.

## Cross-platform startup and OCR

`launch_mcp.py` resolves Python and the pinned dependency runtime without a shell, then replaces its process with the MCP server. Codex supplies the installed plugin directory as its working directory; Claude substitutes the installed root in the launcher argument. The initial Python command must be visible on the host PATH.

`local_ocr.py` provides Apple Vision and Tesseract behind one interface. Automatic selection retries only failed Apple Vision frames with Tesseract, including per-image failures. Explicit selection does not switch engines. Missing language data is an error; it is not an instruction to silently switch to English. OCR failure leaves selected-frame interpretation available.

Visual cache keys include the requested OCR engine and languages. Model confidence scores remain engine-specific; they are not a cross-engine accuracy benchmark.
