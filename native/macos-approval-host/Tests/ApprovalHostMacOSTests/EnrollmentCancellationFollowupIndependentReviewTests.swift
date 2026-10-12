import ApprovalHostCore
import XCTest

@testable import ApprovalHostMacOS

/// Independent fake-only coverage for the terminal-result race at the
/// enrollment protected-work handoff. No AppKit, LocalAuthentication,
/// Keychain, Security, XPC, or signing adapter is constructed here.
final class EnrollmentCancellationFollowupIndependentReviewTests: XCTestCase {
  func testPostHandoffCancellationDominatesLaterWorkerFailure() {
    let state = EnrollmentOperationState()
    XCTAssertTrue(state.beginProtectedWork())
    XCTAssertEqual(state.protectedWorkFailure(), .signingUnavailable)

    // `cancel()` changes the protected-work state before its caller reaches
    // the one-shot completion callback. A worker that observes cancellation
    // and fails in that interval must retain the recovery diagnostic.
    XCTAssertEqual(state.cancel(), .enrollmentStateUnverified)
    XCTAssertEqual(state.protectedWorkFailure(), .enrollmentStateUnverified)
  }
}
