import Foundation
import XCTest

@testable import ApprovalHostCore
@testable import ApprovalHostMacOS

/// Fake-only coverage for the distinct Meta campaign PAUSE V2 helper. No test
/// creates a process, XPC connection, protected store, LocalAuthentication
/// context, signer, or provider request.
final class NativeMetaCampaignPauseApprovalBridgeHelperRunnerTests: XCTestCase {
  private let environment = ["LANG": "C", "LC_ALL": "C", "TZ": "UTC"]

  func testOneV2ExchangeWritesOnlyExactConsumedCandidateAfterCleanEOF() throws {
    let setup = try MetaV2HelperTestSetup()
    setup.operations.maximumWrite = 17

    try setup.runner.run(argumentCount: 1, environment: environment)

    XCTAssertEqual(setup.operations.events.prefix(2), ["pipes", "identity"])
    XCTAssertEqual(setup.operations.client.requests, 1)
    XCTAssertEqual(setup.operations.client.invalidations, 1)
    let decoder = NativeApprovalBridgeFrameDecoder(role: .metaCampaignPauseV2Candidate)
    try decoder.append(setup.operations.output)
    try decoder.finishAtEOF()
    XCTAssertEqual(try decoder.consume(), setup.candidate.canonicalBytes)
    XCTAssertThrowsError(try setup.runner.run(argumentCount: 1, environment: environment))
    XCTAssertEqual(setup.operations.client.requests, 1)
  }

  func testV2FrameRolesRequireCleanEOFAndKeepTheirOwnClosedCaps() throws {
    let request = NativeApprovalBridgeFrameDecoder(role: .metaCampaignPauseV2Request)
    XCTAssertThrowsError(try request.append(Data([0, 1, 128, 1])))

    let candidate = NativeApprovalBridgeFrameDecoder(role: .metaCampaignPauseV2Candidate)
    XCTAssertThrowsError(try candidate.append(Data([0, 2, 0, 1])))

    let beforeEOF = NativeApprovalBridgeFrameDecoder(role: .metaCampaignPauseV2Request)
    try beforeEOF.append(Data([0, 0, 0, 1, 0x61]))
    XCTAssertThrowsError(try beforeEOF.consume())

    let eof = NativeApprovalBridgeFrameDecoder(role: .metaCampaignPauseV2Request)
    try eof.append(Data([0, 0, 0, 1, 0x61]))
    try eof.finishAtEOF()
    XCTAssertEqual(try eof.consume(), Data([0x61]))
  }

  func testArgumentsEnvironmentAndIdentityRejectBeforeV2ClientCreation() throws {
    for (count, values) in [
      (2, environment),
      (1, environment.merging(["DYLD_INSERT_LIBRARIES": "x"]) { $1 }),
      (1, ["LANG": "C", "LC_ALL": "C", "TZ": "Europe/London"]),
    ] {
      let setup = try MetaV2HelperTestSetup()
      XCTAssertThrowsError(try setup.runner.run(argumentCount: count, environment: values))
      XCTAssertEqual(setup.operations.events, [])
      XCTAssertEqual(setup.operations.client.requests, 0)
    }
    let setup = try MetaV2HelperTestSetup()
    setup.operations.identityAllowed = false

    XCTAssertThrowsError(try setup.runner.run(argumentCount: 1, environment: environment))

    XCTAssertEqual(setup.operations.events, ["pipes", "identity"])
    XCTAssertEqual(setup.operations.client.requests, 0)
    XCTAssertTrue(setup.operations.output.isEmpty)
  }

  func testNonPipeTrailingMalformedAndV1InputCannotContactV2Client() throws {
    let notPipe = try MetaV2HelperTestSetup()
    notPipe.operations.pipesAllowed = false
    XCTAssertThrowsError(try notPipe.runner.run(argumentCount: 1, environment: environment))
    XCTAssertEqual(notPipe.operations.client.requests, 0)

    let trailing = try MetaV2HelperTestSetup()
    trailing.operations.input.insert(Data([0]), at: trailing.operations.input.count - 1)
    XCTAssertThrowsError(try trailing.runner.run(argumentCount: 1, environment: environment))
    XCTAssertEqual(trailing.operations.client.requests, 0)
    XCTAssertTrue(trailing.operations.output.isEmpty)

    let v1Input = try MetaV2HelperTestSetup()
    let v1Fixture = try BridgeProducerTestFixture()
    v1Input.operations.input = [
      try NativeApprovalBridgeFrameEncoder.request(v1Fixture.request),
      Data(),
    ]
    XCTAssertThrowsError(try v1Input.runner.run(argumentCount: 1, environment: environment))
    XCTAssertEqual(v1Input.operations.client.requests, 0)
    XCTAssertTrue(v1Input.operations.output.isEmpty)
  }

