// Tiny macOS helper for UI steps the FL scripting API can't do.
//   flhelper click X Y        left-click at screen point
//   flhelper ocr IMAGE        print recognized text lines
import AppKit
import Foundation
import Vision

let args = CommandLine.arguments
func post(_ type: CGEventType, _ p: CGPoint) {
    CGEvent(mouseEventSource: nil, mouseType: type, mouseCursorPosition: p, mouseButton: .left)?.post(tap: .cghidEventTap)
}
switch args.count > 1 ? args[1] : "" {
case "click":
    let p = CGPoint(x: Double(args[2])!, y: Double(args[3])!)
    post(.mouseMoved, p); usleep(80_000)
    post(.leftMouseDown, p); usleep(60_000)
    post(.leftMouseUp, p)
case "ocr":
    guard let img = NSImage(contentsOf: URL(fileURLWithPath: args[2])),
          let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else { exit(2) }
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.usesLanguageCorrection = false
    try VNImageRequestHandler(cgImage: cg).perform([req])
    for obs in req.results ?? [] { if let t = obs.topCandidates(1).first { print(t.string) } }
default:
    FileHandle.standardError.write("usage: flhelper click X Y | ocr IMAGE\n".data(using: .utf8)!)
    exit(1)
}
