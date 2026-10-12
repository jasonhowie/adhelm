import ApprovalHostCore
import Foundation

/// Internal listener surface used by the installed bootstrap.  The concrete
/// listener is constructed only after the bootstrap's fixed-metadata and
/// current-host-code checks succeed; XCTest supplies an in-memory listener.
protocol InstalledApprovalHostListener: AnyObject {
  func activate()
  func invalidate()
}

extension AuthenticatedApprovalXPCListener: InstalledApprovalHostListener {}

/// Internal construction seam for the installed bootstrap.  It separates
/// preflight from construction so XCTest can prove that no store, review UI,
/// engine, or XPC listener is made before the host code is checked.  Product
/// code never receives this seam.
protocol InstalledApprovalHostFactory: AnyObject {
  func makeStore(configuration: TrustedHostConfiguration) throws -> any ProtectedKeyStore
  func makeEngine(
    configuration: TrustedHostConfiguration,
    store: any ProtectedKeyStore
  ) throws -> ApprovalHostEngine
  func makeListener(
    configuration: TrustedHostConfiguration,
    engine: ApprovalHostEngine,
    interactionGate: InstalledApprovalHostInteractionGate
  ) throws -> any InstalledApprovalHostListener
}

/// The production factory additionally constructs the one statically bound
/// post-signing bridge listener. XCTest factories intentionally need not adopt
/// this protocol: their legacy listener keeps the bridge endpoint fail-closed.
/// The alternate method receives the raw fixed store only during bootstrap so
/// it can retain that store's existing coordinator; it cannot derive a store
/// from an XPC request or runtime configuration value.
protocol InstalledApprovalHostNativeBridgeFactory: InstalledApprovalHostFactory {
  func makeNativeBridgeListener(
    metadata: ApprovalHostBuildMetadataV1,
    configuration: TrustedHostConfiguration,
    rawStore: any ProtectedKeyStore,
    engine: ApprovalHostEngine,
    interactionGate: InstalledApprovalHostInteractionGate,
    operationGate: InstalledApprovalHostOperationGate
  ) throws -> any InstalledApprovalHostListener
}

/// A small runtime-generation gate layered over the existing fixed-slot store
/// protocol.  It is not a second coordinator or a cross-process lock.  It
/// prevents a controller or pending in-process flow retained from a shut-down
/// runtime from starting another protected operation after shutdown.
final class InstalledApprovalHostOperationGate: @unchecked Sendable {
  private enum State {
    case prepared
    case active
    case invalidated
  }

  private let lock = NSLock()
  private var state: State = .prepared

  func activate() {
    lock.lock()
    if case .prepared = state { state = .active }
    lock.unlock()
  }

  func invalidate() {
    lock.lock()
    state = .invalidated
    lock.unlock()
  }

  func requireActive() throws {
    lock.lock()
    let active: Bool
    if case .active = state {
      active = true
    } else {
      active = false
    }
    lock.unlock()
    guard active else { throw ProtectedKeyStoreError.unavailable }
  }

  var isActive: Bool {
    lock.lock()
    defer { lock.unlock() }
    if case .active = state { return true }
    return false
  }
}

