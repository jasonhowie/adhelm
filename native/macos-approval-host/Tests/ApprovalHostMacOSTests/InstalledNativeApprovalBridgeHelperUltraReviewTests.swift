import Foundation
import XCTest

@testable import ApprovalHostCore
@testable import ApprovalHostMacOS

/// Independent fake-only review coverage for the helper's parent-liveness
/// boundary. No test creates a process, XPC connection, protected store, or
/// LocalAuthentication context.
final class InstalledNativeApprovalBridgeHelperUltraReviewTests: XCTestCase {
  func testOutputPeerClosingImmediatelyAfterEOFPreventsClientRequest() throws {
    let fixture = try BridgeProducerTestFixture()
    let candidate = try fixture.produce(session: BridgeProducerTestSession())
    let operations = EOFClosingHelperOperations(
      request: fixture.request, candidate: candidate, now: fixture.now)
    let runner = NativeApprovalBridgeHelperRunner(
      metadata: fixture.metadata, operations: operations)

    XCTAssertThrowsError(
      try runner.run(argumentCount: 1, environment: ["LANG": "C", "LC_ALL": "C", "TZ": "UTC"]))
    XCTAssertEqual(operations.client.requests, 0)
    XCTAssertTrue(operations.output.isEmpty)
  }
}

private final class EOFClosingHelperClient: NativeApprovalBridgeHelperClient {
  private let candidate: NativeApprovalBridgeCandidateV1
  private(set) var requests = 0

  init(candidate: NativeApprovalBridgeCandidateV1) { self.candidate = candidate }

  func request(
    _ request: NativeApprovalBridgeRequestV1,
    completion: @escaping (NativeApprovalBridgeXPCResponse) -> Void
  ) {
    requests += 1
    completion(.candidate(candidate))
  }

  func invalidate() {}
}

private final class EOFClosingHelperOperations: NativeApprovalBridgeHelperOperations {
  let client: EOFClosingHelperClient
  private var input: [Data]
  private var peerPresent = true
  private let now: Date
  private(set) var output = Data()

  init(request: NativeApprovalBridgeRequestV1, candidate: NativeApprovalBridgeCandidateV1, now: Date) {
    client = EOFClosingHelperClient(candidate: candidate)
    let frame = try! NativeApprovalBridgeFrameEncoder.request(request)
    input = stride(from: 0, to: frame.count, by: 4096).map {
      Data(frame[$0..<min($0 + 4096, frame.count)])
    } + [Data()]
    self.now = now
  }

  func prepareStandardPipes() throws {}
  func validateCurrentClientIdentity(metadata _: ApprovalHostBuildMetadataV1) throws {}
  func monotonicNow() -> Double { 0 }
  func utcNow() -> Date { now }

  func readRequestChunk(timeoutMilliseconds _: Int32) throws -> Data? {
    guard !input.isEmpty else { return nil }
    let next = input.removeFirst()
    if next.isEmpty { peerPresent = false }
    return next
  }

  func writeCandidateChunk(_ data: Data, timeoutMilliseconds _: Int32) throws -> Int? {
    output.append(data)
    return data.count
  }

  func outputPeerPresent() throws -> Bool { peerPresent }
  func waitForResponseTick() {}
  func makeClient(metadata _: ApprovalHostBuildMetadataV1) -> any NativeApprovalBridgeHelperClient {
    client
  }
}
