// Renders the DuckSwarm Editor app icon as a 1024x1024 PNG.
//
//   swift scripts/launcher/make_icon.swift out.png [duck.png]
//
// With a second argument, that PNG (a MicroDuck cut out on transparency,
// rendered by scripts/launcher/render-duck.html from the real meshes) is
// drawn on the tile instead of the duck emoji. Without one the emoji stands
// in, so a checkout without the render still builds an icon.
//
// AppKit only, no third-party code (CLAUDE.md rule 2). A dark rounded square
// on the macOS icon grid with the MicroDuck on it (the render when given, the
// emoji otherwise), drawn into a bitmap of a fixed pixel size so the result
// does not depend on the display's scale. Prints which of the two it drew.
// scripts/make_launcher_app.sh turns the PNG into an .icns with sips and
// iconutil.
import AppKit

let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "icon.png"
let duckPath: String? = CommandLine.arguments.count > 2 ? CommandLine.arguments[2] : nil
let px = 1024

guard let rep = NSBitmapImageRep(
    bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px, bitsPerSample: 8,
    samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
    bytesPerRow: 0, bitsPerPixel: 0
) else { fatalError("could not allocate a bitmap") }
rep.size = NSSize(width: px, height: px)

guard let ctx = NSGraphicsContext(bitmapImageRep: rep) else { fatalError("no graphics context") }
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = ctx
ctx.cgContext.clear(CGRect(x: 0, y: 0, width: px, height: px))

// macOS icon grid: the rounded square sits inset from the 1024 canvas.
let inset = CGFloat(px) * 0.09
let square = NSRect(x: inset, y: inset, width: CGFloat(px) - 2 * inset, height: CGFloat(px) - 2 * inset)
let shape = NSBezierPath(roundedRect: square, xRadius: square.width * 0.225, yRadius: square.height * 0.225)

let top = NSColor(calibratedRed: 0.20, green: 0.24, blue: 0.32, alpha: 1)
let bottom = NSColor(calibratedRed: 0.08, green: 0.09, blue: 0.12, alpha: 1)
NSGradient(starting: top, ending: bottom)!.draw(in: shape, angle: -90)

// A thin lighter rim, the way system icons read at small sizes.
NSColor(calibratedWhite: 1, alpha: 0.10).setStroke()
shape.lineWidth = CGFloat(px) * 0.012
shape.stroke()

/// The opaque bounding box of an RGBA bitmap, in its own pixel coordinates
/// with y up (AppKit draw space). The render carries transparent margins, and
/// fitting the whole image would leave the duck small on the tile.
func opaqueBounds(_ rep: NSBitmapImageRep) -> NSRect? {
    // Exactly the layout the scan below assumes: interleaved RGBA, 8 bits per
    // sample, alpha last. Anything else returns nil and the caller says so
    // rather than drawing a box it did not measure.
    guard rep.hasAlpha, !rep.isPlanar, rep.samplesPerPixel == 4, rep.bitsPerSample == 8,
          !rep.bitmapFormat.contains(.alphaFirst), let data = rep.bitmapData else { return nil }
    let w = rep.pixelsWide, h = rep.pixelsHigh, stride = rep.bytesPerRow
    var minX = w, minY = h, maxX = -1, maxY = -1
    for y in 0..<h {
        let row = data + y * stride
        for x in 0..<w where row[x * 4 + 3] > 8 {
            if x < minX { minX = x }; if x > maxX { maxX = x }
            if y < minY { minY = y }; if y > maxY { maxY = y }
        }
    }
    guard maxX >= minX, maxY >= minY else { return nil }
    // Bitmap rows run top-down; NSImage.draw(from:) wants a rect with y up.
    return NSRect(x: minX, y: h - 1 - maxY, width: maxX - minX + 1, height: maxY - minY + 1)
}

var drewRender = false
if let duckPath, let duckImage = NSImage(contentsOfFile: duckPath),
   let duckRep = duckImage.representations.compactMap({ $0 as? NSBitmapImageRep }).first,
   let from = opaqueBounds(duckRep) {
    drewRender = true
    // Fit the opaque part inside the tile with a margin, keeping its aspect,
    // and sit it a little low so it reads as standing on the tile.
    let box = square.insetBy(dx: square.width * 0.10, dy: square.height * 0.10)
    let scale = min(box.width / from.width, box.height / from.height)
    let w = from.width * scale, h = from.height * scale
    let at = NSRect(x: box.midX - w / 2, y: box.midY - h / 2 - square.height * 0.015, width: w, height: h)
    // draw(in:from:) takes `from` in the image's own coordinate space, which
    // for a rep whose size equals its pixel size is the bitmap's pixels, y up.
    duckImage.size = NSSize(width: duckRep.pixelsWide, height: duckRep.pixelsHigh)
    duckImage.draw(in: at, from: from, operation: .sourceOver, fraction: 1)
} else {
    if let duckPath {
        let why = NSImage(contentsOfFile: duckPath) == nil ? "could not read it" : "not an interleaved RGBA8 bitmap with alpha, so its opaque bounds cannot be measured"
        FileHandle.standardError.write("warning: \(duckPath): \(why); using the emoji stand-in\n".data(using: .utf8)!)
    }
    let duck = NSAttributedString(string: "🦆", attributes: [.font: NSFont.systemFont(ofSize: CGFloat(px) * 0.62)])
    let s = duck.size()
    duck.draw(at: NSPoint(x: (CGFloat(px) - s.width) / 2, y: (CGFloat(px) - s.height) / 2 + CGFloat(px) * 0.02))
}

NSGraphicsContext.restoreGraphicsState()

guard let png = rep.representation(using: .png, properties: [:]) else { fatalError("no PNG") }
do { try png.write(to: URL(fileURLWithPath: out)) } catch { fatalError("write failed: \(error)") }
print("wrote \(out) (\(px)x\(px)) with \(drewRender ? "the MicroDuck render" : "the emoji stand-in")")
