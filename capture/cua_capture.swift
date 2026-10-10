// cua_capture — ScreenCaptureKit 抓帧 → JPEG → stdout 帧流
//
// 输出协议（stdout 二进制，stderr 给人看）：每帧 = 4 字节大端长度 + JPEG 负载。
// 消费方（dsh 的 Host 半区）按长度取帧，不需要任何握手。
//
// 为什么用 SCK 而不是 `screencapture`：后者一帧要 fork 进程 + 编码 + 缩放，实测
// 154ms + 136ms ≈ 290ms，天花板 ~3fps；SCK 是推帧回调，实测全屏 Retina 29.5fps。
//
// 用法：cua_capture [--width 800] [--fps 30] [--quality 72] [--cursor 1] [--display 1]
//                   [--window <CGWindowID>]
//
// --window 给定时只抓那一扇窗（SCContentFilter(desktopIndependentWindow:)）——
// 观察窗于是显示的是「Agent 正在操作的那个窗口」，而不是整块桌面；窗口被关掉就退
// 回整屏，不至于没有画面。
// 生命周期：由父进程 spawn / kill，并用 --parent <pid> 探活；父进程消失时自己退出，
// 不留孤儿抓屏进程（抓屏进程活着 = 系统录屏灯常亮 + 持续吃 CPU，绝不能漏）。

import Accelerate
import CoreGraphics
import CoreMedia
import CoreVideo
import Darwin
import Dispatch
import Foundation
import ImageIO
import ScreenCaptureKit

struct Args {
    var width = 800
    var fps = 30
    var quality = 72
    var cursor = true
    var display = 1
    var parent = 0
    var window = 0
}

func parseArgs() -> Args {
    var a = Args()
    let argv = CommandLine.arguments
    var i = 1
    while i + 1 < argv.count {
        switch argv[i] {
        case "--width": a.width = Int(argv[i + 1]) ?? a.width
        case "--fps": a.fps = max(1, min(60, Int(argv[i + 1]) ?? a.fps))
        case "--quality": a.quality = max(30, min(95, Int(argv[i + 1]) ?? a.quality))
        case "--cursor": a.cursor = argv[i + 1] != "0"
        case "--display": a.display = Int(argv[i + 1]) ?? a.display
        case "--parent": a.parent = Int(argv[i + 1]) ?? a.parent
        case "--window": a.window = Int(argv[i + 1]) ?? a.window
        default: break
        }
        i += 2
    }
    return a
}

let args = parseArgs()
// SCStream 的 delegate 是弱引用，addStreamOutput 的 output 也不保证被强持有：
// 让它们进全局，否则回调对象先被释放，表现就是「流启动了但一帧都不来」。
var keepAlive: [AnyObject] = []
let out = FileHandle.standardOutput
let rgbSpace = CGColorSpace(name: CGColorSpace.sRGB)!
let jpegSink = NSMutableData()
var scratch: UnsafeMutableRawPointer?
var scratchBytes = 0
var frames = 0
var bytes = 0
let started = Date()

func writeFrame(_ data: Data) {
    var be = UInt32(data.count).bigEndian
    withUnsafeBytes(of: &be) { out.write(Data($0)) }
    out.write(data)
    frames += 1
    bytes += data.count + 4
}

/// CVImageBuffer → vImage 缩放到目标宽度 → ImageIO 编 JPEG。
///
/// 缩放原先走 CoreImage（CIContext），代价是把整条 GPU/Metal 管线都映射进来；
/// vImage 是 CPU SIMD，这个尺寸的缩放肉眼无差、helper 小一大截。SCK 这边已经把
/// pixelFormat 钉成 32BGRA，正好是 vImage_ARGB8888 + CGContext little-endian premultipliedFirst
/// 的布局，不需要再做格式转换。
func encode(_ buffer: CVPixelBuffer) -> Data? {
    let srcW = CVPixelBufferGetWidth(buffer), srcH = CVPixelBufferGetHeight(buffer)
    guard srcW > 1, srcH > 1,
        CVPixelBufferGetPixelFormatType(buffer) == kCVPixelFormatType_32BGRA,
        let base = CVPixelBufferGetBaseAddress(buffer)
    else { return nil }
    let dstW = max(2, min(args.width, srcW))
    let dstH = max(2, srcH * dstW / srcW)
    let rowBytes = dstW * 4
    let need = rowBytes * dstH
    if need > scratchBytes {
        if let p = scratch { free(p) }
        scratch = malloc(need)
        scratchBytes = need
    }
    guard let dstPtr = scratch else { return nil }
    var src = vImage_Buffer(
        data: base, height: vImagePixelCount(srcH), width: vImagePixelCount(srcW),
        rowBytes: CVPixelBufferGetBytesPerRow(buffer))
    var dst = vImage_Buffer(
        data: dstPtr, height: vImagePixelCount(dstH), width: vImagePixelCount(dstW), rowBytes: rowBytes)
    guard vImageScale_ARGB8888(&src, &dst, nil, vImage_Flags(kvImageNoFlags)) == kvImageNoError else { return nil }
    guard let ctx = CGContext(
        data: dstPtr, width: dstW, height: dstH, bitsPerComponent: 8, bytesPerRow: rowBytes,
        space: rgbSpace,
        bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue),
        let cg = ctx.makeImage()
    else { return nil }

    jpegSink.length = 0
    guard let dest = CGImageDestinationCreateWithData(jpegSink, "public.jpeg" as CFString, 1, nil)
    else { return nil }
    CGImageDestinationAddImage(
        dest, cg,
        [kCGImageDestinationLossyCompressionQuality: CGFloat(args.quality) / 100.0] as CFDictionary
    )
    guard CGImageDestinationFinalize(dest) else { return nil }
    return jpegSink as Data
}

