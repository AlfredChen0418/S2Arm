// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 S2Arm contributors
// Original artwork. Palette and wordmark follow docs/site/index.html.
// Finder supplies the draggable icons; they appear only in the review preview.
import AppKit

let assets = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
let root = assets.deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
let layout = try JSONSerialization.jsonObject(with: Data(contentsOf: assets.appendingPathComponent("dmg-layout.json"))) as! [String: Any]
let width = layout["width"] as! Int, height = layout["height"] as! Int
let positions = layout["iconPositions"] as! [String: [Int]]
let iconSize = CGFloat(layout["iconSize"] as! Int)
let output = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
let app = CommandLine.arguments.count > 2 ? CommandLine.arguments[2] : ""
try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)
let canvas = NSSize(width: width, height: height)

func color(_ hex: UInt32, _ alpha: CGFloat = 1) -> NSColor {
    NSColor(srgbRed: CGFloat((hex >> 16) & 255) / 255,
            green: CGFloat((hex >> 8) & 255) / 255,
            blue: CGFloat(hex & 255) / 255, alpha: alpha)
}
let ink = color(0xf2f0eb), muted = color(0xa3a09a), cream = color(0xf6ead2)
let line = color(0x2a2a2d), cyan = color(0x2cc8f0), red = color(0xe5484d)

