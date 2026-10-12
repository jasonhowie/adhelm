import Foundation
import XCTest
@testable import ApprovalHostCore
@testable import ApprovalHostMacOS

/// These tests use only an in-memory transport and timer.  They never create
/// an NSXPCConnection, display AppKit UI, evaluate LocalAuthentication, or
/// access a Keychain item.
final class AuthenticatedXPCTests: XCTestCase {
    private let now = Date(timeIntervalSince1970: 1_893_456_060)

    func testMalformedChallengeDoesNotContactTransport() {
        let transport = FakeXPCTransport()
        let scheduler = FakeDeadlineScheduler()
        let client = makeClient(transport: transport, scheduler: scheduler)
        var response: ApprovalHostXPCResponse?

        client.requestApproval(canonicalChallenge: Data("{}".utf8)) { response = $0 }

        XCTAssertEqual(failure(response), .malformedChallenge)
        XCTAssertEqual(transport.sendCount, 0)
        XCTAssertEqual(scheduler.scheduledCount, 0)
    }

    func testOneOutstandingRequestAndDeadlineInvalidateOnlyThatTransport() throws {
        let transport = FakeXPCTransport()
        let scheduler = FakeDeadlineScheduler()
        let client = makeClient(transport: transport, scheduler: scheduler)
        var responses: [ApprovalHostXPCResponse] = []
        let challenge = fixture("openai-pause-challenge-v1")

        client.requestApproval(canonicalChallenge: challenge) { responses.append($0) }
        client.requestApproval(canonicalChallenge: challenge) { responses.append($0) }

        XCTAssertEqual(transport.sendCount, 1)
        XCTAssertEqual(responses.count, 1)
        XCTAssertEqual(failure(responses[0]), .requestAlreadyActive)
        XCTAssertEqual(scheduler.scheduledCount, 1)

        scheduler.fireLatest()
        XCTAssertEqual(responses.count, 2)
        XCTAssertEqual(failure(responses[1]), .timedOut)
        XCTAssertTrue(transport.invalidated)

        transport.reply(.response(receipt: Data("{}".utf8), code: nil, message: nil))
        XCTAssertEqual(responses.count, 2, "late transport replies cannot revive a timed-out request")
    }

    func testLocalInvalidationCancelsRequestAndIgnoresLateReply() {
        let transport = FakeXPCTransport()
        let client = makeClient(transport: transport, scheduler: FakeDeadlineScheduler())
        var responses: [ApprovalHostXPCResponse] = []

        client.requestApproval(canonicalChallenge: fixture("openai-pause-challenge-v1")) {
            responses.append($0)
        }
        client.invalidate()

        XCTAssertEqual(responses.count, 1)
        XCTAssertEqual(failure(responses[0]), .transportUnavailable)
        XCTAssertTrue(transport.invalidated)
        transport.reply(.response(receipt: Data("{}".utf8), code: nil, message: nil))
        XCTAssertEqual(responses.count, 1)
    }

    func testOnlyBoundedCanonicalReceiptCandidatesAndExactPublicFailuresEscape() throws {
        let accepted = try requestResponse(.response(
            receipt: CanonicalJSON.encode(.object(["schema_version": .integer(1)])),
            code: nil,
            message: nil
        ))
        guard case let .receipt(candidate) = accepted else {
            return XCTFail("expected bounded canonical receipt candidate")
        }
        XCTAssertEqual(candidate, Data("{\"schema_version\":1}".utf8))

        let noncanonical = try requestResponse(.response(
            receipt: Data("{\"z\":1,\"a\":2}".utf8), code: nil, message: nil
        ))
        XCTAssertEqual(failure(noncanonical), .transportUnavailable)

        let oversized = try requestResponse(.response(
            receipt: Data(repeating: 0x7B, count: AuthenticatedApprovalXPCClient.maximumReceiptBytes + 1),
            code: nil,
            message: nil
        ))
        XCTAssertEqual(failure(oversized), .transportUnavailable)

        let forgedFailure = try requestResponse(.response(
            receipt: nil,
            code: "REMOTE_STACK_TRACE",
            message: "untrusted remote text must not leave the client"
        ))
        XCTAssertEqual(failure(forgedFailure), .transportUnavailable)
        guard case let .failure(publicFailure) = forgedFailure else {
            return XCTFail("expected local failure")
        }
        XCTAssertEqual(publicFailure.message, ApprovalHostError.transportUnavailable.publicMessage)

        let exactFailure = try requestResponse(.response(
            receipt: nil,
            code: ApprovalHostError.signingUnavailable.code,
            message: ApprovalHostError.signingUnavailable.publicMessage
        ))
        XCTAssertEqual(failure(exactFailure), .signingUnavailable)
    }

