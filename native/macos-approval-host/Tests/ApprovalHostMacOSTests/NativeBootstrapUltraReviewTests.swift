import ApprovalHostCore
@preconcurrency import Dispatch
import Foundation
import XCTest

@testable import ApprovalHostMacOS

/// Independent adversarial coverage for the installed-bootstrap boundary.
///
/// Every collaborator below is an in-memory fake. These tests do not create a
/// Security object, Keychain item, LocalAuthentication context, XPC listener,
/// installed service, signature, or provider request.
final class NativeBootstrapUltraReviewTests: XCTestCase {
  override func tearDown() {
    ProcessInstalledApprovalHostRuntimeRegistry.shared.resetForTesting()
    super.tearDown()
  }

  func testProtocolProjectionMatchesPythonDefaultAndDoesNotOverclaimInstallationScope() throws {
    let baseline = try metadata()
    let projection = try baseline.makeTrustedHostConfiguration().approvalProtocolConfigurationV1

    XCTAssertEqual(
      String(data: projection.canonicalBytes, encoding: .utf8),
      "{\"host_ref\":\"host:adhelm-macos-approval-v1\",\"maximum_challenge_bytes\":65536,"
        + "\"maximum_challenge_lifetime_seconds\":300,\"maximum_issued_at_skew_seconds\":60,"
        + "\"maximum_policy_items\":64,\"maximum_subplans\":6,"
        + "\"signing_key_ref\":\"key:receipt-v1\"}"
    )
    XCTAssertEqual(
      projection.configurationDigest,
      "6c287f0d5c7d2b5c2a7aba6c0aaffbb9ffa2715946b0d0eee59b5fc8477fa7ee"
    )

    // The documented digest deliberately excludes installed-product selectors.
    // A changed Mach service or provenance selector must not be mislabeled as
    // an attested protocol change.
    let changedInstallation = try metadata(
      machServiceName: "com.aptsonic.adhelm.differentapprovalhost",
      provenanceAccount: "different-initial-slot"
    )
    let installationProjection = try changedInstallation.makeTrustedHostConfiguration()
      .approvalProtocolConfigurationV1
    XCTAssertEqual(installationProjection.canonicalBytes, projection.canonicalBytes)
    XCTAssertEqual(installationProjection.configurationDigest, projection.configurationDigest)

    let changedProtocol = try metadata(hostReference: "host:adhelm-macos-approval-v2")
    let changedProtocolProjection = try changedProtocol.makeTrustedHostConfiguration()
      .approvalProtocolConfigurationV1
    XCTAssertNotEqual(changedProtocolProjection.configurationDigest, projection.configurationDigest)
  }

  func testMetadataRejectsUnsafeSelectorsAndNonIntegralLimitsBeforePreflight() throws {
    let host = try selector(role: .host, bundleIdentifier: "com.aptsonic.adhelm.host")
    let client = try selector(role: .client, bundleIdentifier: "com.aptsonic.adhelm.client")

    XCTAssertThrowsError(
      try ApprovalHostCodeIdentitySelectorV1(
        role: .client,
        bundleIdentifier: "com.example.*",
        teamIdentifier: "ABCDE12345"
      ))
    XCTAssertThrowsError(
      try ApprovalHostCodeIdentitySelectorV1(
        role: .client,
        bundleIdentifier: "com.example.placeholder.client",
        teamIdentifier: "ABCDE12345"
      ))
    XCTAssertThrowsError(
      try ApprovalHostCodeIdentitySelectorV1(
        role: .client,
        bundleIdentifier: "com.example.client\" or true",
        teamIdentifier: "ABCDE12345"
      ))
    XCTAssertThrowsError(try metadata(client: host, host: client))
    XCTAssertThrowsError(
      try metadata(
        limits: ApprovalHostLimits(maximumChallengeLifetime: 12.5)
      ))
    XCTAssertThrowsError(
      try metadata(
        limits: ApprovalHostLimits(maximumIssuedAtSkew: .nan)
      ))

    let valid = try metadata()
    XCTAssertEqual(
      valid.expectedHostCodeSigningRequirement,
      "anchor apple generic and identifier \"com.aptsonic.adhelm.host\""
        + " and certificate leaf[subject.OU] = \"ABCDE12345\""
    )
    XCTAssertEqual(
      valid.expectedClientCodeSigningRequirement,
      "anchor apple generic and identifier \"com.aptsonic.adhelm.client\""
        + " and certificate leaf[subject.OU] = \"ABCDE12345\""
    )
  }

