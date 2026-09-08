#!/usr/bin/env swift
//
// Generates FinanceApp's app icon: two overlapping coins, the front one bearing "$" and the
// one behind it "₹" — a multi-currency tracker, said in one mark.
//
// Usage, from the repo root:
//   swift macos/scripts/make_icon.swift
// Writes macos/Resources/AppIcon.iconset and macos/Resources/AppIcon.icns.
//
// Why two coins rather than one glyph: the app is genuinely multi-currency (USD and INR are
// both first-class), and a single "$" would claim otherwise. Why coins rather than the two
// glyphs side by side: at 16px in the menu bar or the Dock's smallest state, glyph detail is
// gone and only the silhouette survives — two overlapping discs still read as money, whereas
// two adjacent glyphs collapse into a smudge.
//
// Colours are the app's own: the income aqua from the validated categorical palette, into a
// deeper step of the same hue. A finance icon in the colour the app already uses for "money
// in" ties the two together, and one hue keeps it calm at every size.
//
// Drawn with CoreGraphics rather than shipped as a rasterised asset so every size is rendered
// at its own scale — the stroke weights and the coin gap are proportional, so the 16px version
// is a *drawing* rather than a downsample, which is where small icons usually turn to mud.

import AppKit

// MARK: - Palette

/// Slot 3 of the categorical palette (light step), the app's "money in".
let aqua = NSColor(srgbRed: 0x1b / 255, green: 0xaf / 255, blue: 0x7a / 255, alpha: 1)
/// A deeper step of the same hue, for the gradient's far corner.
let deepTeal = NSColor(srgbRed: 0x0b / 255, green: 0x5c / 255, blue: 0x45 / 255, alpha: 1)
/// The coin face. Very slightly warm, so it sits with the app's cream light theme rather than
/// looking like a pure-white sticker.
let coinFace = NSColor(srgbRed: 0xff / 255, green: 0xfd / 255, blue: 0xf7 / 255, alpha: 1)

// MARK: - Drawing

func drawIcon(size: CGFloat) -> NSImage {
    let image = NSImage(size: NSSize(width: size, height: size))
    image.lockFocus()
    let ctx = NSGraphicsContext.current!.cgContext
    let s = size

    // macOS icons sit inside their canvas rather than filling it; ~6% inset with a ~22%
    // corner radius is the modern squircle proportion.
    let inset = s * 0.06
    let plate = CGRect(x: inset, y: inset, width: s - 2 * inset, height: s - 2 * inset)

    ctx.saveGState()
    ctx.addPath(CGPath(roundedRect: plate, cornerWidth: s * 0.22, cornerHeight: s * 0.22, transform: nil))
    ctx.clip()
    let gradient = CGGradient(
        colorsSpace: CGColorSpaceCreateDeviceRGB(),
        colors: [aqua.cgColor, deepTeal.cgColor] as CFArray,
        locations: [0, 1]
    )!
    ctx.drawLinearGradient(
        gradient, start: CGPoint(x: 0, y: s), end: CGPoint(x: s, y: 0), options: []
    )
    ctx.restoreGState()

    // Everything from here on is clipped to the plate, so no coin can bulge past the
    // squircle's corner. The first render did exactly that: the front coin's lower-right
    // crossed the edge and left a bump on the silhouette.
    ctx.saveGState()
    ctx.addPath(CGPath(roundedRect: plate, cornerWidth: s * 0.22, cornerHeight: s * 0.22, transform: nil))
    ctx.clip()

    // Two coins on a diagonal, the front one lower-right so the pair reads left-to-right.
    // Sized to leave a clear margin inside the plate rather than to fill it.
    let coinRadius = s * 0.195
    let spread = s * 0.093
    let backCentre = CGPoint(x: s / 2 - spread, y: s / 2 + spread * 0.82)
    let frontCentre = CGPoint(x: s / 2 + spread, y: s / 2 - spread * 0.82)

    /// The coin behind, dimmer so depth is carried by value and not only by overlap.
    drawCoin(
        ctx: ctx, centre: backCentre, radius: coinRadius, size: s,
        face: coinFace.withAlphaComponent(0.55), glyph: "₹",
        glyphColour: deepTeal.withAlphaComponent(0.75),
        glyphOffset: CGPoint(x: -coinRadius * 0.14, y: coinRadius * 0.12)
    )

    // A gap in the *background* colour punched around the front coin before drawing it, so
    // the two coins never touch. Overlapping marks of the same value read as one blob
    // without a separator; the gap is proportional so it survives to 16px.
    ctx.saveGState()
    let gap = max(s * 0.018, 1)
    ctx.setBlendMode(.destinationOut)
    ctx.addEllipse(
        in: CGRect(
            x: frontCentre.x - coinRadius - gap, y: frontCentre.y - coinRadius - gap,
            width: (coinRadius + gap) * 2, height: (coinRadius + gap) * 2
        )
    )
    ctx.fillPath()
    ctx.restoreGState()
    // The punch above also removed the gradient, so restore it inside the gap ring only.
    ctx.saveGState()
    ctx.addEllipse(
        in: CGRect(
            x: frontCentre.x - coinRadius - gap, y: frontCentre.y - coinRadius - gap,
            width: (coinRadius + gap) * 2, height: (coinRadius + gap) * 2
        )
    )
    ctx.clip()
    ctx.addPath(CGPath(roundedRect: plate, cornerWidth: s * 0.22, cornerHeight: s * 0.22, transform: nil))
    ctx.clip()
    ctx.drawLinearGradient(
        gradient, start: CGPoint(x: 0, y: s), end: CGPoint(x: s, y: 0), options: []
    )
    ctx.restoreGState()

    drawCoin(
        ctx: ctx, centre: frontCentre, radius: coinRadius, size: s,
        face: coinFace, glyph: "$", glyphColour: deepTeal
    )

    ctx.restoreGState()
    image.unlockFocus()
    return image
}