    func testCancelledEnrollmentCallbackInvalidatesArrivingSessionWithoutLocalAuthentication() {
        let session = FakeAuthenticatedSession()
        let consumed = EnrollmentAuthenticationGate.invalidatesArrivingSession(
            .authenticated(session),
            whenCancelled: true
        )
        XCTAssertTrue(consumed)
        XCTAssertTrue(session.invalidated)
        XCTAssertFalse(EnrollmentAuthenticationGate.invalidatesArrivingSession(
            .authenticated(session),
            whenCancelled: false
        ))
    }

    private func requestResponse(_ wireResponse: ApprovalXPCWireResponse) throws -> ApprovalHostXPCResponse {
        let transport = FakeXPCTransport()
        let client = makeClient(transport: transport, scheduler: FakeDeadlineScheduler())
        var result: ApprovalHostXPCResponse?
        client.requestApproval(canonicalChallenge: fixture("openai-pause-challenge-v1")) { result = $0 }
        transport.reply(wireResponse)
        return try XCTUnwrap(result)
    }

    private func makeClient(
        transport: FakeXPCTransport,
        scheduler: FakeDeadlineScheduler
    ) -> AuthenticatedApprovalXPCClient {
        AuthenticatedApprovalXPCClient(
            configuration: try! configuration(),
            transportFactory: { transport },
            scheduler: scheduler,
            clock: { self.now }
        )
    }

    private func configuration() throws -> TrustedHostConfiguration {
        try TrustedHostConfiguration(
            hostReference: "host:adhelm-macos-approval-v1",
            signingKeyReference: "approval-key:fixture-v1",
            machServiceName: "invalid.fixture.service",
            expectedClientCodeSigningRequirement: "identifier \"com.aptsonic.adhelm.client\"",
            expectedHostCodeSigningRequirement: "identifier \"com.aptsonic.adhelm.host\"",
            signingKeyApplicationTag: Data("fixture".utf8),
            keyStorage: .keychainP256
        )
    }

    private func fixture(_ name: String) -> Data {
        let url = Bundle.module.url(forResource: name, withExtension: "json", subdirectory: "Fixtures")!
        return try! Data(contentsOf: url)
    }

    private func failure(_ response: ApprovalHostXPCResponse) -> ApprovalHostError? {
        guard case let .failure(publicFailure) = response else { return nil }
        return ApprovalHostError.exactPublicFailure(
            code: publicFailure.code,
            message: publicFailure.message
        )
    }

    private func failure(_ response: ApprovalHostXPCResponse?) -> ApprovalHostError? {
        guard let response else { return nil }
        return failure(response)
    }
}

private final class FakeXPCTransport: ApprovalXPCTransport {
    private(set) var sendCount = 0
    private(set) var invalidated = false
    private var completion: ((ApprovalXPCWireResponse) -> Void)?

    func send(
        canonicalChallenge _: Data,
        completion: @escaping (ApprovalXPCWireResponse) -> Void
    ) {
        sendCount += 1
        self.completion = completion
    }

    func invalidate() {
        invalidated = true
    }

    func reply(_ response: ApprovalXPCWireResponse) {
        completion?(response)
    }
}

private final class FakeDeadlineScheduler: ApprovalDeadlineScheduler {
    private struct ScheduledAction {
        let action: () -> Void
        let cancellation: FakeCancellation
    }

    private var actions: [ScheduledAction] = []

    var scheduledCount: Int { actions.count }

    func schedule(after _: TimeInterval, action: @escaping () -> Void) -> any ApprovalCancellation {
        let cancellation = FakeCancellation()
        actions.append(ScheduledAction(action: action, cancellation: cancellation))
        return cancellation
    }

    func fireLatest() {
        actions.last?.action()
    }
}

private final class FakeCancellation: ApprovalCancellation {
    private(set) var cancelled = false
    func cancel() { cancelled = true }
}

private final class FakeAuthenticatedSession: AuthenticatedApprovalSession {
    private(set) var invalidated = false
    func invalidate() { invalidated = true }
}