func render(scale: Int, preview: Bool = false) -> NSBitmapImageRep {
    let cg = CGContext(data: nil, width: width * scale, height: height * scale,
                       bitsPerComponent: 8, bytesPerRow: width * scale * 4,
                       space: CGColorSpace(name: CGColorSpace.sRGB)!,
                       bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
    cg.translateBy(x: 0, y: CGFloat(height * scale))
    cg.scaleBy(x: CGFloat(scale), y: -CGFloat(scale))
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(cgContext: cg, flipped: true)
    defer { NSGraphicsContext.restoreGraphicsState() }

    func round(_ rect: NSRect, _ radius: CGFloat, _ fill: NSColor, _ stroke: NSColor? = nil) {
        let path = NSBezierPath(roundedRect: rect, xRadius: radius, yRadius: radius)
        fill.setFill(); path.fill()
        if let stroke { stroke.setStroke(); path.lineWidth = 1; path.stroke() }
    }
    func text(_ value: String, _ x: CGFloat, _ y: CGFloat, _ size: CGFloat,
              _ weight: NSFont.Weight = .regular, _ tint: NSColor = ink) {
        (value as NSString).draw(at: NSPoint(x: x, y: y), withAttributes: [
            .font: NSFont.systemFont(ofSize: size, weight: weight), .foregroundColor: tint
        ])
    }
    func rule(_ x1: CGFloat, _ y: CGFloat, _ x2: CGFloat, _ tint: NSColor = line) {
        let path = NSBezierPath(); path.move(to: NSPoint(x: x1, y: y)); path.line(to: NSPoint(x: x2, y: y))
        path.lineWidth = 1; tint.setStroke(); path.stroke()
    }
    func glow(_ x: CGFloat, _ y: CGFloat, _ radius: CGFloat, _ tint: NSColor) {
        let gradient = CGGradient(colorsSpace: CGColorSpace(name: CGColorSpace.sRGB),
                                  colors: [tint.cgColor, tint.withAlphaComponent(0).cgColor] as CFArray,
                                  locations: [0, 1])!
        cg.drawRadialGradient(gradient, startCenter: CGPoint(x: x, y: y), startRadius: 0,
                             endCenter: CGPoint(x: x, y: y), endRadius: radius, options: [])
    }
    func image(_ image: NSImage, _ rect: NSRect) {
        image.draw(in: rect, from: .zero, operation: .sourceOver, fraction: 1, respectFlipped: true, hints: nil)
    }

    color(0x0b0b0c).setFill(); NSRect(origin: .zero, size: canvas).fill()
    glow(100, -40, 460, color(0x756044, 0.14))
    let wordmark = NSImage(contentsOf: root.appendingPathComponent("docs/images/s2arm-wordmark.png"))!
    image(wordmark, NSRect(x: 40, y: 28, width: 122, height: 36))
    round(NSRect(x: 579, y: 34, width: 141, height: 26), 13, color(0x151516), line)
    text("macOS · Apple Silicon", 593, 40, 10, .medium, cream)
    rule(40, 82, 720)

    text("安装 S2Arm", 40, 105, 34, .bold)
    text("拖入右侧文件夹，即可安装。", 42, 155, 15, .regular, muted)
    text("Drag S2Arm into Applications to install.", 42, 179, 12, .regular, muted)

    for name in ["S2Arm.app", "Applications"] {
        let center = positions[name]!
        let x = CGFloat(center[0]), y = CGFloat(center[1])
        let card = NSRect(x: x - 118, y: y - 58, width: 236, height: 178)
        round(card, 16, color(0x151516), line)
        cg.saveGState(); NSBezierPath(roundedRect: card, xRadius: 16, yRadius: 16).addClip()
        glow(x, y, 130, (name == "S2Arm.app" ? red : cyan).withAlphaComponent(0.07))
        cg.restoreGState()
        // Finder uses black labels on image backgrounds, including in Dark Mode.
        // The cream plates keep the real labels readable without changing the app.
        round(NSRect(x: x - 88, y: y + 61, width: 176, height: 28), 14, cream)
        text(name == "S2Arm.app" ? "01  拖动应用 / DRAG" : "02  放入文件夹 / DROP",
             x - 78, y + 100, 10, .medium, muted)
        if preview {
            let rect = NSRect(x: x - iconSize / 2, y: CGFloat(center[1]) - iconSize / 2,
                              width: iconSize, height: iconSize)
            if name == "S2Arm.app", app.isEmpty {
                cg.saveGState()
                NSBezierPath(roundedRect: rect.insetBy(dx: 5, dy: 5), xRadius: 21, yRadius: 21).addClip()
                image(NSImage(contentsOf: assets.appendingPathComponent("AppIcon-1024.png"))!, rect.insetBy(dx: 5, dy: 5))
                cg.restoreGState()
            } else if name == "Applications" {
                // Read the system artwork directly: NSWorkspace's deferred folder
                // image can render blank in an offscreen command-line context.
                image(NSImage(contentsOfFile: "/System/Library/CoreServices/CoreTypes.bundle/Contents/Resources/ApplicationsFolderIcon.icns")!, rect)
            } else {
                image(NSWorkspace.shared.icon(forFile: app), rect)
            }
            let label = name as NSString
            let attributes: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 13), .foregroundColor: color(0x1a1712)]
            label.draw(at: NSPoint(x: x - label.size(withAttributes: attributes).width / 2, y: CGFloat(center[1]) + 64), withAttributes: attributes)
        }
    }
    let arrowY = CGFloat(positions["S2Arm.app"]![1])
    rule(322, arrowY, 438, color(0x5a5346))
    round(NSRect(x: 357, y: arrowY - 23, width: 46, height: 46), 23, cream)
    let arrow = NSBezierPath(); arrow.move(to: NSPoint(x: 369, y: arrowY)); arrow.line(to: NSPoint(x: 391, y: arrowY))
    arrow.move(to: NSPoint(x: 383, y: arrowY - 8)); arrow.line(to: NSPoint(x: 391, y: arrowY)); arrow.line(to: NSPoint(x: 383, y: arrowY + 8))
    arrow.lineWidth = 2; arrow.lineCapStyle = .round; arrow.lineJoinStyle = .round
    color(0x1a1712).setStroke(); arrow.stroke()

    rule(40, 409, 720)
    text("复制完成后，从「应用程序」打开 S2Arm。", 42, 430, 13, .medium)
    text("Once copied, open S2Arm from Applications.", 42, 452, 11, .regular, muted)
    text("MACOS 15+", 42, 491, 9, .semibold, muted)
    text("安装后可推出此磁盘 / Eject this disk after installing", 441, 491, 9, .regular, muted)

    let bitmap = NSBitmapImageRep(cgImage: cg.makeImage()!)
    bitmap.size = canvas
    return bitmap
}

let background = NSImage(size: canvas)
background.addRepresentation(render(scale: 1)); background.addRepresentation(render(scale: 2))
try background.tiffRepresentation!.write(to: output.appendingPathComponent("background.tiff"))
try render(scale: 2, preview: true).representation(using: .png, properties: [:])!.write(to: output.appendingPathComponent("preview.png"))
print("Created Retina installer artwork and preview: \(output.path)")
