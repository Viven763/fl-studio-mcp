// Tiny macOS helper for UI steps the FL scripting API can't do.
//   flhelper click X Y        left-click at screen point
//   flhelper ocr IMAGE        print recognized text lines
//   flhelper windows OWNER    print "title|x,y,w,h|onscreen|layer" for each window of apps whose
//                             name starts with OWNER (WindowServer list: sees every desktop/Space,
//                             titles need the Screen Recording permission)
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
case "windows":
    let owner = args.count > 2 ? args[2] : "FL Studio"
    let opts: CGWindowListOption = [.optionAll, .excludeDesktopElements]
    let list = CGWindowListCopyWindowInfo(opts, kCGNullWindowID) as? [[String: Any]] ?? []
    for w in list {
        guard let name = w[kCGWindowOwnerName as String] as? String, name.hasPrefix(owner) else { continue }
        let title = (w[kCGWindowName as String] as? String ?? "").replacingOccurrences(of: "|", with: "/")
        let b = w[kCGWindowBounds as String] as? [String: Double] ?? [:]
        let on = (w[kCGWindowIsOnscreen as String] as? Bool) ?? false
        let layer = w[kCGWindowLayer as String] as? Int ?? 0
        print("\(title)|\(Int(b["X"] ?? 0)),\(Int(b["Y"] ?? 0)),\(Int(b["Width"] ?? 0)),\(Int(b["Height"] ?? 0))|\(on ? 1 : 0)|\(layer)")
    }
default:
    FileHandle.standardError.write("usage: flhelper click X Y | ocr IMAGE | windows OWNER\n".data(using: .utf8)!)
    exit(1)
}
