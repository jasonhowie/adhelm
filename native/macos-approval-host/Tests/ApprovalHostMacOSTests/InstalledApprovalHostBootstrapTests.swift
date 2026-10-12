import Foundation
import XCTest

@testable import ApprovalHostCore
@testable import ApprovalHostMacOS

/// All tests in this file use only in-memory factory, code-requirement, and
/// listener fakes. They do not call Security, Keychain, LocalAuthentication,
/// AppKit, NSXPCListener, or an installed service.
final class InstalledApprovalHostBootstrapTests: XCTestCase {
  override func tearDown() {
    ProcessInstalledApprovalHostRuntimeRegistry.shared.resetForTesting()
    super.tearDown()
  }

  func testBootstrapsOnlyAfterBothRequirementsAndCurrentHostValidation() throws {
    let events = BootstrapEvents()
    let preflight = FakePreflight(events: events)
    let factory = FakeFactory(events: events)

    let runtime = try ApprovalHostInstallation.bootstrapInstalledHost(
      metadata: try metadata(),
      preflight: preflight,
      factory: factory
    )
    defer { runtime.shutdown() }

    XCTAssertEqual(
      events.values,
      [
        "compile.host", "compile.client", "validate.host", "store", "engine", "listener",
        "activate",
      ]
    )
    XCTAssertEqual(factory.listener.activateCount, 1)
    XCTAssertEqual(factory.listener.invalidateCount, 0)
  }

  func testHostRequirementFailurePreventsAllConstruction() throws {
    let events = BootstrapEvents()
    let preflight = FakePreflight(events: events)
    preflight.failAt = .hostCompilation
    let factory = FakeFactory(events: events)

    XCTAssertThrowsError(
      try ApprovalHostInstallation.bootstrapInstalledHost(
        metadata: try metadata(),
        preflight: preflight,
        factory: factory
      )
    ) { error in
      XCTAssertEqual(error as? ApprovalHostError, .hostNotInstalled)
    }
    XCTAssertEqual(events.values, ["compile.host"])
  }

  func testClientRequirementFailurePreventsHostValidationAndConstruction() throws {
    let events = BootstrapEvents()
    let preflight = FakePreflight(events: events)
    preflight.failAt = .clientCompilation
    let factory = FakeFactory(events: events)

    XCTAssertThrowsError(
      try ApprovalHostInstallation.bootstrapInstalledHost(
        metadata: try metadata(),
        preflight: preflight,
        factory: factory
      )
    ) { error in
      XCTAssertEqual(error as? ApprovalHostError, .hostNotInstalled)
    }
    XCTAssertEqual(events.values, ["compile.host", "compile.client"])
  }

  func testHostValidationFailureAfterBothCompilationsPreventsConstruction() throws {
    let events = BootstrapEvents()
    let preflight = FakePreflight(events: events)
    preflight.failAt = .hostValidation
    let factory = FakeFactory(events: events)

    XCTAssertThrowsError(
      try ApprovalHostInstallation.bootstrapInstalledHost(
        metadata: try metadata(),
        preflight: preflight,
        factory: factory
      )
    ) { error in
      XCTAssertEqual(error as? ApprovalHostError, .hostNotInstalled)
    }
    XCTAssertEqual(events.values, ["compile.host", "compile.client", "validate.host"])
  }

  func testValidationUsesGeneratedHostRequirementNotClientRequirement() throws {
    let events = BootstrapEvents()
    let preflight = FakePreflight(events: events)
    let factory = FakeFactory(events: events)
    let metadata = try metadata()
    preflight.expectedHostRequirement = metadata.expectedHostCodeSigningRequirement
    preflight.rejectIfHostRequirementEquals = metadata.expectedClientCodeSigningRequirement

    let runtime = try ApprovalHostInstallation.bootstrapInstalledHost(
      metadata: metadata,
      preflight: preflight,
      factory: factory
    )
    defer { runtime.shutdown() }

    XCTAssertEqual(preflight.compiledHostRequirement, metadata.expectedHostCodeSigningRequirement)
    XCTAssertEqual(
      preflight.compiledClientRequirement, metadata.expectedClientCodeSigningRequirement)
    XCTAssertTrue(preflight.validatedCurrentHost)
  }