func drawCoin(
    ctx: CGContext, centre: CGPoint, radius: CGFloat, size s: CGFloat,
    face: NSColor, glyph: String, glyphColour: NSColor,
    /// Nudges the glyph inside its coin. The back coin's lower-right is covered by the front
    /// one, so its glyph is shifted up-left to stay in the visible crescent.
    glyphOffset: CGPoint = .zero
) {
    ctx.saveGState()
    ctx.setFillColor(face.cgColor)
    ctx.addEllipse(
        in: CGRect(x: centre.x - radius, y: centre.y - radius, width: radius * 2, height: radius * 2)
    )
    ctx.fillPath()
    ctx.restoreGState()

    // Below about 40px the glyph is smaller than a few pixels and turns to mud; the coin
    // silhouette alone carries the icon there. Drawing it anyway is what makes small icons
    // look dirty rather than simple.
    guard s >= 40 else { return }

    let pointSize = radius * 1.5
    let font = NSFont.systemFont(ofSize: pointSize, weight: .semibold)
    let attributed = NSAttributedString(
        string: glyph,
        attributes: [.font: font, .foregroundColor: glyphColour]
    )
    let bounds = attributed.size()
    NSGraphicsContext.saveGraphicsState()
    attributed.draw(
        at: NSPoint(
            x: centre.x - bounds.width / 2 + glyphOffset.x,
            y: centre.y - bounds.height / 2 + glyphOffset.y
        )
    )
    NSGraphicsContext.restoreGraphicsState()
}

// MARK: - Export

func png(_ image: NSImage, _ px: Int) -> Data {
    let rep = NSBitmapImageRep(
        bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0
    )!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    image.draw(in: NSRect(x: 0, y: 0, width: px, height: px))
    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])!
}

let resources = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
    .appendingPathComponent("macos/Resources")
let iconset = resources.appendingPathComponent("AppIcon.iconset")
try? FileManager.default.createDirectory(at: iconset, withIntermediateDirectories: true)

let specs: [(String, Int)] = [
    ("icon_16x16", 16), ("icon_16x16@2x", 32),
    ("icon_32x32", 32), ("icon_32x32@2x", 64),
    ("icon_128x128", 128), ("icon_128x128@2x", 256),
    ("icon_256x256", 256), ("icon_256x256@2x", 512),
    ("icon_512x512", 512), ("icon_512x512@2x", 1024),
]
for (name, px) in specs {
    // Rendered at its own size, not scaled from one master.
    try! png(drawIcon(size: CGFloat(px)), px)
        .write(to: iconset.appendingPathComponent("\(name).png"))
}
print("wrote \(iconset.path)")
