// Free, on-device text recognition with Apple's Vision framework (macOS 10.15+).
// Usage: vision_ocr <image-path>   ->   prints {"lines":[{"text","conf","x","y","w","h"}]}
// Coordinates are normalised 0..1 with origin at the BOTTOM-left (Vision convention).
import AppKit
import Foundation
import Vision

func fail(_ msg: String) -> Never {
    let safe = msg.replacingOccurrences(of: "\"", with: "'")
    print("{\"error\":\"\(safe)\"}")
    exit(1)
}

guard CommandLine.arguments.count > 1 else { fail("usage: vision_ocr <image>") }
let path = CommandLine.arguments[1]
guard let image = NSImage(contentsOfFile: path),
      let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    fail("cannot open image \(path)")
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = false  // card names are not dictionary words
request.recognitionLanguages = ["fr-FR", "en-US"]

do {
    try VNImageRequestHandler(cgImage: cg, options: [:]).perform([request])
} catch {
    fail("vision error: \(error.localizedDescription)")
}

var lines: [[String: Any]] = []
for case let obs as VNRecognizedTextObservation in (request.results ?? []) {
    guard let best = obs.topCandidates(1).first else { continue }
    let b = obs.boundingBox
    lines.append([
        "text": best.string,
        "conf": Double(best.confidence),
        "x": Double(b.origin.x), "y": Double(b.origin.y),
        "w": Double(b.size.width), "h": Double(b.size.height),
    ])
}

guard let data = try? JSONSerialization.data(withJSONObject: ["lines": lines]),
      let text = String(data: data, encoding: .utf8) else { fail("json error") }
print(text)
