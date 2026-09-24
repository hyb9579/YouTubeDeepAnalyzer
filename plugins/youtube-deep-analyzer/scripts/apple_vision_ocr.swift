import Foundation
import ImageIO
import Vision

enum OCRFailure: Error {
    case invalidArguments
    case imageLoad(String)
}

func loadImage(_ path: String) throws -> CGImage {
    let url = URL(fileURLWithPath: path)
    guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
          let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
        throw OCRFailure.imageLoad(path)
    }
    return image
}

var mode = "accurate"
var languages = ["ko-KR", "en-US"]
var paths: [String] = []
var index = 1
while index < CommandLine.arguments.count {
    let argument = CommandLine.arguments[index]
    if argument == "--mode", index + 1 < CommandLine.arguments.count {
        mode = CommandLine.arguments[index + 1]
        index += 2
    } else if argument == "--languages", index + 1 < CommandLine.arguments.count {
        languages = CommandLine.arguments[index + 1]
            .split(separator: ",")
            .map { String($0).trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        index += 2
    } else {
        paths.append(argument)
        index += 1
    }
}

guard (mode == "fast" || mode == "accurate"), !paths.isEmpty else {
    throw OCRFailure.invalidArguments
}

var output: [[String: Any]] = []
for path in paths {
    let started = CFAbsoluteTimeGetCurrent()
    do {
        let image = try loadImage(path)
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = mode == "fast" ? .fast : .accurate
        request.usesLanguageCorrection = mode == "accurate"
        request.recognitionLanguages = languages
        let handler = VNImageRequestHandler(cgImage: image, options: [:])
        try handler.perform([request])
        let candidates = (request.results ?? []).compactMap { $0.topCandidates(1).first }
        let confidence = candidates.isEmpty
            ? 0.0
            : candidates.reduce(0.0) { $0 + Double($1.confidence) } / Double(candidates.count)
        output.append([
            "path": path,
            "text": candidates.map(\.string).joined(separator: "\n"),
            "average_confidence": confidence,
            "observation_count": candidates.count,
            "elapsed_ms": (CFAbsoluteTimeGetCurrent() - started) * 1000.0,
            "engine": "apple-vision",
        ])
    } catch {
        output.append([
            "path": path,
            "text": "",
            "average_confidence": 0.0,
            "observation_count": 0,
            "elapsed_ms": (CFAbsoluteTimeGetCurrent() - started) * 1000.0,
            "engine": "apple-vision",
            "error": String(describing: error),
        ])
    }
}

let data = try JSONSerialization.data(withJSONObject: output, options: [.sortedKeys])
FileHandle.standardOutput.write(data)