  func testSecondBootstrapRefusesBeforeSecondFactoryAndShutdownIsTerminal() throws {
    let events = BootstrapEvents()
    let firstFactory = FakeFactory(events: events)
    let first = try ApprovalHostInstallation.bootstrapInstalledHost(
      metadata: try metadata(),
      preflight: FakePreflight(events: events),
      factory: firstFactory
    )

    let secondEvents = BootstrapEvents()
    let secondFactory = FakeFactory(events: secondEvents)
    XCTAssertThrowsError(
      try ApprovalHostInstallation.bootstrapInstalledHost(
        metadata: try metadata(),
        preflight: FakePreflight(events: secondEvents),
        factory: secondFactory
      )
    ) { error in
      XCTAssertEqual(error as? ApprovalHostError, .hostNotInstalled)
    }
    XCTAssertEqual(secondEvents.values, ["compile.host", "compile.client", "validate.host"])

    first.shutdown()
    XCTAssertEqual(firstFactory.listener.invalidateCount, 1)
    first.shutdown()
    XCTAssertEqual(firstFactory.listener.invalidateCount, 1)

    let replacementEvents = BootstrapEvents()
    let replacementFactory = FakeFactory(events: replacementEvents)
    XCTAssertThrowsError(
      try ApprovalHostInstallation.bootstrapInstalledHost(
        metadata: try metadata(),
        preflight: FakePreflight(events: replacementEvents),
        factory: replacementFactory
      )
    ) { error in
      XCTAssertEqual(error as? ApprovalHostError, .hostNotInstalled)
    }
    XCTAssertEqual(replacementEvents.values, ["compile.host", "compile.client", "validate.host"])
  }

  func testLeaseStaysClaimedUntilListenerInvalidationReturns() throws {
    let events = BootstrapEvents()
    let firstFactory = FakeFactory(events: events)
    let first = try ApprovalHostInstallation.bootstrapInstalledHost(
      metadata: try metadata(),
      preflight: FakePreflight(events: events),
      factory: firstFactory
    )
    let duringShutdownEvents = BootstrapEvents()
    let duringShutdownFactory = FakeFactory(events: duringShutdownEvents)
    var refusedDuringInvalidation = false
    firstFactory.listener.onInvalidate = {
      do {
        _ = try ApprovalHostInstallation.bootstrapInstalledHost(
          metadata: try self.metadata(),
          preflight: FakePreflight(events: duringShutdownEvents),
          factory: duringShutdownFactory
        )
        XCTFail("a second runtime must not be constructed while shutdown invalidates the first")
      } catch let error as ApprovalHostError {
        refusedDuringInvalidation = error == .hostNotInstalled
      } catch {
        XCTFail("bootstrap returned an unexpected error type")
      }
    }

    first.shutdown()

    XCTAssertTrue(refusedDuringInvalidation)
    XCTAssertEqual(
      duringShutdownEvents.values, ["compile.host", "compile.client", "validate.host"])
  }

  func testStaleRuntimeBoundStoreRejectsWorkAfterTerminalShutdown() throws {
    let firstEvents = BootstrapEvents()
    let firstFactory = FakeFactory(events: firstEvents)
    let first = try ApprovalHostInstallation.bootstrapInstalledHost(
      metadata: try metadata(),
      preflight: FakePreflight(events: firstEvents),
      factory: firstFactory
    )
    let staleStore = try XCTUnwrap(firstFactory.engineStore)

    first.shutdown()
    XCTAssertThrowsError(try staleStore.validateConfiguredHostCodeIdentity()) { error in
      XCTAssertEqual(error as? ProtectedKeyStoreError, .unavailable)
    }
    XCTAssertEqual(firstFactory.rawStore.codeIdentityCalls, 0)

    let replacementEvents = BootstrapEvents()
    XCTAssertThrowsError(
      try ApprovalHostInstallation.bootstrapInstalledHost(
        metadata: try metadata(),
        preflight: FakePreflight(events: replacementEvents),
        factory: FakeFactory(events: replacementEvents)
      )
    ) { error in
      XCTAssertEqual(error as? ApprovalHostError, .hostNotInstalled)
    }
    XCTAssertThrowsError(try staleStore.validateConfiguredHostCodeIdentity()) { error in
      XCTAssertEqual(error as? ProtectedKeyStoreError, .unavailable)
    }
    XCTAssertEqual(firstFactory.rawStore.codeIdentityCalls, 0)
  }

