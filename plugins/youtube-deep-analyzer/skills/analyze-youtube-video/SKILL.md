---
name: analyze-youtube-video
description: Fetch and normalize permitted public YouTube captions as the primary transcript, use local STT only on short caption ranges where the intended word, term, or expression remains materially ambiguous, independently inspect only necessary visual ranges with local filtering and OCR, and create Markdown summaries, explanations, and transcripts. Use whenever a user provides a YouTube video or playlist and asks to watch, analyze, explain, organize, summarize, recap, transcribe, or take comprehensive notes.
---

# Analyze YouTube Video

Use the bundled `youtube-media` MCP tools. Captions are the default evidence spine. Local STT is optional lexical verification, not a mandatory transcript stage and not a prerequisite for visual inspection.

## Core rules

- Match the user's language unless requested otherwise.
- Call `youtube_probe`, then `youtube_prepare_transcript_bundle`.
- The bundle must return `execution_order=[captions]`, `media=null`, and `audio_transcription_performed=false`.
- Read the complete normalized captions before deciding whether STT or images are needed.
- Do not run STT merely because audio exists, captions are automatic, or a summary was requested.
- Run selective STT only when context cannot reliably distinguish an exact word, technical term, proper noun, number, negation, quotation, or expression and that distinction materially affects the explanation.
- Do not run STT for stylistic polishing, punctuation, obvious caption duplication, or wording that can safely remain paraphrased.
- If no caption track is available, do not silently start full-audio STT. Explain that the caption-first path is unavailable and ask whether the user wants an explicit full local transcription or visual-only analysis.
- When STT is justified, use the smallest practical ranges through `youtube_verify_caption_ranges`; never transcribe the entire audio unless the user explicitly requests a full audio-derived transcript.
- Visual inspection is an independent branch. Go directly from caption review to `youtube_prepare_visual_ranges` whenever missing screen context is the issue.
- Do not infer that lexical ambiguity requires an image, or that visual ambiguity requires STT.
- Do not identify speakers or analyze pitch, energy, pauses, speech rate, emphasis, emotion, laughter, music, or delivery.
- Use local OCR before requesting any image.
- Download only public YouTube media or content the user is authorized to access. Never bypass restrictions or supply credentials.
- Delete prepared media only after requested artifacts are written unless the user requests retention.

## Workflow

### 1. Establish scope and order

1. Call `youtube_probe`.
2. Record canonical URL, title, channel, duration, publication date, and caption availability.
3. When both `v` and `list` are present, treat the URL as a playlist unless the user limits it to the selected video.
4. For a playlist, create an immutable manifest with displayed position, video ID, canonical URL, title, duration, and availability.
5. Preserve displayed order unless explicit numbering, prerequisites, or narrative evidence proves another order. Disclose reordering.

### 2. Build the caption transcript first

1. Call `youtube_prepare_transcript_bundle` with exact caption preferences. Korean preference is normally `ko-orig -> ko -> en`.
2. Confirm that no audio or video was downloaded and no STT occurred.
3. Read all `transcript.segments` chronologically and merge only rolling-caption duplication already identified by the tool.
4. Treat `transcript.basis=normalized_youtube_captions` as the primary transcript basis.
5. Mark caption gaps and uncertain phrases. Do not automatically escalate every uncertainty.

### 3. Classify unresolved evidence independently

For each unresolved point, choose zero or more branches:

- **No extra evidence:** surrounding captions make the intended meaning sufficiently clear, or exact wording is unnecessary.
- **Selective STT:** exact audible wording materially matters and captions plus context cannot decide it.
- **Visual inspection:** the explanation depends on visible text, layout, chart, diagram, equation, code, demonstration, motion, or before/after evidence.
- **Both:** only when separate lexical and visual questions remain in the same span. Neither branch is a prerequisite for the other.

### 4. Verify only ambiguous caption ranges with STT

Before calling STT, record for each range:

