import Foundation

/// Fixed framing roles for the installed helper's private anonymous pipes.
/// Framing does not validate a request, prove a native observation, or grant
/// authority. The typed V1 and V2 validators remain mandatory after
/// consumption.
package enum NativeApprovalBridgeFrameRole: Sendable {
  case request
  case candidate
  case metaCampaignPauseV2Request
  case metaCampaignPauseV2Candidate

  fileprivate var maximumPayloadBytes: Int {
    switch self {
    case .request: NativeApprovalBridgeRequestV1.maximumWireBytes
    case .candidate: NativeApprovalBridgeCandidateV1.maximumWireBytes
    case .metaCampaignPauseV2Request:
      NativeMetaCampaignPauseApprovalBridgeRequestV2.maximumWireBytes
    case .metaCampaignPauseV2Candidate:
      NativeMetaCampaignPauseApprovalBridgeCandidateV2.maximumWireBytes
    }
  }
}

package enum NativeApprovalBridgeFramingError: Error, Equatable, Sendable {
  case invalidFrame
}

/// One bounded frame followed by EOF, consumed at most once. No caller can
/// select an arbitrary size limit or recover a decoder after a framing error.
/// The synchronous helper owns this object; it is intentionally not Sendable.
package final class NativeApprovalBridgeFrameDecoder: CustomStringConvertible,
  CustomDebugStringConvertible
{
  private enum State { case receiving, awaitingEOF, complete, consumed, failed }
  private let maximumPayloadBytes: Int
  private var bytes = Data()
  private var expectedPayloadBytes: Int?
  private var state = State.receiving

  package init(role: NativeApprovalBridgeFrameRole) {
    maximumPayloadBytes = role.maximumPayloadBytes
  }

  package var description: String { "NativeApprovalBridgeFrameDecoder(redacted)" }
  package var debugDescription: String { description }

  package func append(_ incoming: Data) throws {
    guard state == .receiving || state == .awaitingEOF,
      incoming.count <= maximumPayloadBytes + 4 - bytes.count,
      state != .awaitingEOF || incoming.isEmpty
    else { throw fail() }
    if incoming.isEmpty { return }
    incoming.withUnsafeBytes { buffer in
      bytes.append(contentsOf: buffer.bindMemory(to: UInt8.self))
    }
    if expectedPayloadBytes == nil, bytes.count >= 4 {
      let prefix = bytes.prefix(4)
      let length = prefix.reduce(UInt32(0)) { ($0 << 8) | UInt32($1) }
      guard length > 0, length <= maximumPayloadBytes else { throw fail() }
      expectedPayloadBytes = Int(length)
    }
    if let expectedPayloadBytes {
      guard bytes.count <= expectedPayloadBytes + 4 else { throw fail() }
      if bytes.count == expectedPayloadBytes + 4 { state = .awaitingEOF }
    }
  }

  package func finishAtEOF() throws {
    guard state == .awaitingEOF else { throw fail() }
    state = .complete
  }

  package func consume() throws -> Data {
    guard state == .complete else { throw fail() }
    state = .consumed
    let result = bytes.withUnsafeBytes { buffer in
      Data(bytes: buffer.baseAddress!.advanced(by: 4), count: buffer.count - 4)
    }
    bytes.removeAll(keepingCapacity: false)
    return result
  }

  private func fail() -> NativeApprovalBridgeFramingError {
    state = .failed
    bytes.removeAll(keepingCapacity: false)
    return .invalidFrame
  }
}

package enum NativeApprovalBridgeFrameEncoder {
  package static func request(_ value: NativeApprovalBridgeRequestV1) throws -> Data {
    try encode(value.canonicalBytes, maximumBytes: NativeApprovalBridgeRequestV1.maximumWireBytes)
  }

  package static func candidate(_ value: NativeApprovalBridgeCandidateV1) throws -> Data {
    try encode(value.canonicalBytes, maximumBytes: NativeApprovalBridgeCandidateV1.maximumWireBytes)
  }

  package static func metaCampaignPauseV2Request(
    _ value: NativeMetaCampaignPauseApprovalBridgeRequestV2
  ) throws -> Data {
    try encode(
      value.canonicalBytes,
      maximumBytes: NativeMetaCampaignPauseApprovalBridgeRequestV2.maximumWireBytes
    )
  }

  package static func metaCampaignPauseV2Candidate(
    _ value: NativeMetaCampaignPauseApprovalBridgeCandidateV2
  ) throws -> Data {
    try encode(
      value.canonicalBytes,
      maximumBytes: NativeMetaCampaignPauseApprovalBridgeCandidateV2.maximumWireBytes
    )
  }

  private static func encode(_ payload: Data, maximumBytes: Int) throws -> Data {
    guard !payload.isEmpty, payload.count <= maximumBytes else {
      throw NativeApprovalBridgeFramingError.invalidFrame
    }
    let length = UInt32(payload.count)
    var bytes = Data([
      UInt8(truncatingIfNeeded: length >> 24), UInt8(truncatingIfNeeded: length >> 16),
      UInt8(truncatingIfNeeded: length >> 8), UInt8(truncatingIfNeeded: length),
    ])
    payload.withUnsafeBytes { buffer in
      bytes.append(contentsOf: buffer.bindMemory(to: UInt8.self))
    }
    return bytes
  }
}