  func testFactoryFailureReleasesLeaseWithoutActivatingListener() throws {
    let failedEvents = BootstrapEvents()
    let failingFactory = FakeFactory(events: failedEvents)
    failingFactory.failAt = .engine

    XCTAssertThrowsError(
      try ApprovalHostInstallation.bootstrapInstalledHost(
        metadata: try metadata(),
        preflight: FakePreflight(events: failedEvents),
        factory: failingFactory
      )
    ) { error in
      XCTAssertEqual(error as? ApprovalHostError, .hostNotInstalled)
    }
    XCTAssertEqual(
      failedEvents.values,
      ["compile.host", "compile.client", "validate.host", "store", "engine"]
    )
    XCTAssertEqual(failingFactory.listener.activateCount, 0)

    let recoveredEvents = BootstrapEvents()
    let recoveredFactory = FakeFactory(events: recoveredEvents)
    let recovered = try ApprovalHostInstallation.bootstrapInstalledHost(
      metadata: try metadata(),
      preflight: FakePreflight(events: recoveredEvents),
      factory: recoveredFactory
    )
    recovered.shutdown()
    XCTAssertEqual(recoveredFactory.listener.activateCount, 1)
  }

  @MainActor
  func testEnrollmentControllerComesOnlyFromActivePreflightedRuntime() throws {
    let events = BootstrapEvents()
    let factory = FakeFactory(events: events)
    let runtime = try ApprovalHostInstallation.bootstrapInstalledHost(
      metadata: try metadata(),
      preflight: FakePreflight(events: events),
      factory: factory
    )

    let interactionLease = try runtime.acquireLocalInteraction()
    _ = try runtime.makeEnrollmentController(interactionLease: interactionLease)
    runtime.shutdown()
    XCTAssertThrowsError(try runtime.makeEnrollmentController(interactionLease: interactionLease)) {
      error in
      XCTAssertEqual(error as? ApprovalHostError, .hostNotInstalled)
    }
  }

  func testFoundationLaunchRemainsExplicitlyUnconfigured() {
    XCTAssertThrowsError(try ApprovalHostInstallation.refuseUnconfiguredLaunch()) { error in
      XCTAssertEqual(error as? ApprovalHostError, .hostNotInstalled)
    }
  }

  private func metadata() throws -> ApprovalHostBuildMetadataV1 {
    try ApprovalHostBuildMetadataV1(
      hostReference: "host:adhelm-macos-approval-v1",
      signingKeyReference: "key:fixture-v1",
      machServiceName: "com.aptsonic.adhelm.approvalhost",
      clientCodeIdentity: try ApprovalHostCodeIdentitySelectorV1(
        role: .client,
        bundleIdentifier: "com.aptsonic.adhelm.client",
        teamIdentifier: "ABCDE12345"
      ),
      hostCodeIdentity: try ApprovalHostCodeIdentitySelectorV1(
        role: .host,
        bundleIdentifier: "com.aptsonic.adhelm.host",
        teamIdentifier: "ABCDE12345"
      ),
      signingKeyApplicationTag: Data("fixture-tag".utf8),
      keyStorage: .keychainP256,
      provenanceService: "com.aptsonic.adhelm.enrollment",
      provenanceAccount: "initial-slot"
    )
  }
}

private final class BootstrapEvents {
  private(set) var values: [String] = []
  func append(_ value: String) { values.append(value) }
}

private final class FakePreflight: InstalledApprovalHostPreflight {
  enum FailurePoint {
    case hostCompilation
    case clientCompilation
    case hostValidation
  }

  private let events: BootstrapEvents
  var failAt: FailurePoint?
  var expectedHostRequirement: String?
  var rejectIfHostRequirementEquals: String?
  private(set) var compiledHostRequirement: String?
  private(set) var compiledClientRequirement: String?
  private(set) var validatedCurrentHost = false

  init(events: BootstrapEvents) {
    self.events = events
  }

  func compileHostRequirement(_ requirement: String) throws {
    events.append("compile.host")
    if failAt == .hostCompilation { throw ApprovalHostError.hostNotInstalled }
    compiledHostRequirement = requirement
  }

  func compileClientRequirement(_ requirement: String) throws {
    events.append("compile.client")
    if failAt == .clientCompilation { throw ApprovalHostError.hostNotInstalled }
    compiledClientRequirement = requirement
  }