  func testUnknownPreflightFailureIsSanitizedAndPreventsEveryConstructionStep() throws {
    let preflight = UltraPreflight(failure: .customHostCompilation)
    let factory = UltraFactory(store: UltraBlockingStore())

    XCTAssertThrowsError(
      try ApprovalHostInstallation.bootstrapInstalledHost(
        metadata: try metadata(),
        preflight: preflight,
        factory: factory
      )
    ) { error in
      XCTAssertEqual(error as? ApprovalHostError, .hostNotInstalled)
      XCTAssertFalse(String(describing: error).contains("private-ultra-preflight-sentinel"))
    }
    XCTAssertEqual(preflight.events, ["compile.host"])
    XCTAssertEqual(factory.storeBuilds, 0)
    XCTAssertEqual(factory.engineBuilds, 0)
    XCTAssertEqual(factory.listenerBuilds, 0)
  }

  func testShutdownMakesBootstrapTerminalWhileAnOldFakeStoreCallIsStillInFlight() throws {
    let rawStore = UltraBlockingStore()
    let firstFactory = UltraFactory(store: rawStore)
    let runtime = try ApprovalHostInstallation.bootstrapInstalledHost(
      metadata: try metadata(),
      preflight: UltraPreflight(),
      factory: firstFactory
    )
    let guardedStore = try XCTUnwrap(firstFactory.engineStore)

    let callFinished = expectation(description: "blocked fake store call finishes")
    DispatchQueue.global(qos: .userInitiated).async {
      defer { callFinished.fulfill() }
      do {
        try guardedStore.validateConfiguredHostCodeIdentity()
      } catch {
        XCTFail("the operation was admitted before shutdown and should complete through the fake")
      }
    }
    XCTAssertEqual(rawStore.entered.wait(timeout: .now() + 1), .success)

    runtime.shutdown()
    XCTAssertEqual(firstFactory.listener.invalidateCount, 1)

    let replacementFactory = UltraFactory(store: UltraBlockingStore())
    XCTAssertThrowsError(
      try ApprovalHostInstallation.bootstrapInstalledHost(
        metadata: try metadata(),
        preflight: UltraPreflight(),
        factory: replacementFactory
      )
    ) { error in
      XCTAssertEqual(error as? ApprovalHostError, .hostNotInstalled)
    }
    XCTAssertEqual(replacementFactory.storeBuilds, 0)
    XCTAssertEqual(replacementFactory.engineBuilds, 0)
    XCTAssertEqual(replacementFactory.listenerBuilds, 0)

    rawStore.release.signal()
    wait(for: [callFinished], timeout: 1)

    // The conservative terminal lease remains in force after the earlier
    // operation returns, so the process cannot produce a second runtime.
    let laterFactory = UltraFactory(store: UltraBlockingStore())
    XCTAssertThrowsError(
      try ApprovalHostInstallation.bootstrapInstalledHost(
        metadata: try metadata(),
        preflight: UltraPreflight(),
        factory: laterFactory
      )
    ) { error in
      XCTAssertEqual(error as? ApprovalHostError, .hostNotInstalled)
    }
    XCTAssertEqual(laterFactory.storeBuilds, 0)
  }

