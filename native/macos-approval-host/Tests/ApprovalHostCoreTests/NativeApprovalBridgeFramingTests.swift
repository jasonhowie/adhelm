import Foundation
import XCTest

@testable import ApprovalHostCore

final class NativeApprovalBridgeFramingTests: XCTestCase {
  func testSplitHeaderAndPayloadRequireEOFAndConsumeExactlyOnce() throws {
    let decoder = NativeApprovalBridgeFrameDecoder(role: .request)
    for chunk in [Data([0]), Data([0, 0]), Data([3, 0x61]), Data([0x62, 0x63])] {
      try decoder.append(chunk)
    }
    try decoder.finishAtEOF()
    XCTAssertEqual(try decoder.consume(), Data("abc".utf8))
    XCTAssertThrowsError(try decoder.consume())
    XCTAssertThrowsError(try decoder.append(Data([0, 0, 0, 1, 0x64])))
  }

  func testIncompleteFramesAndEarlyConsumptionPermanentlyFail() throws {
    for data in [Data(), Data([0, 0, 0]), Data([0, 0, 0, 2, 0x61])] {
      let decoder = NativeApprovalBridgeFrameDecoder(role: .request)
      try decoder.append(data)
      XCTAssertThrowsError(try decoder.finishAtEOF())
      XCTAssertThrowsError(try decoder.append(Data([0x62])))
      XCTAssertThrowsError(try decoder.consume())
    }
    let decoder = NativeApprovalBridgeFrameDecoder(role: .request)
    try decoder.append(Data([0, 0, 0, 1, 0x61]))
    XCTAssertThrowsError(try decoder.consume())
    XCTAssertThrowsError(try decoder.finishAtEOF())
  }

  func testTrailingByteAndSecondFrameFailWhetherInSameOrLaterChunk() throws {
    for extra in [Data([0]), Data([0, 0, 0, 1, 0x62])] {
      let together = NativeApprovalBridgeFrameDecoder(role: .request)
      XCTAssertThrowsError(try together.append(Data([0, 0, 0, 1, 0x61]) + extra))
      let separate = NativeApprovalBridgeFrameDecoder(role: .request)
      try separate.append(Data([0, 0, 0, 1, 0x61]))
      XCTAssertThrowsError(try separate.append(extra))
      XCTAssertThrowsError(try separate.finishAtEOF())
    }
  }

  func testUnsignedLengthsAreBoundedBeforePayloadAccumulation() throws {
    for role in [NativeApprovalBridgeFrameRole.request, .candidate] {
      for header in [Data([0, 0, 0, 0]), Data([255, 255, 255, 255])] {
        let decoder = NativeApprovalBridgeFrameDecoder(role: role)
        XCTAssertThrowsError(try decoder.append(header))
        XCTAssertThrowsError(try decoder.finishAtEOF())
      }
    }
    let request = NativeApprovalBridgeFrameDecoder(role: .request)
    XCTAssertThrowsError(try request.append(Data([0, 1, 128, 1])))
    let candidate = NativeApprovalBridgeFrameDecoder(role: .candidate)
    XCTAssertThrowsError(try candidate.append(Data([0, 2, 0, 1])))
    let oversized = NativeApprovalBridgeFrameDecoder(role: .candidate)
    XCTAssertThrowsError(try oversized.append(Data(repeating: 0, count: 131_077)))
  }

  func testMaximumRoleLengthsAndMutableInputIsolation() throws {
    for (role, header, size) in [
      (NativeApprovalBridgeFrameRole.request, Data([0, 1, 128, 0]), 98_304),
      (.candidate, Data([0, 2, 0, 0]), 131_072),
    ] {
      let decoder = NativeApprovalBridgeFrameDecoder(role: role)
      try decoder.append(header)
      let mutable = NSMutableData(data: Data(repeating: 0x61, count: size))
      try decoder.append(mutable as Data)
      mutable.resetBytes(in: NSRange(location: 0, length: mutable.length))
      try decoder.finishAtEOF()
      let result = try decoder.consume()
      XCTAssertEqual(result, Data(repeating: 0x61, count: size))
      XCTAssertEqual(String(reflecting: decoder), "NativeApprovalBridgeFrameDecoder(redacted)")
    }
  }

  func testTypedRequestEncoderUsesNetworkOrderLengthAndExactCanonicalBytes() throws {
    let path = try XCTUnwrap(
      Bundle.module.url(
        forResource: "openai-pause-challenge-v1", withExtension: "json", subdirectory: "Fixtures"))
    let request = try NativeApprovalBridgeRequestV1(
      requestID: Data(repeating: 0x32, count: 32),
      sourceContextDigest: String(repeating: "a", count: 64),
      metadataDigest: String(repeating: "b", count: 64),
      protocolConfigurationDigest: String(repeating: "c", count: 64),
      canonicalChallenge: Data(contentsOf: path))
    let frame = try NativeApprovalBridgeFrameEncoder.request(request)
    XCTAssertEqual(
      frame.prefix(4).reduce(UInt32(0)) { ($0 << 8) | UInt32($1) },
      UInt32(request.canonicalBytes.count))
    let decoder = NativeApprovalBridgeFrameDecoder(role: .request)
    try decoder.append(frame)
    try decoder.finishAtEOF()
    XCTAssertEqual(try decoder.consume(), request.canonicalBytes)
  }
}
