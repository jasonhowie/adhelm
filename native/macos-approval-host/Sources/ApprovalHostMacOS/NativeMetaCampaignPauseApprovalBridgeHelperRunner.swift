import ApprovalHostCore
import Foundation

/// Closed client seam for the fixed Meta campaign PAUSE V2 helper. It cannot
/// send a raw receipt or V1 request, and it exposes no selector, endpoint,
/// key, session, or transport input.
protocol NativeMetaCampaignPauseApprovalBridgeHelperClient: AnyObject {
  func request(
    _ request: NativeMetaCampaignPauseApprovalBridgeRequestV2,
    completion: @escaping (NativeMetaCampaignPauseApprovalBridgeXPCResponse) -> Void
  )

  func invalidate()
}

/// The V2 helper adds only typed-client construction to the common fixed pipe
/// operations. Its request/candidate role is fixed by this protocol and never
/// selected from stdin, the environment, or an executable argument.
protocol NativeMetaCampaignPauseApprovalBridgeHelperOperations:
  NativeApprovalBridgeHelperPipeOperations
{
  func makeClient(
    metadata: ApprovalHostBuildMetadataV1
  ) -> any NativeMetaCampaignPauseApprovalBridgeHelperClient
}

/// Synchronously exchanges one exact framed Meta campaign PAUSE V2 request
/// for one exact framed V2 candidate. It is package-only helper mechanics:
/// it performs no installation or enrollment and itself does not implement or
/// expose authentication, protected-store reads, signing, nonce consumption,
/// provider contact, or a generic pipe route. It forwards only one fixed typed
/// V2 request to the separate client; a future configured Mach-service
/// connection may activate that fixed service.
package final class NativeMetaCampaignPauseApprovalBridgeHelperRunner {
  private let metadata: ApprovalHostBuildMetadataV1
  private let operations: any NativeMetaCampaignPauseApprovalBridgeHelperOperations
  private var previousMonotonic: Double?
  private var deadline: Double?
  private var previousUTC: Date?
  private var started = false

  init(
    metadata: ApprovalHostBuildMetadataV1,
    operations: any NativeMetaCampaignPauseApprovalBridgeHelperOperations
  ) {
    self.metadata = metadata
    self.operations = operations
  }

  package static func run(metadata: ApprovalHostBuildMetadataV1) throws {
    let runner = NativeMetaCampaignPauseApprovalBridgeHelperRunner(
      metadata: metadata,
      operations: SystemNativeMetaCampaignPauseApprovalBridgeHelperOperations()
    )
    try runner.run(
      argumentCount: CommandLine.arguments.count,
      environment: ProcessInfo.processInfo.environment
    )
  }

  func run(argumentCount: Int, environment: [String: String]) throws {
    guard !started else { throw ApprovalHostError.transportUnavailable }
    started = true
    guard argumentCount == 1,
      environment == ["LANG": "C", "LC_ALL": "C", "TZ": "UTC"]
    else { throw ApprovalHostError.transportUnavailable }
    try checkpoint()
    try operations.prepareStandardPipes()
    try operations.validateCurrentClientIdentity(metadata: metadata)
    try checkpoint()
    let decoder = NativeApprovalBridgeFrameDecoder(role: .metaCampaignPauseV2Request)
    while true {
      try checkpoint()
      guard try operations.outputPeerPresent() else { throw ApprovalHostError.transportUnavailable }
      guard let chunk = try operations.readRequestChunk(timeoutMilliseconds: 50) else { continue }
      if chunk.isEmpty {
        try decoder.finishAtEOF()
        break
      }
      guard chunk.count <= 16_384 else { throw ApprovalHostError.transportUnavailable }
      try decoder.append(chunk)
    }
    let request = try NativeMetaCampaignPauseApprovalBridgeRequestV2.decode(decoder.consume())
    let exchangeStartedAt = try utcNow()
    let challenge = try request.boundChallenge(
      metadata: metadata,
      exchangeStartedAt: exchangeStartedAt
    )
    try checkpoint()
    guard try operations.outputPeerPresent() else { throw ApprovalHostError.transportUnavailable }
    let client = operations.makeClient(metadata: metadata)
    defer { client.invalidate() }
    let response = MetaCampaignPauseHelperResponseSlot()
    defer { response.close() }
    guard try operations.outputPeerPresent() else { throw ApprovalHostError.transportUnavailable }
    client.request(request) { response.complete($0) }
    while !response.hasResponse {
      try checkpoint()
      guard try operations.outputPeerPresent() else { throw ApprovalHostError.transportUnavailable }
      guard try utcNow() < challenge.expiresAt else { throw ApprovalHostError.challengeExpired }
      operations.waitForResponseTick()
    }
    try checkpoint()
    guard case .candidate(let supplied) = try response.consume() else {
      throw ApprovalHostError.transportUnavailable
    }
    let now = try utcNow()
    let candidate = try NativeMetaCampaignPauseApprovalBridgeCandidateV2.decode(
      supplied.canonicalBytes,
      expectedRequest: request,
      metadata: metadata,
      exchangeStartedAt: exchangeStartedAt,
      currentNow: now
    )
    let frame = try NativeApprovalBridgeFrameEncoder.metaCampaignPauseV2Candidate(candidate)
    var offset = 0
    while offset < frame.count {
      try checkpoint()
      guard try utcNow() < challenge.expiresAt, try operations.outputPeerPresent() else {
        throw ApprovalHostError.transportUnavailable
      }
      let chunk = Data(frame[offset..<min(offset + 16_384, frame.count)])
      guard let written = try operations.writeCandidateChunk(chunk, timeoutMilliseconds: 50) else {
        continue
      }
      guard written > 0, written <= chunk.count else {
        throw ApprovalHostError.transportUnavailable
      }
      offset += written
    }
    // There is no second frame or acknowledgement. The executable exits and
    // closes stdout; the parent must validate the complete frame and clean EOF.
    try checkpoint()
    guard try utcNow() < challenge.expiresAt else { throw ApprovalHostError.challengeExpired }
  }

  private func checkpoint() throws {
    let now = operations.monotonicNow()
    guard now.isFinite, now >= 0, previousMonotonic.map({ now >= $0 }) ?? true else {
      throw ApprovalHostError.transportUnavailable
    }
    if deadline == nil {
      let value = now + 300
      guard value.isFinite, value > now else { throw ApprovalHostError.transportUnavailable }
      deadline = value
    }
    guard let deadline, now < deadline else { throw ApprovalHostError.timedOut }
    previousMonotonic = now
  }

  private func utcNow() throws -> Date {
    let now = operations.utcNow()
    guard now.timeIntervalSince1970.isFinite, previousUTC.map({ now >= $0 }) ?? true else {
      throw ApprovalHostError.transportUnavailable
    }
    previousUTC = now
    return now
  }
}