/// Keeps the existing fixed-slot coordinator and store interface intact while
/// making that one store usable only for its owning runtime generation.
final class RuntimeBoundProtectedKeyStore: ProtectedKeyStore, ReceiptRedemptionGlobalSetupStore,
  @unchecked Sendable
{
  private let store: any ProtectedKeyStore
  private let gate: InstalledApprovalHostOperationGate

  init(store: any ProtectedKeyStore, gate: InstalledApprovalHostOperationGate) {
    self.store = store
    self.gate = gate
  }

  /// Internal construction check only. It is never returned through XPC or a
  /// status API; it prevents a fake/default protected store from acquiring an
  /// explicit setup controller merely because it was runtime-wrapped.
  var hasReceiptRedemptionGlobalSetupCapability: Bool {
    store is any ReceiptRedemptionGlobalSetupStore
  }

  func validateConfiguredHostCodeIdentity() throws {
    try gate.requireActive()
    try store.validateConfiguredHostCodeIdentity()
  }

  func enrollInitialPrivateKey(
    authenticatedSession: LocalAuthenticationApprovalSession,
    shouldContinue: () -> Bool
  ) throws -> InitialEnrollmentActive {
    try gate.requireActive()
    return try store.enrollInitialPrivateKey(
      authenticatedSession: authenticatedSession,
      shouldContinue: { self.gate.isActive && shouldContinue() }
    )
  }

  func revokeInitialPrivateKey(
    authenticatedSession: LocalAuthenticationApprovalSession,
    shouldContinue: () -> Bool
  ) throws -> InitialEnrollmentRevoked {
    try gate.requireActive()
    return try store.revokeInitialPrivateKey(
      authenticatedSession: authenticatedSession,
      shouldContinue: { self.gate.isActive && shouldContinue() }
    )
  }

  func signApprovedReceiptPayload(
    _ payload: ReceiptSigningPayload,
    authenticatedSession: LocalAuthenticationApprovalSession,
    claimPermit: () -> Bool
  ) throws -> Data {
    try gate.requireActive()
    return try store.signApprovedReceiptPayload(
      payload,
      authenticatedSession: authenticatedSession,
      claimPermit: { self.gate.isActive && claimPermit() }
    )
  }

  /// Forwards only the closed explicit setup capability of this runtime's
  /// fixed store. A normal `ProtectedKeyStore` does not acquire the capability
  /// by wrapping it, and the runtime generation gate remains live for every
  /// protected step.
  func initializeReceiptRedemptionGlobalState(
    authenticatedSession: any AuthenticatedApprovalSession,
    shouldContinue: @escaping () -> Bool
  ) throws -> ReceiptRedemptionGlobalInitializationResult {
    try gate.requireActive()
    guard let setupStore = store as? any ReceiptRedemptionGlobalSetupStore else {
      throw ProtectedKeyStoreError.unavailable
    }
    return try setupStore.initializeReceiptRedemptionGlobalState(
      authenticatedSession: authenticatedSession,
      shouldContinue: { self.gate.isActive && shouldContinue() }
    )
  }
}

/// The only production factory.  Its constructors do not perform a Keychain
/// query, create an LAContext, or display review UI.  The bootstrap calls them
/// only after it has compiled the fixed host/client requirements and checked
/// the current host code against the fixed host requirement.
final class SecurityInstalledApprovalHostFactory: InstalledApprovalHostNativeBridgeFactory {
  func makeStore(configuration: TrustedHostConfiguration) throws -> any ProtectedKeyStore {
    SecurityProtectedKeyStore(configuration: configuration)
  }

  func makeEngine(
    configuration: TrustedHostConfiguration,
    store: any ProtectedKeyStore
  ) throws -> ApprovalHostEngine {
    ApprovalHostEngine(
      configuration: configuration,
      reviewer: AppKitApprovalReviewer(),
      authenticator: LocalAuthenticationFreshAuthenticator(),
      signer: SecurityProtectedKeyReceiptSigner(store: store)
    )
  }

  func makeListener(
    configuration: TrustedHostConfiguration,
    engine: ApprovalHostEngine,
    interactionGate: InstalledApprovalHostInteractionGate
  ) throws -> any InstalledApprovalHostListener {
    AuthenticatedApprovalXPCListener(
      configuration: configuration, engine: engine, interactionGate: interactionGate
    )
  }

