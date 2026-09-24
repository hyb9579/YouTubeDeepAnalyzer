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
    |                              |   [local Apple Vision OCR]
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