private final class MetaCampaignPauseHelperResponseSlot: @unchecked Sendable {
  private let lock = NSLock()
  private var response: NativeMetaCampaignPauseApprovalBridgeXPCResponse?
  private var closed = false

  var hasResponse: Bool {
    lock.lock()
    defer { lock.unlock() }
    return response != nil
  }

  func complete(_ value: NativeMetaCampaignPauseApprovalBridgeXPCResponse) {
    lock.lock()
    defer { lock.unlock() }
    guard !closed, response == nil else { return }
    response = value
  }

  func consume() throws -> NativeMetaCampaignPauseApprovalBridgeXPCResponse {
    lock.lock()
    defer { lock.unlock() }
    guard !closed, let value = response else { throw ApprovalHostError.transportUnavailable }
    closed = true
    response = nil
    return value
  }

  func close() {
    lock.lock()
    closed = true
    response = nil
    lock.unlock()
  }
}

private final class SystemNativeMetaCampaignPauseApprovalBridgeHelperClient:
  NativeMetaCampaignPauseApprovalBridgeHelperClient
{
  private let client: AuthenticatedNativeMetaCampaignPauseApprovalBridgeClient

  init(metadata: ApprovalHostBuildMetadataV1) {
    client = AuthenticatedNativeMetaCampaignPauseApprovalBridgeClient(metadata: metadata)
  }

  func request(
    _ request: NativeMetaCampaignPauseApprovalBridgeRequestV2,
    completion: @escaping (NativeMetaCampaignPauseApprovalBridgeXPCResponse) -> Void
  ) {
    client.request(request, completion: completion)
  }

  func invalidate() { client.invalidate() }
}

private final class SystemNativeMetaCampaignPauseApprovalBridgeHelperOperations:
  NativeMetaCampaignPauseApprovalBridgeHelperOperations
{
  private let pipes = SystemNativeApprovalBridgeHelperPipeOperations()

  func prepareStandardPipes() throws { try pipes.prepareStandardPipes() }

  func validateCurrentClientIdentity(metadata: ApprovalHostBuildMetadataV1) throws {
    try pipes.validateCurrentClientIdentity(metadata: metadata)
  }

  func monotonicNow() -> Double { pipes.monotonicNow() }

  func utcNow() -> Date { pipes.utcNow() }

  func readRequestChunk(timeoutMilliseconds: Int32) throws -> Data? {
    try pipes.readRequestChunk(timeoutMilliseconds: timeoutMilliseconds)
  }

  func writeCandidateChunk(_ data: Data, timeoutMilliseconds: Int32) throws -> Int? {
    try pipes.writeCandidateChunk(data, timeoutMilliseconds: timeoutMilliseconds)
  }

  func outputPeerPresent() throws -> Bool { try pipes.outputPeerPresent() }

  func waitForResponseTick() { pipes.waitForResponseTick() }

  func makeClient(
    metadata: ApprovalHostBuildMetadataV1
  ) -> any NativeMetaCampaignPauseApprovalBridgeHelperClient {
    SystemNativeMetaCampaignPauseApprovalBridgeHelperClient(metadata: metadata)
  }
}
