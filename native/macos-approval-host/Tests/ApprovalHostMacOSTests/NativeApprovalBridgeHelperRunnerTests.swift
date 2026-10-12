import Foundation
import XCTest

@testable import ApprovalHostCore
@testable import ApprovalHostMacOS

final class NativeApprovalBridgeHelperRunnerTests: XCTestCase {
  private let environment = ["LANG": "C", "LC_ALL": "C", "TZ": "UTC"]

  func testOnePrivateExchangeWritesOnlyTheExactConsumedCandidate() throws {
    let setup = try HelperTestSetup()
    setup.operations.maximumWrite = 17
    try setup.runner.run(argumentCount: 1, environment: environment)
    XCTAssertEqual(setup.operations.events.prefix(2), ["pipes", "identity"])
    XCTAssertEqual(setup.operations.client.requests, 1)
    XCTAssertEqual(setup.operations.client.invalidations, 1)
    let decoder = NativeApprovalBridgeFrameDecoder(role: .candidate)
    try decoder.append(setup.operations.output)
    try decoder.finishAtEOF()
    XCTAssertEqual(try decoder.consume(), setup.candidate.canonicalBytes)
    XCTAssertThrowsError(try setup.runner.run(argumentCount: 1, environment: environment))
    XCTAssertEqual(setup.operations.client.requests, 1)
  }

  func testArgumentsEnvironmentAndIdentityRejectBeforeClientCreation() throws {
    for (count, values) in [
      (2, environment), (1, environment.merging(["DYLD_INSERT_LIBRARIES": "x"]) { $1 }),
      (1, ["LANG": "C", "LC_ALL": "C", "TZ": "Europe/London"]),
    ] {
      let setup = try HelperTestSetup()
      XCTAssertThrowsError(try setup.runner.run(argumentCount: count, environment: values))
      XCTAssertEqual(setup.operations.events, [])
      XCTAssertEqual(setup.operations.client.requests, 0)
    }
    let setup = try HelperTestSetup()
    setup.operations.identityAllowed = false
    XCTAssertThrowsError(try setup.runner.run(argumentCount: 1, environment: environment))
    XCTAssertEqual(setup.operations.events, ["pipes", "identity"])
    XCTAssertEqual(setup.operations.client.requests, 0)
    XCTAssertTrue(setup.operations.output.isEmpty)
  }

  func testNonPipeOrTrailingInputCannotContactNativeClient() throws {
    let notPipe = try HelperTestSetup()
    notPipe.operations.pipesAllowed = false
    XCTAssertThrowsError(try notPipe.runner.run(argumentCount: 1, environment: environment))
    XCTAssertEqual(notPipe.operations.client.requests, 0)
    let trailing = try HelperTestSetup()
    trailing.operations.input.insert(Data([0]), at: trailing.operations.input.count - 1)
    XCTAssertThrowsError(try trailing.runner.run(argumentCount: 1, environment: environment))
    XCTAssertEqual(trailing.operations.client.requests, 0)
    XCTAssertTrue(trailing.operations.output.isEmpty)
  }

  func testDeadlineAndLostParentInvalidateClientWithoutWriting() throws {
    let deadline = try HelperTestSetup()
    deadline.operations.client.response = nil
    deadline.operations.waitAction = { deadline.operations.monotonic = 301 }
    XCTAssertThrowsError(try deadline.runner.run(argumentCount: 1, environment: environment))
    XCTAssertEqual(deadline.operations.client.invalidations, 1)
    XCTAssertTrue(deadline.operations.output.isEmpty)
    let parent = try HelperTestSetup()
    parent.operations.client.response = nil
    parent.operations.waitAction = { parent.operations.peerPresent = false }
    XCTAssertThrowsError(try parent.runner.run(argumentCount: 1, environment: environment))
    XCTAssertEqual(parent.operations.client.invalidations, 1)
    XCTAssertTrue(parent.operations.output.isEmpty)
  }

  func testClockRollbackAndExpiredResponseCannotBeWritten() throws {
    for rollback in [false, true] {
      let setup = try HelperTestSetup()
      setup.operations.client.beforeReply = {
        setup.operations.utc = setup.operations.utc.addingTimeInterval(rollback ? -1 : 300)
      }
      XCTAssertThrowsError(try setup.runner.run(argumentCount: 1, environment: environment))
      XCTAssertEqual(setup.operations.client.invalidations, 1)
      XCTAssertTrue(setup.operations.output.isEmpty)
    }
  }

