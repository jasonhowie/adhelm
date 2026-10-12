import Foundation
import XCTest

@testable import ApprovalHostMacOS

/// Registry-only tests deliberately avoid an NSXPC listener, connection,
/// Security, Keychain, AppKit, LocalAuthentication, or provider operation.
final class AuthenticatedXPCListenerShutdownTests: XCTestCase {
  func testShutdownClosesAdmissionCancelsServiceAndInvalidatesConnectionOnce() {
    let registry = AuthenticatedApprovalXPCConnectionRegistry()
    let service = NSObject()
    let connection = NSObject()
    var calls: [String] = []
    let identifier = registry.admit(
      service: service,
      connection: connection,
      cancelService: { calls.append("cancel") },
      invalidateConnection: { calls.append("invalidate") }
    )

    XCTAssertNotNil(identifier)
    XCTAssertTrue(registry.canAdmit)
    XCTAssertEqual(registry.acceptedConnectionCount, 1)
    XCTAssertTrue(registry.invalidate())
    XCTAssertEqual(calls, ["cancel", "invalidate"])
    XCTAssertFalse(registry.canAdmit)
    XCTAssertEqual(registry.acceptedConnectionCount, 0)
    XCTAssertFalse(registry.invalidate())
    XCTAssertEqual(calls, ["cancel", "invalidate"])
    XCTAssertNil(
      registry.admit(
        service: NSObject(),
        connection: NSObject(),
        cancelService: {},
        invalidateConnection: {}
      ))
    if let identifier {
      XCTAssertFalse(registry.canActivate(identifier))
    }
  }

  func testSelfInvalidatedConnectionIsReleasedAndNotCancelledAgainAtShutdown() {
    let registry = AuthenticatedApprovalXPCConnectionRegistry()
    var service: NSObject? = NSObject()
    var connection: NSObject? = NSObject()
    let weakService = WeakReference(service)
    let weakConnection = WeakReference(connection)
    var cancellationCount = 0
    var invalidationCount = 0
    let identifier = registry.admit(
      service: service!,
      connection: connection!,
      cancelService: { cancellationCount += 1 },
      invalidateConnection: { invalidationCount += 1 }
    )!

    service = nil
    connection = nil
    XCTAssertNotNil(weakService.value)
    XCTAssertNotNil(weakConnection.value)
    registry.remove(identifier)
    XCTAssertNil(weakService.value)
    XCTAssertNil(weakConnection.value)
    XCTAssertTrue(registry.invalidate())
    XCTAssertEqual(cancellationCount, 0)
    XCTAssertEqual(invalidationCount, 0)
  }

  func testActivationRequiresTheSameOpenAdmission() {
    let registry = AuthenticatedApprovalXPCConnectionRegistry()
    let identifier = registry.admit(
      service: NSObject(),
      connection: NSObject(),
      cancelService: {},
      invalidateConnection: {}
    )!

    XCTAssertTrue(registry.canActivateListener)
    XCTAssertTrue(registry.canActivate(identifier))
    registry.remove(identifier)
    XCTAssertFalse(registry.canActivate(identifier))
    XCTAssertTrue(registry.canActivateListener)
    XCTAssertTrue(registry.invalidate())
    XCTAssertFalse(registry.canActivateListener)
  }

  func testShutdownClosesAdmissionBeforePendingCancellationCompletes() {
    let registry = AuthenticatedApprovalXPCConnectionRegistry()
    let cancellationStarted = expectation(description: "cancellation started")
    let shutdownFinished = expectation(description: "shutdown finished")
    let allowCancellation = DispatchSemaphore(value: 0)
    _ = registry.admit(
      service: NSObject(),
      connection: NSObject(),
      cancelService: {
        cancellationStarted.fulfill()
        _ = allowCancellation.wait(timeout: .now() + 1)
      },
      invalidateConnection: {}
    )

    DispatchQueue.global().async {
      _ = registry.invalidate()
      shutdownFinished.fulfill()
    }
    wait(for: [cancellationStarted], timeout: 1)
    XCTAssertFalse(registry.canAdmit)
    XCTAssertNil(
      registry.admit(
        service: NSObject(),
        connection: NSObject(),
        cancelService: {},
        invalidateConnection: {}
      ))
    allowCancellation.signal()
    wait(for: [shutdownFinished], timeout: 1)
  }
}

private final class WeakReference {
  weak var value: AnyObject?

  init(_ value: AnyObject?) {
    self.value = value
  }
}