  func testConcurrentBootstrapAttemptsConstructAtMostOneFakeRuntime() throws {
    let barrier = UltraConcurrentPreflight()
    let firstFactory = UltraFactory(store: UltraBlockingStore())
    let secondFactory = UltraFactory(store: UltraBlockingStore())
    let results = UltraBootstrapResults()
    let group = DispatchGroup()
    let firstMetadata = try metadata()
    let secondMetadata = try metadata()

    for (metadata, factory) in [(firstMetadata, firstFactory), (secondMetadata, secondFactory)] {
      group.enter()
      DispatchQueue.global(qos: .userInitiated).async {
        defer { group.leave() }
        do {
          results.record(
            runtime: try ApprovalHostInstallation.bootstrapInstalledHost(
              metadata: metadata,
              preflight: barrier,
              factory: factory
            ))
        } catch {
          results.record(error: error)
        }
      }
    }

    let bothReachedPreflight = barrier.bothReached.wait(timeout: .now() + 1)
    barrier.release.signal()
    barrier.release.signal()
    XCTAssertEqual(bothReachedPreflight, .success)
    XCTAssertEqual(group.wait(timeout: .now() + 1), .success)

    XCTAssertEqual(results.runtimeCount, 1)
    XCTAssertEqual(results.hostNotInstalledFailureCount, 1)
    XCTAssertEqual(firstFactory.storeBuilds + secondFactory.storeBuilds, 1)
    XCTAssertEqual(firstFactory.engineBuilds + secondFactory.engineBuilds, 1)
    XCTAssertEqual(firstFactory.listenerBuilds + secondFactory.listenerBuilds, 1)

    try XCTUnwrap(results.firstRuntime).shutdown()
  }

  private func metadata(
    hostReference: String = "host:adhelm-macos-approval-v1",
    machServiceName: String = "com.aptsonic.adhelm.approvalhost",
    provenanceAccount: String = "initial-slot",
    limits: ApprovalHostLimits = ApprovalHostLimits(),
    client: ApprovalHostCodeIdentitySelectorV1? = nil,
    host: ApprovalHostCodeIdentitySelectorV1? = nil
  ) throws -> ApprovalHostBuildMetadataV1 {
    try ApprovalHostBuildMetadataV1(
      hostReference: hostReference,
      signingKeyReference: "key:receipt-v1",
      machServiceName: machServiceName,
      clientCodeIdentity: try client
        ?? selector(
          role: .client,
          bundleIdentifier: "com.aptsonic.adhelm.client"
        ),
      hostCodeIdentity: try host
        ?? selector(
          role: .host,
          bundleIdentifier: "com.aptsonic.adhelm.host"
        ),
      signingKeyApplicationTag: Data("ultra-bootstrap-tag-v1".utf8),
      signingKeyAccessGroup: "ABCDE12345.com.aptsonic.adhelm.host",
      keyStorage: .keychainP256,
      provenanceService: "com.aptsonic.adhelm.enrollment",
      provenanceAccount: provenanceAccount,
      limits: limits
    )
  }

  private func selector(
    role: ApprovalHostCodeIdentityRoleV1,
    bundleIdentifier: String
  ) throws -> ApprovalHostCodeIdentitySelectorV1 {
    try ApprovalHostCodeIdentitySelectorV1(
      role: role,
      bundleIdentifier: bundleIdentifier,
      teamIdentifier: "ABCDE12345"
    )
  }
}

private final class UltraPreflight: InstalledApprovalHostPreflight, @unchecked Sendable {
  enum Failure {
    case customHostCompilation
  }

  private let failure: Failure?
  private(set) var events: [String] = []

  init(failure: Failure? = nil) {
    self.failure = failure
  }

  func compileHostRequirement(_: String) throws {
    events.append("compile.host")
    if failure == .customHostCompilation { throw UltraPrivatePreflightError() }
  }

  func compileClientRequirement(_: String) throws {
    events.append("compile.client")
  }

  func validateCurrentHost() throws {
    events.append("validate.host")
  }
}

private final class UltraConcurrentPreflight: InstalledApprovalHostPreflight, @unchecked Sendable {
  let bothReached = DispatchSemaphore(value: 0)
  let release = DispatchSemaphore(value: 0)
  private let lock = NSLock()
  private var hostValidationCount = 0

  func compileHostRequirement(_: String) throws {}
  func compileClientRequirement(_: String) throws {}