final class Sink: NSObject, SCStreamOutput, SCStreamDelegate {
    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .screen, CMSampleBufferIsValid(sampleBuffer) else { return }
        guard let imageBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        CVPixelBufferLockBaseAddress(imageBuffer, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(imageBuffer, .readOnly) }
        guard let jpeg = encode(imageBuffer) else { return }
        writeFrame(jpeg)
        if frames == 1 {
            FileHandle.standardError.write("首帧到达: \(jpeg.count) 字节, 用时 \(String(format: "%.1f", Date().timeIntervalSince(started) * 1000))ms\n".data(using: .utf8)!)
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        FileHandle.standardError.write("SCK 流中断: \(error)\n".data(using: .utf8)!)
        exit(3)
    }
}

// `SCContentFilter(desktopIndependentWindow:)` 内部会走 CGS 连接，连接没建好时
// 直接 `Assertion failed: (did_initialize), function CGS_REQUIRE_INIT` 把进程打死
// （整屏那条路径不会，所以只在窗口模式下需要先把连接建立起来）。
_ = CGMainDisplayID()
_ = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)

SCShareableContent.getWithCompletionHandler { content, error in
    // 失败必须**非零退出**：只 signal 一个没人等的信号量的话，进程会活着但一帧不出，
    // 宿主以为流正常就永远不回落，观察窗变成永久冻帧。
    if let error = error {
        FileHandle.standardError.write("SCK 拿不到可共享内容（屏幕录制未授予？）: \(error)\n".data(using: .utf8)!)
        exit(2)
    }
    guard let content = content else {
        FileHandle.standardError.write("SCK 返回空内容\n".data(using: .utf8)!)
        exit(2)
    }
    let index = max(0, min(content.displays.count - 1, args.display - 1))
    let display = content.displays[index]

    // 目标窗口在屏幕上的实际尺寸决定画面比例；找不到（关掉了 / id 过期）就退回整屏。
    var sourceRatio = Double(display.width) / Double(display.height)
    var filter: SCContentFilter
    if args.window > 0, let win = content.windows.first(where: { $0.windowID == UInt32(args.window) }) {
        let b = win.frame
        if b.width > 1, b.height > 1 { sourceRatio = Double(b.width) / Double(b.height) }
        filter = SCContentFilter(desktopIndependentWindow: win)
        FileHandle.standardError.write("按窗口抓取 #\(args.window): \(Int(b.width))x\(Int(b.height)) \(win.title ?? "")\n".data(using: .utf8)!)
    } else {
        if args.window > 0 {
            FileHandle.standardError.write("窗口 #\(args.window) 不存在，退回整屏\n".data(using: .utf8)!)
        }
        filter = SCContentFilter(display: display, excludingWindows: [])
    }

    let config = SCStreamConfiguration()
    config.width = args.width
    config.height = max(1, Int(Double(args.width) / sourceRatio))
    config.minimumFrameInterval = CMTime(value: 1, timescale: Int32(args.fps))
    config.queueDepth = 6
    config.showsCursor = args.cursor
    config.pixelFormat = kCVPixelFormatType_32BGRA
    let sink = Sink()
    keepAlive.append(sink)
    let stream = SCStream(filter: filter, configuration: config, delegate: sink)
    keepAlive.append(stream)
    do {
        try stream.addStreamOutput(sink, type: .screen, sampleHandlerQueue: DispatchQueue(label: "cua.capture"))
        try stream.startCapture()
    } catch {
        FileHandle.standardError.write("启动抓屏失败: \(error)\n".data(using: .utf8)!)
        exit(3)
    }
    FileHandle.standardError.write(
        "cua_capture 已启动: \(config.width)x\(config.height) @\(args.fps)fps quality=\(args.quality)\n".data(using: .utf8)!
    )

    // 父进程没了就自己收：kill(pid,0) 探活，2 秒一次。
    if args.parent > 0 {
        let parentPid = pid_t(args.parent)
        Thread.detachNewThread {
            while true {
                Thread.sleep(forTimeInterval: 2)
                if kill(parentPid, 0) != 0 {
                    try? stream.stopCapture()
                    exit(0)
                }
            }
        }
    }

    // 每 5 秒把吞吐打到 stderr，方便实机验收帧率与码率。
    Thread.detachNewThread {
        var lastFrames = 0
        while true {
            Thread.sleep(forTimeInterval: 5)
            let d = frames - lastFrames
            lastFrames = frames
            let secs = Date().timeIntervalSince(started)
            FileHandle.standardError.write(
                "fps≈\(String(format: "%.1f", Double(d) / 5.0)) 累计\(frames)帧 平均\(String(format: "%.1f", Double(bytes) / Double(max(1, frames)) / 1024))KB/帧 运行\(String(format: "%.0f", secs))s\n".data(using: .utf8)!
            )
        }
    }
}

// SCK 的回调链要主 runloop 喂：`dispatchMain()` 只跑主队列、不跑 runloop，
// 表现就是 startCapture 成功、delegate 一个帧都不回。
RunLoop.main.run()
