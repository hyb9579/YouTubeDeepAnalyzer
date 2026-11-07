# OCR licensing and alternatives

Reviewed 2026-11-07 against upstream sources. This records the integration's distribution assumptions, not a blanket approval of every third-party installer or model.

## Current integration

The plugin's own code remains under its MIT license. Tesseract is an optional external executable; no Tesseract binaries, Leptonica binaries, or OCR traineddata are committed or included in this release. The wrapper calls the executable through subprocesses. CI installs it through platform package managers.

- [Tesseract engine](https://github.com/tesseract-ocr/tesseract/blob/main/LICENSE): Apache-2.0.
- [Official tessdata](https://github.com/tesseract-ocr/tessdata/blob/main/LICENSE): Apache-2.0. Verify the exact data repository/release when selecting another model source.
- [Leptonica](https://github.com/DanBloomberg/leptonica/blob/master/leptonica-license.txt), used by Tesseract: permissive two-condition BSD-style terms. Binary builds can include additional libraries with their own notices.

Apache-2.0 permits commercial use, modification, and distribution. It does not require the separately developed plugin's source code to be disclosed. If a future release redistributes the engine or its language data, include the license, preserve applicable upstream notices, retain any required NOTICE attribution, and mark modified upstream files. Separately inspect the actual binary's dependencies and model artifacts. See [Apache-2.0 section 4](https://www.apache.org/licenses/LICENSE-2.0).

Keeping Tesseract external does not mean its license disappears. It keeps engine installation and its accompanying license materials with the selected package distribution. Do not silently bundle arbitrary Windows installers or replace their notices with the plugin's MIT license.

## Alternatives

| Candidate | Engineering fit | Remaining checks |
|---|---|---|
| Apple Vision | Existing macOS OCR; no separately downloaded OCR model in this plugin | Apple platform only; confidence and recognition quality vary by image |
| Tesseract | Existing cross-platform CLI integration, optional dependency, English/Korean data available | System installation and language data; benchmark low-resolution video text |
| RapidOCR with ONNX Runtime | Candidate for an optional neural OCR backend without requiring the full Paddle runtime | Pin a release, choose a Korean-capable model, inspect exact model terms, and measure package size, memory, speed, and accuracy |
| PaddleOCR | Candidate when broader text-detection/recognition models are needed | Larger dependency surface; select language-specific models and supported runtime wheels |
| EasyOCR | Python API and CPU inference are available | PyTorch/torchvision dependency and model downloads; inspect exact model artifacts |

Sources:

- [RapidOCR](https://github.com/RapidAI/RapidOCR) documents an Apache-2.0 code license and ONNX Runtime installation. Its README model-licensing link was not retrievable during this review; do not treat the code license alone as verification of a chosen weight file.
- [ONNX Runtime](https://github.com/microsoft/onnxruntime/blob/main/LICENSE) uses MIT.
- [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR/blob/main/LICENSE) uses Apache-2.0. The [official multilingual PP-OCRv5 documentation](https://www.paddleocr.ai/latest/en/version3.x/algorithm/PP-OCRv5/PP-OCRv5_multi_languages.html) lists a separate Korean-and-English recognition model; a generic default model is not proof of Korean support.
- [EasyOCR](https://github.com/JaidedAI/EasyOCR) documents Apache-2.0, PyTorch-based execution, CPU mode, and downloaded language models.

## Decision for this release

Retain Apple Vision and optional Tesseract behind one interface. Do not introduce another automatic model download or make a neural OCR framework mandatory. RapidOCR is an evaluation candidate, not an implemented backend or a measured winner.

Before changing the default, compare the same representative Korean/English slides, source-code screenshots, small captions, charts, and low-contrast frames. Measure character errors, meaningful numbers/negations, cold start, warm inference, memory, installation size, and how often a frame still needs model interpretation. Normalize evaluation criteria rather than assuming that engine confidence scores are interchangeable.
