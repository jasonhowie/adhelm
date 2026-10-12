import ApprovalHostCore
import XCTest

@testable import ApprovalHostMacOS

final class EnrollmentCancellationPhaseTests: XCTestCase {
  func testCancellationBeforeProtectedWorkPreventsItsHandoff() {
    let state = EnrollmentOperationState()
    XCTAssertEqual(state.cancel(), .reviewCancelled)
    XCTAssertFalse(state.beginProtectedWork())
    XCTAssertFalse(state.mayContinueProtectedWork)
  }

  func testCancellationAfterHandoffReportsUnknownProtectedState() {
    let state = EnrollmentOperationState()
    XCTAssertTrue(state.beginProtectedWork())
    XCTAssertTrue(state.mayContinueProtectedWork)
    XCTAssertEqual(state.cancel(), .enrollmentStateUnverified)
    XCTAssertFalse(state.mayContinueProtectedWork)
    XCTAssertFalse(state.beginProtectedWork())
    XCTAssertEqual(state.cancel(), .enrollmentStateUnverified)
  }

  func testProtectedWorkCannotStartTwiceAndUncertaintyHasClosedDiagnostic() {
    let state = EnrollmentOperationState()
    XCTAssertTrue(state.beginProtectedWork())
    XCTAssertFalse(state.beginProtectedWork())
    let error = ApprovalHostError.enrollmentStateUnverified
    XCTAssertEqual(
      ApprovalHostError.exactPublicFailure(code: error.code, message: error.publicMessage), error
    )
    XCTAssertNil(ApprovalHostError.exactPublicFailure(code: error.code, message: "untrusted"))
  }
}
