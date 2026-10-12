import ApprovalHostCore
import CoreFoundation
import Darwin
import Foundation
@preconcurrency import Security

/// Module-local fixed pipe mechanics shared by the V1 and Meta V2 helper
/// runners. This has no request, candidate, selector, role, or client factory
/// operation, so neither runner can route a request through the other.
protocol NativeApprovalBridgeHelperPipeOperations: AnyObject {
  func prepareStandardPipes() throws
  func validateCurrentClientIdentity(metadata: ApprovalHostBuildMetadataV1) throws
  func monotonicNow() -> Double
  func utcNow() -> Date
  func readRequestChunk(timeoutMilliseconds: Int32) throws -> Data?
  func writeCandidateChunk(_ data: Data, timeoutMilliseconds: Int32) throws -> Int?
  func outputPeerPresent() throws -> Bool
  func waitForResponseTick()
}

/// All collaborators are private to the fixed V1 native helper. None accepts
/// a path, descriptor number, command, environment override, receipt, or pin.
protocol NativeApprovalBridgeHelperClient: AnyObject {
  func request(
    _ request: NativeApprovalBridgeRequestV1,
    completion: @escaping (NativeApprovalBridgeXPCResponse) -> Void)
  func invalidate()
}

protocol NativeApprovalBridgeHelperOperations: NativeApprovalBridgeHelperPipeOperations {
  func makeClient(metadata: ApprovalHostBuildMetadataV1) -> any NativeApprovalBridgeHelperClient
}

