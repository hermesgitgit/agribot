// PDF → 繁中 OCR 文字（macOS 內建 PDFKit + Vision，離線、零外部依賴）
// 用法：ocr_pdf <pdf路徑>
// 輸出：每頁以 "\f<頁碼>\f" 分隔的純文字到 stdout（供 Python 切塊建索引）
//
// 建置（macOS）：swiftc -O scripts/ocr_pdf.swift -o scripts/ocr_pdf
// 註：編譯出的 ocr_pdf 是 macOS 專用二進位、已被 .gitignore；僅本機建知識庫時用，
//     不進容器（容器用預建好的 knowledge.db）。換機或更新時用上面指令重新編譯。
import Foundation
import PDFKit
import Vision
import CoreGraphics

let args = CommandLine.arguments
guard args.count >= 2 else { FileHandle.standardError.write("usage: ocr_pdf <pdf>\n".data(using: .utf8)!); exit(2) }

guard let doc = PDFDocument(url: URL(fileURLWithPath: args[1])) else {
    FileHandle.standardError.write("cannot open pdf\n".data(using: .utf8)!); exit(1)
}

let scale: CGFloat = 2.0  // ~144→288dpi 等效，平衡清晰度與速度

func ocrPage(_ page: PDFPage) -> String {
    let bounds = page.bounds(for: .mediaBox)
    let w = Int(bounds.width * scale), h = Int(bounds.height * scale)
    guard w > 0, h > 0,
          let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8,
                              bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(),
                              bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return "" }
    ctx.setFillColor(CGColor(gray: 1, alpha: 1))
    ctx.fill(CGRect(x: 0, y: 0, width: w, height: h))
    ctx.scaleBy(x: scale, y: scale)
    page.draw(with: .mediaBox, to: ctx)
    guard let cgImage = ctx.makeImage() else { return "" }

    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["zh-Hant", "en-US"]
    request.usesLanguageCorrection = true
    let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
    do { try handler.perform([request]) } catch { return "" }
    guard let obs = request.results else { return "" }
    return obs.compactMap { $0.topCandidates(1).first?.string }.joined(separator: "\n")
}

var out = ""
for i in 0..<doc.pageCount {
    guard let page = doc.page(at: i) else { continue }
    let text = ocrPage(page)
    out += "\u{0c}\(i + 1)\u{0c}\n" + text + "\n"
}
FileHandle.standardOutput.write(out.data(using: .utf8)!)
