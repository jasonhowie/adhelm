import ApprovalHostCore
import Foundation
@preconcurrency import Security

/// A narrow, fakeable preflight for the installed product bootstrap.  It
/// compiles both generated requirements and checks the current host code only
/// against the generated host requirement.  The values are build metadata,
/// never configuration-file, environment, XPC, or challenge input.
protocol InstalledApprovalHostPreflight: AnyObject {
  func compileHostRequirement(_ requirement: String) throws
  func compileClientRequirement(_ requirement: String) throws
  func validateCurrentHost() throws
}

final class SecurityInstalledApprovalHostPreflight: InstalledApprovalHostPreflight {
  private var hostRequirement: SecRequirement?

  func compileHostRequirement(_ requirement: String) throws {
    var hostRequirement: SecRequirement?
    guard
      SecRequirementCreateWithString(
        requirement as CFString,
        [],
        &hostRequirement
      ) == errSecSuccess, let hostRequirement
    else { throw ApprovalHostError.hostNotInstalled }
    self.hostRequirement = hostRequirement
  }

  func compileClientRequirement(_ requirement: String) throws {
    var clientRequirement: SecRequirement?
    guard
      SecRequirementCreateWithString(
        requirement as CFString,
        [],
        &clientRequirement
      ) == errSecSuccess, clientRequirement != nil
    else { throw ApprovalHostError.hostNotInstalled }
  }

  func validateCurrentHost() throws {
    var currentHost: SecCode?
    guard SecCodeCopySelf([], &currentHost) == errSecSuccess,
      let currentHost,
      let hostRequirement,
      SecCodeCheckValidity(currentHost, [], hostRequirement) == errSecSuccess
    else { throw ApprovalHostError.hostNotInstalled }
  }
}

extension ApprovalHostInstallation {
  /// Starts one installed runtime from static, reviewed build metadata.  This
  /// package-scoped entry point is for a future signed product target in this
  /// package; the checked-in foundation executable deliberately does not call
  /// it and continues to refuse unconfigured launch.
  package static func bootstrapInstalledHost(
    metadata: ApprovalHostBuildMetadataV1
  ) throws -> InstalledApprovalHostRuntime {
    try bootstrapInstalledHost(
      metadata: metadata,
      preflight: SecurityInstalledApprovalHostPreflight(),
      factory: SecurityInstalledApprovalHostFactory()
    )
  }

  /// XCTest-only construction seam.  It is module-internal so a product target
  /// cannot inject a fake preflight, store, engine, or listener.
  static func bootstrapInstalledHost(
    metadata: ApprovalHostBuildMetadataV1,
    preflight: any InstalledApprovalHostPreflight,
    factory: any InstalledApprovalHostFactory
  ) throws -> InstalledApprovalHostRuntime {
    do {
      let configuration = try metadata.makeTrustedHostConfiguration()
      try preflight.compileHostRequirement(configuration.expectedHostCodeSigningRequirement)
      try preflight.compileClientRequirement(configuration.expectedClientCodeSigningRequirement)
      try preflight.validateCurrentHost()
      let lease = try ProcessInstalledApprovalHostRuntimeRegistry.shared.claim(
        configuration: configuration
      )

      do {
        let runtime = try buildRuntime(
          metadata: metadata,
          configuration: configuration,
          lease: lease,
          factory: factory
        )
        runtime.activateBeforeReturn()
        ProcessInstalledApprovalHostRuntimeRegistry.shared.activate(
          configuration: configuration,
          lease: lease
        )
        return runtime
      } catch {
        ProcessInstalledApprovalHostRuntimeRegistry.shared.abandon(
          configuration: configuration,
          lease: lease
        )
        throw error
      }
    } catch let error as ApprovalHostError {
      throw error
    } catch {
      // Startup must not leak Security, XPC, or fake-adapter diagnostics into
      // an installed product launch path.
      throw ApprovalHostError.hostNotInstalled
    }
  }

  /// A separate scope ensures partially constructed components are released
  /// before the caller abandons its process-local construction lease.
  private static func buildRuntime(
    metadata: ApprovalHostBuildMetadataV1,
    configuration: TrustedHostConfiguration,
    lease: UUID,
    factory: any InstalledApprovalHostFactory
  ) throws -> InstalledApprovalHostRuntime {
    let rawStore = try factory.makeStore(configuration: configuration)
    let operationGate = InstalledApprovalHostOperationGate()
    let interactionGate = InstalledApprovalHostInteractionGate()
    let store = RuntimeBoundProtectedKeyStore(store: rawStore, gate: operationGate)
    let engine = try factory.makeEngine(configuration: configuration, store: store)
    let listener: any InstalledApprovalHostListener
    if let nativeBridgeFactory = factory as? any InstalledApprovalHostNativeBridgeFactory {
      listener = try nativeBridgeFactory.makeNativeBridgeListener(
        metadata: metadata,
        configuration: configuration,
        rawStore: rawStore,
        engine: engine,
        interactionGate: interactionGate,
        operationGate: operationGate
      )
    } else {
      listener = try factory.makeListener(
        configuration: configuration, engine: engine, interactionGate: interactionGate
      )
    }
    return InstalledApprovalHostRuntime(
      configuration: configuration,
      store: store,
      engine: engine,
      listener: listener,
      operationGate: operationGate,
      interactionGate: interactionGate,
      bootstrapLease: lease
    )
  }
}
