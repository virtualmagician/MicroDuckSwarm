// Renders the DuckSwarm Editor app icon as a 1024x1024 PNG.
//
//   swift scripts/launcher/make_icon.swift out.png
//
// AppKit only, no third-party code (CLAUDE.md rule 2). A dark rounded square
// on the macOS icon grid with the duck emoji on it, drawn into a bitmap of a
// fixed pixel size so the result does not depend on the display's scale.
// scripts/make_launcher_app.sh turns the PNG into an .icns with sips and
// iconutil.
import AppKit

let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "icon.png"
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

let duck = NSAttributedString(string: "🦆", attributes: [.font: NSFont.systemFont(ofSize: CGFloat(px) * 0.62)])
let s = duck.size()
duck.draw(at: NSPoint(x: (CGFloat(px) - s.width) / 2, y: (CGFloat(px) - s.height) / 2 + CGFloat(px) * 0.02))

NSGraphicsContext.restoreGraphicsState()

guard let png = rep.representation(using: .png, properties: [:]) else { fatalError("no PNG") }
do { try png.write(to: URL(fileURLWithPath: out)) } catch { fatalError("write failed: \(error)") }
print("wrote \(out) (\(px)x\(px))")