  func validateCurrentHost() throws {
    lock.lock()
    hostValidationCount += 1
    let isSecond = hostValidationCount == 2
    lock.unlock()
    if isSecond { bothReached.signal() }
    _ = release.wait(timeout: .now() + 3)
  }
}

private struct UltraPrivatePreflightError: Error, CustomStringConvertible {
  var description: String { "private-ultra-preflight-sentinel" }
}

private final class UltraFactory: InstalledApprovalHostFactory, @unchecked Sendable {
  let rawStore: UltraBlockingStore
  let listener = UltraListener()
  private(set) var storeBuilds = 0
  private(set) var engineBuilds = 0
  private(set) var listenerBuilds = 0
  private(set) var engineStore: (any ProtectedKeyStore)?

  init(store: UltraBlockingStore) {
    rawStore = store
  }

  func makeStore(configuration _: TrustedHostConfiguration) throws -> any ProtectedKeyStore {
    storeBuilds += 1
    return rawStore
  }

  func makeEngine(
    configuration: TrustedHostConfiguration,
    store: any ProtectedKeyStore
  ) throws -> ApprovalHostEngine {
    engineBuilds += 1
    engineStore = store
    return ApprovalHostEngine(
      configuration: configuration,
      reviewer: UltraReviewer(),
      authenticator: UltraAuthenticator(),
      signer: UltraSigner()
    )
  }

  func makeListener(
    configuration _: TrustedHostConfiguration,
    engine _: ApprovalHostEngine,
    interactionGate _: InstalledApprovalHostInteractionGate
  ) throws -> any InstalledApprovalHostListener {
    listenerBuilds += 1
    return listener
  }
}

private final class UltraBootstrapResults: @unchecked Sendable {
  private let lock = NSLock()
  private var runtime: InstalledApprovalHostRuntime?
  private var errors: [Error] = []

  func record(runtime: InstalledApprovalHostRuntime) {
    lock.lock()
    self.runtime = runtime
    lock.unlock()
  }

  func record(error: Error) {
    lock.lock()
    errors.append(error)
    lock.unlock()
  }

  var firstRuntime: InstalledApprovalHostRuntime? {
    lock.lock()
    defer { lock.unlock() }
    return runtime
  }

  var runtimeCount: Int {
    lock.lock()
    defer { lock.unlock() }
    return runtime == nil ? 0 : 1
  }

  var hostNotInstalledFailureCount: Int {
    lock.lock()
    defer { lock.unlock() }
    return errors.reduce(into: 0) { count, error in
      if (error as? ApprovalHostError) == .hostNotInstalled { count += 1 }
    }
  }
}

private final class UltraListener: InstalledApprovalHostListener, @unchecked Sendable {
  private(set) var activateCount = 0
  private(set) var invalidateCount = 0

  func activate() { activateCount += 1 }
  func invalidate() { invalidateCount += 1 }
}

private final class UltraBlockingStore: ProtectedKeyStore, @unchecked Sendable {
  let entered = DispatchSemaphore(value: 0)
  let release = DispatchSemaphore(value: 0)

  func validateConfiguredHostCodeIdentity() throws {
    entered.signal()
    _ = release.wait(timeout: .now() + 3)
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

private final class UltraReviewer: ApprovalReviewer {
  func present(
    review _: DeterministicApprovalReview,
    completion _: @escaping (ApprovalReviewDecision) -> Void
  ) -> any ApprovalCancellation {
    NoopApprovalCancellation()
  }
}

private final class UltraAuthenticator: FreshAuthenticator {
  func authenticate(
    reason _: String,
    completion _: @escaping (ApprovalAuthenticationResult) -> Void
  ) -> any ApprovalCancellation {
    NoopApprovalCancellation()
  }
}

private final class UltraSigner: ReceiptSigner {
  func sign(
    _: ReceiptSigningPayload,
    authenticatedSession _: any AuthenticatedApprovalSession,
    permit _: any ReceiptSigningPermit,
    completion _: @escaping (Result<Data, ApprovalHostError>) -> Void
  ) -> any ApprovalCancellation {
    NoopApprovalCancellation()
  }
}