  func makeNativeBridgeListener(
    metadata: ApprovalHostBuildMetadataV1,
    configuration: TrustedHostConfiguration,
    rawStore: any ProtectedKeyStore,
    engine: ApprovalHostEngine,
    interactionGate: InstalledApprovalHostInteractionGate,
    operationGate: InstalledApprovalHostOperationGate
  ) throws -> any InstalledApprovalHostListener {
    guard let securityStore = rawStore as? SecurityProtectedKeyStore else {
      throw ApprovalHostError.hostNotInstalled
    }
    let metaCampaignPauseRouteBinding: NativeMetaCampaignPauseApprovalRouteBindingV2?
    do {
      // The checked-in generated resolver returns nil. A separately built
      // profile may return only a digest-bound V2 route; it never receives a
      // caller-selected runtime flag, request, or configuration source.
      metaCampaignPauseRouteBinding = try GeneratedMetaCampaignPauseApprovalRouteBinding.resolve(
        metadata: metadata,
        configuration: configuration
      )
    } catch {
      throw ApprovalHostError.hostNotInstalled
    }
    let factories = try securityStore.makeNativeApprovalBridgePostSigningConsumerFactories(
      metadata: metadata,
      configuration: configuration,
      operationGate: operationGate,
      metaCampaignPauseRouteBinding: metaCampaignPauseRouteBinding
    )
    return AuthenticatedApprovalXPCListener(
      configuration: configuration,
      engine: engine,
      interactionGate: interactionGate,
      nativeBridgeConsumerFactory: factories.v1,
      nativeMetaCampaignPauseBridgeConsumerFactory: factories.metaCampaignPauseV2
    )
  }
}

/// Retains exactly one configuration, protected store, engine, and listener
/// for one bootstrap invocation.  The bootstrap activates the listener before
/// returning this value.  `shutdown()` is idempotent and invokes listener
/// invalidation at most once; it does not delete a protected key, alter
/// enrollment provenance, or infer recovery.
package final class InstalledApprovalHostRuntime: @unchecked Sendable {
  private enum Lifecycle {
    case prepared
    case active
    case shutDown
  }

  private let configuration: TrustedHostConfiguration
  private let store: any ProtectedKeyStore
  private let engine: ApprovalHostEngine
  private let listener: any InstalledApprovalHostListener
  private let operationGate: InstalledApprovalHostOperationGate
  private let interactionGate: InstalledApprovalHostInteractionGate
  private let bootstrapLease: UUID?
  private let lock = NSLock()
  private var lifecycle: Lifecycle = .prepared

  init(
    configuration: TrustedHostConfiguration,
    store: any ProtectedKeyStore,
    engine: ApprovalHostEngine,
    listener: any InstalledApprovalHostListener,
    operationGate: InstalledApprovalHostOperationGate,
    interactionGate: InstalledApprovalHostInteractionGate,
    bootstrapLease: UUID? = nil
  ) {
    self.configuration = configuration
    self.store = store
    self.engine = engine
    self.listener = listener
    self.operationGate = operationGate
    self.interactionGate = interactionGate
    self.bootstrapLease = bootstrapLease
  }

  /// This is intentionally called only by the bootstrap before it returns the
  /// runtime, so no caller can race activation with shutdown.
  func activateBeforeReturn() {
    lock.lock()
    guard case .prepared = lifecycle else {
      lock.unlock()
      return
    }
    lifecycle = .active
    lock.unlock()
    operationGate.activate()
    interactionGate.activate()
    listener.activate()
  }

  /// Stops the one installed listener.  Repeated shutdown requests and object
  /// teardown do not reactivate it or invalidate it more than once.
  package func shutdown() {
    lock.lock()
    if case .shutDown = lifecycle {
      lock.unlock()
      return
    }
    operationGate.invalidate()
    interactionGate.invalidate()
    lifecycle = .shutDown
    lock.unlock()
    listener.invalidate()
    if let bootstrapLease {
      ProcessInstalledApprovalHostRuntimeRegistry.shared.terminate(
        configuration: configuration,
        lease: bootstrapLease
      )
    }
  }

  var isInteractionAvailable: Bool { interactionGate.isAvailable }

  func acquireLocalInteraction() throws -> InstalledApprovalHostInteractionLease {
    try interactionGate.acquire()
  }

  /// Enrollment remains an explicit local UI operation, but the controller can
  /// only be constructed from an already-preflighted runtime and shares that
  /// runtime's fixed protected store.  It is unavailable after shutdown and
  /// never accepts a caller-selected selector, key, or code requirement.
  @MainActor
  func makeEnrollmentController(
    interactionLease: InstalledApprovalHostInteractionLease
  ) throws -> AppKitProtectedKeyEnrollmentController {
    try interactionGate.requireLease(interactionLease)
    lock.lock()
    guard case .active = lifecycle, configuration.initialEnrollment != nil else {
      lock.unlock()
      throw ApprovalHostError.hostNotInstalled
    }
    lock.unlock()
    return AppKitProtectedKeyEnrollmentController(
      store: store,
      authenticator: LocalAuthenticationFreshAuthenticator(),
      requireInteraction: { [interactionGate] in
        try interactionGate.requireLease(interactionLease)
      }
    )
  }

  /// Constructs the one local status-menu controller for the explicit V2
  /// redemption-global setup sequence. It remains unavailable to XPC,
  /// challenge, CLI, helper, and automatic enrollment paths. The controller
  /// receives the runtime-bound store, never a caller-selected raw store.
  @MainActor
  func makeReceiptRedemptionGlobalSetupController(
    interactionLease: InstalledApprovalHostInteractionLease
  ) throws -> AppKitReceiptRedemptionGlobalSetupController {
    try interactionGate.requireLease(interactionLease)
    lock.lock()
    guard case .active = lifecycle, configuration.initialEnrollment != nil else {
      lock.unlock()
      throw ApprovalHostError.hostNotInstalled
    }
    lock.unlock()
    guard let setupStore = store as? RuntimeBoundProtectedKeyStore,
      setupStore.hasReceiptRedemptionGlobalSetupCapability
    else {
      throw ApprovalHostError.hostNotInstalled
    }
    return AppKitReceiptRedemptionGlobalSetupController(
      store: setupStore,
      authenticator: LocalAuthenticationFreshAuthenticator(),
      requireInteraction: { [interactionGate] in
        try interactionGate.requireLease(interactionLease)
      }
    )
  }

  deinit {
    shutdown()
  }
}