- `start_seconds` and `end_seconds`, including only enough surrounding speech to recognize the phrase;
- the normalized `caption_text`;
- a precise `question`, such as “Was the product name X or Y?”;
- why the distinction changes the final explanation.

Call `youtube_verify_caption_ranges` only if at least one justified range exists. Ranges must be chronological and non-overlapping, each no longer than 90 seconds, with at most 20 ranges and at most 600 seconds total.

Use returned `stt_text` only to resolve the recorded lexical question. Preserve the caption transcript as the main transcript and annotate corrections or uncertainty. Do not replace unaffected captions with STT output. Record:

- `basis=normalized_youtube_captions_with_selective_local_stt_verification`;
- `full_audio_transcription_performed=false`;
- the exact verified ranges and conclusions.

If no range needs verification, do not call `youtube_stt_runtime_status`, download audio, or initialize the STT model.

### 5. Inspect visual context independently

Create a visual range when captions explicitly or implicitly depend on unseen screen content. Record the smallest practical range, exact visual question, `question_kind`, caption context, known text, and priority.

1. Call `youtube_prepare_visual_ranges` directly after caption review; prior STT is never required.
2. Read local OCR, novelty ratios, duplicate flags, decision reasons, and `image_model_recommended`.
3. Use reliable OCR directly for on-screen text.
4. Suppress a near duplicate only when pHash distance `<=0.03125`, aligned MAD `<=0.006`, edge change `<=0.04`, and OCR novel-token ratio `<=0.03`.
5. Call `youtube_get_frame` only for still-unresolved chart, diagram, spatial, equation, demonstration, motion, before/after, or unreliable-OCR meaning.
6. Use `youtube_get_frame_at` only when candidates miss the exact moment.
7. Use a whole-video visual pass only when caption review proves that unique visual meaning spans most of the video, and document why.

### 6. Write the artifacts

Write the analysis artifact in this order:

1. source and evidence scope;
2. compact summary;
3. 3–7 essential takeaways;
4. table of contents;
5. complete chronological or logical explanation with timestamps;
6. selective STT corrections only where they affected meaning;
7. inspected visual context only where used;
8. limitations, contradictions, and unresolved points;
9. link to the transcript.

Write `<safe-video-title>-transcript.md` with:

```markdown
# <Video title> — Caption transcript

- Source: <YouTube URL>
- Transcript basis: normalized YouTube captions
- Selective local STT: none | <verified ranges>
- Full audio transcription performed: no
- Visual evidence: none | targeted frames
- Language: <caption language>
- Notes: <gaps, corrections, and uncertainties>

## Transcript

### [00:00–02:15] <optional section label>

<normalized caption text>

> **STT verification [01:12–01:20]:** Caption “...” was checked as “...” because the exact term affected the conclusion.

> **Visual context [01:42]:** <only an inspected frame observation>
```

Omit the STT verification block when STT was not run.

### 7. Complete playlists without scrambling order

- Process sequentially when later items depend on earlier items or the series is narrative or instructional.
- Use bounded parallel processing only for independent videos.
- Assemble results by canonical playlist position, never completion order.
- Mark unavailable items without removing their positions.
- Make STT and visual decisions independently for every video; do not apply one video's need to the entire playlist.
- Create `<playlist-title>-artifact-index.md` and a playlist-wide synthesis.

## Completeness check

- captions remained the primary transcript basis;
- STT ran only for documented, materially ambiguous lexical ranges;
- no full-audio STT occurred unless explicitly requested;
- visual inspection did not wait for or depend on STT;
- no speaker guesses or acoustic analysis were added;
- every visual assertion comes from returned frames or local OCR;
- both requested Markdown artifacts exist before deletion.

## Required response order

1. Give a compact summary and essential takeaways.
2. Explain the full content structurally with timestamps and inspected evidence.
3. Link the analysis artifact.
4. Link the caption transcript artifact.
5. State whether selective STT or visual inspection was used and why.