/// The configured executable calls only the static entrypoint. Tests inject
/// bounded in-memory operations; they never create a process, XPC peer or key.
package final class NativeApprovalBridgeHelperRunner {
  private let metadata: ApprovalHostBuildMetadataV1
  private let operations: any NativeApprovalBridgeHelperOperations
  private var previousMonotonic: Double?
  private var deadline: Double?
  private var previousUTC: Date?
  private var started = false

  init(metadata: ApprovalHostBuildMetadataV1, operations: any NativeApprovalBridgeHelperOperations)
  {
    self.metadata = metadata
    self.operations = operations
  }

  package static func run(metadata: ApprovalHostBuildMetadataV1) throws {
    let runner = NativeApprovalBridgeHelperRunner(
      metadata: metadata, operations: SystemNativeApprovalBridgeHelperOperations())
    try runner.run(
      argumentCount: CommandLine.arguments.count,
      environment: ProcessInfo.processInfo.environment)
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
    let decoder = NativeApprovalBridgeFrameDecoder(role: .request)
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
    let request = try NativeApprovalBridgeRequestV1.decode(decoder.consume())
    let exchangeStartedAt = try utcNow()
    let challenge = try request.boundChallenge(
      metadata: metadata, exchangeStartedAt: exchangeStartedAt)
    try checkpoint()
    guard try operations.outputPeerPresent() else { throw ApprovalHostError.transportUnavailable }
    let client = operations.makeClient(metadata: metadata)
    defer { client.invalidate() }
    let response = HelperResponseSlot()
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
    let candidate = try NativeApprovalBridgeCandidateV1.decode(
      supplied.canonicalBytes, expectedRequest: request, metadata: metadata,
      exchangeStartedAt: exchangeStartedAt, currentNow: now)
    let frame = try NativeApprovalBridgeFrameEncoder.candidate(candidate)
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

private final class HelperResponseSlot: @unchecked Sendable {
  private let lock = NSLock()
  private var response: NativeApprovalBridgeXPCResponse?
  private var closed = false

  var hasResponse: Bool {
    lock.lock()
    defer { lock.unlock() }
    return response != nil
  }
  func complete(_ value: NativeApprovalBridgeXPCResponse) {
    lock.lock()
    defer { lock.unlock() }
    guard !closed, response == nil else { return }
    response = value
  }
  func consume() throws -> NativeApprovalBridgeXPCResponse {
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

private final class SystemNativeApprovalBridgeHelperClient: NativeApprovalBridgeHelperClient {
  private let client: AuthenticatedNativeApprovalBridgeClient
  init(metadata: ApprovalHostBuildMetadataV1) {
    client = AuthenticatedNativeApprovalBridgeClient(metadata: metadata)
  }
  func request(
    _ request: NativeApprovalBridgeRequestV1,
    completion: @escaping (NativeApprovalBridgeXPCResponse) -> Void
  ) {
    client.request(request, completion: completion)
  }
  func invalidate() { client.invalidate() }
}

/// Fixed system implementation of the narrow common helper-pipe surface. It
/// is module-internal only so the two closed typed runners can share the same
/// descriptor, identity, clock, and liveness mechanics.
final class SystemNativeApprovalBridgeHelperPipeOperations: NativeApprovalBridgeHelperPipeOperations
{
  func prepareStandardPipes() throws {
    for descriptor in [STDIN_FILENO, STDOUT_FILENO, STDERR_FILENO] {
      var info = stat()
      guard fstat(descriptor, &info) == 0, info.st_mode & mode_t(S_IFMT) == mode_t(S_IFIFO) else {
        throw ApprovalHostError.transportUnavailable
      }
      let flags = fcntl(descriptor, F_GETFL)
      guard flags >= 0, fcntl(descriptor, F_SETFL, flags | O_NONBLOCK) == 0 else {
        throw ApprovalHostError.transportUnavailable
      }
    }
    guard fcntl(STDOUT_FILENO, F_SETNOSIGPIPE, 1) == 0 else {
      throw ApprovalHostError.transportUnavailable
    }
  }

  func validateCurrentClientIdentity(metadata: ApprovalHostBuildMetadataV1) throws {
    var requirement: SecRequirement?
    var code: SecCode?
    guard
      SecRequirementCreateWithString(
        metadata.expectedClientCodeSigningRequirement as CFString, SecCSFlags(), &requirement)
        == errSecSuccess,
      let requirement,
      SecCodeCopySelf(SecCSFlags(), &code) == errSecSuccess, let code,
      SecCodeCheckValidity(code, SecCSFlags(), requirement) == errSecSuccess
    else { throw ApprovalHostError.hostNotInstalled }
  }

  func monotonicNow() -> Double { ProcessInfo.processInfo.systemUptime }
  func utcNow() -> Date { Date() }

  func readRequestChunk(timeoutMilliseconds: Int32) throws -> Data? {
    var event = pollfd(fd: STDIN_FILENO, events: Int16(POLLIN), revents: 0)
    let count = poll(&event, 1, timeoutMilliseconds)
    if count < 0, errno == EINTR { return nil }
    guard count >= 0, event.revents & Int16(POLLERR | POLLNVAL) == 0 else {
      throw ApprovalHostError.transportUnavailable
    }
    if count == 0 { return nil }
    var buffer = [UInt8](repeating: 0, count: 16_384)
    let readCount = buffer.withUnsafeMutableBytes {
      Darwin.read(STDIN_FILENO, $0.baseAddress, $0.count)
    }
    if readCount < 0, errno == EAGAIN || errno == EINTR { return nil }
    guard readCount >= 0 else { throw ApprovalHostError.transportUnavailable }
    return Data(buffer.prefix(readCount))
  }

  func writeCandidateChunk(_ data: Data, timeoutMilliseconds: Int32) throws -> Int? {
    guard !data.isEmpty, data.count <= 16_384 else { throw ApprovalHostError.transportUnavailable }
    var event = pollfd(fd: STDOUT_FILENO, events: Int16(POLLOUT), revents: 0)
    let count = poll(&event, 1, timeoutMilliseconds)
    if count < 0, errno == EINTR { return nil }
    guard count >= 0, event.revents & Int16(POLLERR | POLLHUP | POLLNVAL) == 0 else {
      throw ApprovalHostError.transportUnavailable
    }
    if count == 0 { return nil }
    let written = data.withUnsafeBytes { Darwin.write(STDOUT_FILENO, $0.baseAddress, $0.count) }
    if written < 0, errno == EAGAIN || errno == EINTR { return nil }
    guard written > 0 else { throw ApprovalHostError.transportUnavailable }
    return written
  }

  func outputPeerPresent() throws -> Bool {
    var event = pollfd(fd: STDOUT_FILENO, events: 0, revents: 0)
    let count = poll(&event, 1, 0)
    if count < 0, errno == EINTR { return true }
    guard count >= 0 else { throw ApprovalHostError.transportUnavailable }
    return event.revents & Int16(POLLERR | POLLHUP | POLLNVAL) == 0
  }

  func waitForResponseTick() {
    CFRunLoopRunInMode(CFRunLoopMode.defaultMode, 0.05, true)
  }

}

private final class SystemNativeApprovalBridgeHelperOperations:
  NativeApprovalBridgeHelperOperations
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

  func makeClient(metadata: ApprovalHostBuildMetadataV1) -> any NativeApprovalBridgeHelperClient {
    SystemNativeApprovalBridgeHelperClient(metadata: metadata)
  }
}