  func validateCurrentHost() throws {
    events.append("validate.host")
    guard failAt != .hostValidation,
      compiledHostRequirement == expectedHostRequirement || expectedHostRequirement == nil,
      compiledHostRequirement != rejectIfHostRequirementEquals
    else { throw ApprovalHostError.hostNotInstalled }
    validatedCurrentHost = true
  }
}

private final class FakeFactory: InstalledApprovalHostFactory {
  enum FailurePoint {
    case store
    case engine
    case listener
  }

  private let events: BootstrapEvents
  let listener: FakeListener
  let rawStore = FakeProtectedKeyStore()
  var failAt: FailurePoint?
  private(set) var engineStore: (any ProtectedKeyStore)?

  init(events: BootstrapEvents) {
    self.events = events
    listener = FakeListener(events: events)
  }

  func makeStore(configuration _: TrustedHostConfiguration) throws -> any ProtectedKeyStore {
    events.append("store")
    if failAt == .store { throw ApprovalHostError.hostNotInstalled }
    return rawStore
  }

  func makeEngine(
    configuration: TrustedHostConfiguration,
    store: any ProtectedKeyStore
  ) throws -> ApprovalHostEngine {
    events.append("engine")
    if failAt == .engine { throw ApprovalHostError.hostNotInstalled }
    engineStore = store
    return ApprovalHostEngine(
      configuration: configuration,
      reviewer: FakeReviewer(),
      authenticator: FakeAuthenticator(),
      signer: FakeSigner()
    )
  }

  func makeListener(
    configuration _: TrustedHostConfiguration,
    engine _: ApprovalHostEngine,
    interactionGate _: InstalledApprovalHostInteractionGate
  ) throws -> any InstalledApprovalHostListener {
    events.append("listener")
    if failAt == .listener { throw ApprovalHostError.hostNotInstalled }
    return listener
  }
}

private final class FakeListener: InstalledApprovalHostListener {
  private let events: BootstrapEvents
  private(set) var activateCount = 0
  private(set) var invalidateCount = 0
  var onInvalidate: (() -> Void)?

  init(events: BootstrapEvents) {
    self.events = events
  }

  func activate() {
    activateCount += 1
    events.append("activate")
  }

  func invalidate() {
    invalidateCount += 1
    events.append("invalidate")
    onInvalidate?()
  }
}

private final class FakeProtectedKeyStore: ProtectedKeyStore, @unchecked Sendable {
  private(set) var codeIdentityCalls = 0

  func validateConfiguredHostCodeIdentity() throws {
    codeIdentityCalls += 1
  }

  func enrollInitialPrivateKey(
    authenticatedSession _: LocalAuthenticationApprovalSession,
    shouldContinue _: () -> Bool
  ) throws -> InitialEnrollmentActive {
    throw ProtectedKeyStoreError.unavailable
  }

  func revokeInitialPrivateKey(
    authenticatedSession _: LocalAuthenticationApprovalSession,
    shouldContinue _: () -> Bool
  ) throws -> InitialEnrollmentRevoked {
    throw ProtectedKeyStoreError.unavailable
  }

  func signApprovedReceiptPayload(
    _: ReceiptSigningPayload,
    authenticatedSession _: LocalAuthenticationApprovalSession,
    claimPermit _: () -> Bool
  ) throws -> Data {
    throw ProtectedKeyStoreError.unavailable
  }
}

private final class FakeReviewer: ApprovalReviewer {
  func present(
    review _: DeterministicApprovalReview,
    completion _: @escaping (ApprovalReviewDecision) -> Void
  ) -> any ApprovalCancellation {
    NoopApprovalCancellation()
  }
}

private final class FakeAuthenticator: FreshAuthenticator {
  func authenticate(
    reason _: String,
    completion _: @escaping (ApprovalAuthenticationResult) -> Void
  ) -> any ApprovalCancellation {
    NoopApprovalCancellation()
  }
}

private final class FakeSigner: ReceiptSigner {
  func sign(
    _: ReceiptSigningPayload,
    authenticatedSession _: any AuthenticatedApprovalSession,
    permit _: any ReceiptSigningPermit,
    completion _: @escaping (Result<Data, ApprovalHostError>) -> Void
  ) -> any ApprovalCancellation {
    NoopApprovalCancellation()
  }
}