  func testOverreportedWriteAndExpiryAfterFinalWriteRemainFailure() throws {
    let wrongWrite = try HelperTestSetup()
    wrongWrite.operations.overreportWrite = true
    XCTAssertThrowsError(try wrongWrite.runner.run(argumentCount: 1, environment: environment))
    XCTAssertEqual(wrongWrite.operations.client.invalidations, 1)
    let expired = try HelperTestSetup()
    expired.operations.afterWrite = {
      expired.operations.utc = expired.operations.utc.addingTimeInterval(300)
    }
    XCTAssertThrowsError(try expired.runner.run(argumentCount: 1, environment: environment))
    XCTAssertEqual(expired.operations.client.invalidations, 1)
    // Even if a whole frame reached stdout, the executable's failed exit is
    // mandatory rejection at the parent transport boundary.
    XCTAssertFalse(expired.operations.output.isEmpty)
  }
}

private struct HelperTestSetup {
  let candidate: NativeApprovalBridgeCandidateV1
  let operations: HelperFakeOperations
  let runner: NativeApprovalBridgeHelperRunner

  init() throws {
    let fixture = try BridgeProducerTestFixture()
    candidate = try fixture.produce(session: BridgeProducerTestSession())
    operations = HelperFakeOperations(
      request: fixture.request, candidate: candidate, now: fixture.now)
    runner = NativeApprovalBridgeHelperRunner(metadata: fixture.metadata, operations: operations)
  }
}

private final class HelperFakeClient: NativeApprovalBridgeHelperClient {
  let expected: NativeApprovalBridgeRequestV1
  var response: NativeApprovalBridgeXPCResponse?
  var beforeReply: (() -> Void)?
  var requests = 0
  var invalidations = 0
  init(request: NativeApprovalBridgeRequestV1, candidate: NativeApprovalBridgeCandidateV1) {
    expected = request
    response = .candidate(candidate)
  }
  func request(
    _ request: NativeApprovalBridgeRequestV1,
    completion: @escaping (NativeApprovalBridgeXPCResponse) -> Void
  ) {
    requests += 1
    XCTAssertEqual(request, expected)
    beforeReply?()
    if let response { completion(response) }
  }
  func invalidate() { invalidations += 1 }
}

private final class HelperFakeOperations: NativeApprovalBridgeHelperOperations {
  let client: HelperFakeClient
  var input: [Data]
  var output = Data()
  var events: [String] = []
  var monotonic = 0.0
  var utc: Date
  var pipesAllowed = true
  var identityAllowed = true
  var peerPresent = true
  var maximumWrite = 16_384
  var overreportWrite = false
  var waitAction: (() -> Void)?
  var afterWrite: (() -> Void)?

  init(
    request: NativeApprovalBridgeRequestV1, candidate: NativeApprovalBridgeCandidateV1, now: Date
  ) {
    client = HelperFakeClient(request: request, candidate: candidate)
    let frame = try! NativeApprovalBridgeFrameEncoder.request(request)
    input =
      stride(from: 0, to: frame.count, by: 4096).map {
        Data(frame[$0..<min($0 + 4096, frame.count)])
      } + [Data()]
    utc = now
  }
  func prepareStandardPipes() throws {
    events.append("pipes")
    guard pipesAllowed else { throw ApprovalHostError.transportUnavailable }
  }
  func validateCurrentClientIdentity(metadata: ApprovalHostBuildMetadataV1) throws {
    events.append("identity")
    guard identityAllowed else { throw ApprovalHostError.hostNotInstalled }
  }
  func monotonicNow() -> Double { monotonic }
  func utcNow() -> Date { utc }
  func readRequestChunk(timeoutMilliseconds: Int32) throws -> Data? {
    XCTAssertEqual(timeoutMilliseconds, 50)
    guard !input.isEmpty else { throw ApprovalHostError.transportUnavailable }
    return input.removeFirst()
  }
  func writeCandidateChunk(_ data: Data, timeoutMilliseconds: Int32) throws -> Int? {
    XCTAssertEqual(timeoutMilliseconds, 50)
    let count = min(maximumWrite, data.count)
    output.append(data.prefix(count))
    afterWrite?()
    return overreportWrite ? data.count + 1 : count
  }
  func outputPeerPresent() throws -> Bool { peerPresent }
  func waitForResponseTick() { waitAction?() }
  func makeClient(metadata: ApprovalHostBuildMetadataV1) -> any NativeApprovalBridgeHelperClient {
    client
  }
}