  func testDeadlineAndLostParentInvalidateV2ClientWithoutWriting() throws {
    let deadline = try MetaV2HelperTestSetup()
    deadline.operations.client.response = nil
    deadline.operations.waitAction = { deadline.operations.monotonic = 301 }
    XCTAssertThrowsError(try deadline.runner.run(argumentCount: 1, environment: environment))
    XCTAssertEqual(deadline.operations.client.invalidations, 1)
    XCTAssertTrue(deadline.operations.output.isEmpty)

    let parent = try MetaV2HelperTestSetup()
    parent.operations.client.response = nil
    parent.operations.waitAction = { parent.operations.peerPresent = false }
    XCTAssertThrowsError(try parent.runner.run(argumentCount: 1, environment: environment))
    XCTAssertEqual(parent.operations.client.invalidations, 1)
    XCTAssertTrue(parent.operations.output.isEmpty)
  }

  func testClockRollbackAndExpiredV2ResponseCannotBeWritten() throws {
    for rollback in [false, true] {
      let setup = try MetaV2HelperTestSetup()
      setup.operations.client.beforeReply = {
        setup.operations.utc = setup.operations.utc.addingTimeInterval(rollback ? -1 : 300)
      }

      XCTAssertThrowsError(try setup.runner.run(argumentCount: 1, environment: environment))
      XCTAssertEqual(setup.operations.client.invalidations, 1)
      XCTAssertTrue(setup.operations.output.isEmpty)
    }
  }

  func testOverreportedWriteAndExpiryAfterFinalWriteRemainFailure() throws {
    let wrongWrite = try MetaV2HelperTestSetup()
    wrongWrite.operations.overreportWrite = true
    XCTAssertThrowsError(try wrongWrite.runner.run(argumentCount: 1, environment: environment))
    XCTAssertEqual(wrongWrite.operations.client.invalidations, 1)

    let expired = try MetaV2HelperTestSetup()
    expired.operations.afterWrite = {
      expired.operations.utc = expired.operations.utc.addingTimeInterval(300)
    }
    XCTAssertThrowsError(try expired.runner.run(argumentCount: 1, environment: environment))
    XCTAssertEqual(expired.operations.client.invalidations, 1)
    // A failed exit means the parent transport rejects even a full frame that
    // reached stdout before the final expiry check.
    XCTAssertFalse(expired.operations.output.isEmpty)
  }
}

private struct MetaV2HelperTestSetup {
  let candidate: NativeMetaCampaignPauseApprovalBridgeCandidateV2
  let operations: MetaV2HelperFakeOperations
  let runner: NativeMetaCampaignPauseApprovalBridgeHelperRunner

  init() throws {
    let fixture = try MetaV2ProducerFixture()
    candidate = try fixture.produce(session: BridgeProducerTestSession())
    operations = MetaV2HelperFakeOperations(
      request: fixture.request,
      candidate: candidate,
      now: fixture.now
    )
    runner = NativeMetaCampaignPauseApprovalBridgeHelperRunner(
      metadata: fixture.metadata,
      operations: operations
    )
  }
}

private final class MetaV2HelperFakeClient: NativeMetaCampaignPauseApprovalBridgeHelperClient {
  let expected: NativeMetaCampaignPauseApprovalBridgeRequestV2
  var response: NativeMetaCampaignPauseApprovalBridgeXPCResponse?
  var beforeReply: (() -> Void)?
  var requests = 0
  var invalidations = 0

  init(
    request: NativeMetaCampaignPauseApprovalBridgeRequestV2,
    candidate: NativeMetaCampaignPauseApprovalBridgeCandidateV2
  ) {
    expected = request
    response = .candidate(candidate)
  }

  func request(
    _ request: NativeMetaCampaignPauseApprovalBridgeRequestV2,
    completion: @escaping (NativeMetaCampaignPauseApprovalBridgeXPCResponse) -> Void
  ) {
    requests += 1
    XCTAssertEqual(request, expected)
    beforeReply?()
    if let response { completion(response) }
  }

  func invalidate() { invalidations += 1 }
}

private final class MetaV2HelperFakeOperations:
  NativeMetaCampaignPauseApprovalBridgeHelperOperations
{
  let client: MetaV2HelperFakeClient
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
    request: NativeMetaCampaignPauseApprovalBridgeRequestV2,
    candidate: NativeMetaCampaignPauseApprovalBridgeCandidateV2,
    now: Date
  ) {
    client = MetaV2HelperFakeClient(request: request, candidate: candidate)
    let frame = try! NativeApprovalBridgeFrameEncoder.metaCampaignPauseV2Request(request)
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

  func validateCurrentClientIdentity(metadata _: ApprovalHostBuildMetadataV1) throws {
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

  func makeClient(
    metadata _: ApprovalHostBuildMetadataV1
  ) -> any NativeMetaCampaignPauseApprovalBridgeHelperClient {
    client
  }
}