/// Prevents two bootstraps in this host process from constructing competing
/// stores, engines, or Mach listeners.  It is deliberately process-local: it
/// neither claims an installation nor provides a cross-process mutex.  A
/// shutdown makes its exact lease terminal only after listener invalidation.
/// Once a runtime was active, this process cannot bootstrap a replacement;
/// process restart is required. That conservative rule prevents a blocked old
/// protected operation from overlapping a new store/runtime after shutdown.
final class ProcessInstalledApprovalHostRuntimeRegistry: @unchecked Sendable {
  static let shared = ProcessInstalledApprovalHostRuntimeRegistry()

  private enum State {
    case idle
    case starting(configuration: TrustedHostConfiguration, lease: UUID)
    case active(configuration: TrustedHostConfiguration, lease: UUID)
    case terminated
  }

  private let lock = NSLock()
  private var state: State = .idle

  func claim(configuration: TrustedHostConfiguration) throws -> UUID {
    lock.lock()
    defer { lock.unlock() }
    guard case .idle = state else { throw ApprovalHostError.hostNotInstalled }
    let lease = UUID()
    state = .starting(configuration: configuration, lease: lease)
    return lease
  }

  func activate(configuration: TrustedHostConfiguration, lease: UUID) {
    lock.lock()
    defer { lock.unlock() }
    guard case .starting(let currentConfiguration, let currentLease) = state,
      currentConfiguration == configuration,
      currentLease == lease
    else { return }
    state = .active(configuration: configuration, lease: lease)
  }

  func abandon(configuration: TrustedHostConfiguration, lease: UUID) {
    lock.lock()
    defer { lock.unlock() }
    guard case .starting(let currentConfiguration, let currentLease) = state,
      currentConfiguration == configuration,
      currentLease == lease
    else { return }
    state = .idle
  }

  func terminate(configuration: TrustedHostConfiguration, lease: UUID) {
    lock.lock()
    defer { lock.unlock() }
    guard case .active(let currentConfiguration, let currentLease) = state,
      currentConfiguration == configuration,
      currentLease == lease
    else { return }
    state = .terminated
  }

  /// XCTest runs multiple isolated cases in one process. This internal test
  /// reset is unavailable to a separately compiled product target and only
  /// accepts an already terminal or idle state; it cannot clear a live claim.
  func resetForTesting() {
    lock.lock()
    defer { lock.unlock() }
    switch state {
    case .idle, .terminated:
      state = .idle
    case .starting, .active:
      return
    }
  }
}
