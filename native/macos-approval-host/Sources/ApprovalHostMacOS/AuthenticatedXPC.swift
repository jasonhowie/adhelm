import ApprovalHostCore
@preconcurrency import Darwin
@preconcurrency import Foundation

/// The closed XPC surface has a raw-receipt operation and two distinct native
/// bridge operations. The raw-receipt route never reaches bridge redemption;
/// each bridge selector accepts only its own canonical request and returns
/// only its own fully revalidated post-consumption candidate or compact public
/// failure. There is no generic signing, key, enrollment, policy-file,
/// configuration, shell, or provider operation.
@objc protocol ApprovalHostXPCProtocol {
  func requestApproval(
    _ canonicalChallenge: Data,
    withReply reply: @escaping (Data?, String?, String?) -> Void
  )

  func requestNativeApprovalBridge(
    _ canonicalRequest: Data,
    withReply reply: @escaping (Data?, String?, String?) -> Void
  )

  func requestNativeMetaCampaignPauseApprovalBridge(
    _ canonicalRequest: Data,
    withReply reply: @escaping (Data?, String?, String?) -> Void
  )
}

public enum ApprovalHostXPCResponse: Sendable, Equatable {
  /// A bounded canonical receipt candidate.  It is not a durable grant or a
  /// verified authorization until a separately pinned verifier validates the
  /// full receipt, challenge, and signature.
  case receipt(Data)
  case failure(ApprovalPublicFailure)
}

/// A package-only result for the dedicated M32/M33 bridge. It does not expose
/// a raw receipt, authenticated session, key, permit, provider capability, or
/// Python authority token.
package enum NativeApprovalBridgeXPCResponse: Sendable, Equatable {
  case candidate(NativeApprovalBridgeCandidateV1)
  case failure(ApprovalPublicFailure)
}

/// An internal seam for offline tests.  Production uses the fixed Mach service
/// and host requirement from `TrustedHostConfiguration`; callers cannot
/// provide a transport, endpoint, or code requirement through the public API.
enum ApprovalXPCWireResponse: Equatable {
  case response(receipt: Data?, code: String?, message: String?)
  case transportUnavailable
}

enum NativeApprovalBridgeXPCWireResponse: Equatable {
  case response(candidate: Data?, code: String?, message: String?)
  case transportUnavailable
}

protocol ApprovalXPCTransport: AnyObject {
  func send(
    canonicalChallenge: Data,
    completion: @escaping (ApprovalXPCWireResponse) -> Void
  )

  func invalidate()
}

protocol NativeApprovalBridgeXPCTransport: AnyObject {
  func send(
    canonicalRequest: Data,
    completion: @escaping (NativeApprovalBridgeXPCWireResponse) -> Void
  )

  func invalidate()
}

/// A configured client pins the expected host identity before its connection
/// becomes active.  The configuration is a reviewed installed-build constant;
/// no request supplies a service name or code-signing requirement.
public final class AuthenticatedApprovalXPCClient {
  public static let maximumReceiptBytes = 16_384

  private struct PendingRequest {
    let generation: UInt64
    let transport: any ApprovalXPCTransport
    let completion: (ApprovalHostXPCResponse) -> Void
    var deadline: (any ApprovalCancellation)?
  }

  private static let receiptLimits = CanonicalJSONLimits(
    maximumBytes: maximumReceiptBytes,
    maximumDepth: 20,
    maximumContainerItems: 256,
    maximumStringBytes: 4_096
  )

  private let configuration: TrustedHostConfiguration
  private let transportFactory: () -> any ApprovalXPCTransport
  private let scheduler: any ApprovalDeadlineScheduler
  private let clock: () -> Date
  private let lock = NSLock()
  private var pending: PendingRequest?
  private var nextGeneration: UInt64 = 0

  public init(configuration: TrustedHostConfiguration) {
    self.configuration = configuration
    transportFactory = { NSXPCApprovalTransport(configuration: configuration) }
    scheduler = DispatchApprovalDeadlineScheduler()
    clock = Date.init
  }

  /// This initializer is deliberately internal.  It lets XCTest exercise
  /// timeout, malformed-response, and cancellation behavior without a Mach
  /// service, AppKit, Keychain, LocalAuthentication, or provider call.
  init(
    configuration: TrustedHostConfiguration,
    transportFactory: @escaping () -> any ApprovalXPCTransport,
    scheduler: any ApprovalDeadlineScheduler,
    clock: @escaping () -> Date
  ) {
    self.configuration = configuration
    self.transportFactory = transportFactory
    self.scheduler = scheduler
    self.clock = clock
  }

  public func requestApproval(
    canonicalChallenge: Data,
    completion: @escaping (ApprovalHostXPCResponse) -> Void
  ) {
    let now = clock()
    let challenge: ApprovalChallenge
    do {
      challenge = try ApprovalChallenge.decode(
        canonicalChallenge,
        now: now,
        limits: configuration.limits
      )
      guard Self.hasExpectedHostBinding(challenge, configuration: configuration) else {
        completion(.failure(ApprovalPublicFailure(.malformedChallenge)))
        return
      }
    } catch let error as ApprovalHostError {
      completion(.failure(ApprovalPublicFailure(error)))
      return
    } catch {
      completion(.failure(ApprovalPublicFailure(.malformedChallenge)))
      return
    }

    let interval = challenge.expiresAt.timeIntervalSince(now)
    guard interval.isFinite, interval > 0 else {
      completion(.failure(ApprovalPublicFailure(.challengeExpired)))
      return
    }

    let transport = transportFactory()
    lock.lock()
    guard pending == nil else {
      lock.unlock()
      completion(.failure(ApprovalPublicFailure(.requestAlreadyActive)))
      return
    }
    nextGeneration &+= 1
    let generation = nextGeneration
    pending = PendingRequest(
      generation: generation,
      transport: transport,
      completion: completion,
      deadline: nil
    )
    lock.unlock()

    // The transport is request-bound.  Its own invalidation guard means a
    // concurrent local cancellation before this call cannot start an XPC
    // request after the client has already completed fail-closed.
    transport.send(canonicalChallenge: canonicalChallenge) { [weak self] response in
      self?.received(response, generation: generation)
    }
    let deadline = scheduler.schedule(after: interval) { [weak self] in
      self?.finish(
        generation: generation,
        response: .failure(ApprovalPublicFailure(.timedOut))
      )
    }
    installDeadline(deadline, generation: generation)
  }

  public func invalidate() {
    lock.lock()
    let generation = pending?.generation
    lock.unlock()
    if let generation {
      finish(
        generation: generation,
        response: .failure(ApprovalPublicFailure(.transportUnavailable))
      )
    }
  }

  private func received(_ response: ApprovalXPCWireResponse, generation: UInt64) {
    finish(generation: generation, response: Self.validateWireResponse(response))
  }

  private func installDeadline(_ deadline: any ApprovalCancellation, generation: UInt64) {
    lock.lock()
    guard var current = pending, current.generation == generation else {
      lock.unlock()
      deadline.cancel()
      return
    }
    current.deadline = deadline
    pending = current
    lock.unlock()
  }

  private func finish(generation: UInt64, response: ApprovalHostXPCResponse) {
    lock.lock()
    guard let current = pending, current.generation == generation else {
      lock.unlock()
      return
    }
    pending = nil
    lock.unlock()
    current.deadline?.cancel()
    current.transport.invalidate()
    current.completion(response)
  }

  private static func hasExpectedHostBinding(
    _ challenge: ApprovalChallenge,
    configuration: TrustedHostConfiguration
  ) -> Bool {
    switch challenge.subject {
    case .openAIPausePlan:
      true
    case .ga4KeyEventCreatePlan:
      true
    case .metaCampaignPausePlan:
      // M42 is a Core-only review/receipt subject. The raw XPC receipt route
      // and the separately allowlisted bridge cannot admit Meta plans.
      false
    case .delegationPolicyInstall(let policy), .delegationPolicyRevoke(let policy, _, _):
      policy.configurationHostRef == configuration.hostReference
    }
  }

  private static func validateWireResponse(_ response: ApprovalXPCWireResponse)
    -> ApprovalHostXPCResponse
  {
    switch response {
    case .response(receipt: let receipt?, code: nil, message: nil):
      guard receipt.count <= maximumReceiptBytes,
        !receipt.isEmpty
      else { return .failure(ApprovalPublicFailure(.transportUnavailable)) }
      do {
        let value = try CanonicalJSON.parseCanonical(receipt, limits: receiptLimits)
        guard case .object = value else {
          return .failure(ApprovalPublicFailure(.transportUnavailable))
        }
        return .receipt(receipt)
      } catch {
        return .failure(ApprovalPublicFailure(.transportUnavailable))
      }
    case .response(receipt: nil, code: let code?, message: let message?):
      guard let error = ApprovalHostError.exactPublicFailure(code: code, message: message) else {
        return .failure(ApprovalPublicFailure(.transportUnavailable))
      }
      return .failure(ApprovalPublicFailure(error))
    case .response, .transportUnavailable:
      return .failure(ApprovalPublicFailure(.transportUnavailable))
    }
  }
}

/// The package-only client for the dedicated native bridge operation. Unlike
/// the public raw-receipt client, it sends only a closed M32 request and
/// accepts only a candidate that revalidates against that exact request,
/// compiled metadata, and local exchange clock. No initializer accepts raw
/// bytes, a service name, a requirement, a key, or a generic transport.
package final class AuthenticatedNativeApprovalBridgeClient {
  private struct PendingRequest {
    let generation: UInt64
    let request: NativeApprovalBridgeRequestV1
    let exchangeStartedAt: Date
    let transport: any NativeApprovalBridgeXPCTransport
    let completion: (NativeApprovalBridgeXPCResponse) -> Void
    var deadline: (any ApprovalCancellation)?
  }

  private let metadata: ApprovalHostBuildMetadataV1
  private let configuration: TrustedHostConfiguration?
  private let transportFactory: () -> any NativeApprovalBridgeXPCTransport
  private let scheduler: any ApprovalDeadlineScheduler
  private let clock: () -> Date
  private let lock = NSLock()
  private var pending: PendingRequest?
  private var nextGeneration: UInt64 = 0

  package init(metadata: ApprovalHostBuildMetadataV1) {
    self.metadata = metadata
    if let configuration = try? metadata.makeTrustedHostConfiguration() {
      self.configuration = configuration
      transportFactory = { NativeApprovalBridgeNSXPCTransport(configuration: configuration) }
    } else {
      configuration = nil
      transportFactory = { UnavailableNativeApprovalBridgeTransport() }
    }
    scheduler = DispatchApprovalDeadlineScheduler()
    clock = Date.init
  }

  /// Internal XCTest seam. Product callers receive the fixed constructor
  /// above; test code still supplies only a typed request to `request`.
  init(
    metadata: ApprovalHostBuildMetadataV1,
    transportFactory: @escaping () -> any NativeApprovalBridgeXPCTransport,
    scheduler: any ApprovalDeadlineScheduler,
    clock: @escaping () -> Date
  ) {
    self.metadata = metadata
    configuration = try? metadata.makeTrustedHostConfiguration()
    self.transportFactory = transportFactory
    self.scheduler = scheduler
    self.clock = clock
  }

  package func request(
    _ request: NativeApprovalBridgeRequestV1,
    completion: @escaping (NativeApprovalBridgeXPCResponse) -> Void
  ) {
    guard configuration != nil else {
      completion(.failure(ApprovalPublicFailure(.hostNotInstalled)))
      return
    }
    let exchangeStartedAt = clock()
    let challenge: ApprovalChallenge
    do {
      challenge = try request.boundChallenge(
        metadata: metadata,
        exchangeStartedAt: exchangeStartedAt
      )
    } catch {
      completion(.failure(ApprovalPublicFailure(.malformedChallenge)))
      return
    }
    let interval = challenge.expiresAt.timeIntervalSince(exchangeStartedAt)
    guard interval.isFinite, interval > 0 else {
      completion(.failure(ApprovalPublicFailure(.challengeExpired)))
      return
    }

    let transport = transportFactory()
    lock.lock()
    guard pending == nil else {
      lock.unlock()
      completion(.failure(ApprovalPublicFailure(.requestAlreadyActive)))
      return
    }
    nextGeneration &+= 1
    let generation = nextGeneration
    pending = PendingRequest(
      generation: generation,
      request: request,
      exchangeStartedAt: exchangeStartedAt,
      transport: transport,
      completion: completion,
      deadline: nil
    )
    lock.unlock()

    transport.send(canonicalRequest: request.canonicalBytes) { [weak self] response in
      self?.received(response, generation: generation)
    }
    let deadline = scheduler.schedule(after: interval) { [weak self] in
      self?.finish(
        generation: generation,
        response: .failure(ApprovalPublicFailure(.timedOut))
      )
    }
    installDeadline(deadline, generation: generation)
  }

  package func invalidate() {
    lock.lock()
    let generation = pending?.generation
    lock.unlock()
    if let generation {
      finish(
        generation: generation,
        response: .failure(ApprovalPublicFailure(.transportUnavailable))
      )
    }
  }

  private func received(_ response: NativeApprovalBridgeXPCWireResponse, generation: UInt64) {
    lock.lock()
    guard let current = pending, current.generation == generation else {
      lock.unlock()
      return
    }
    lock.unlock()
    finish(
      generation: generation,
      response: validateWireResponse(response, pending: current)
    )
  }

  private func installDeadline(_ deadline: any ApprovalCancellation, generation: UInt64) {
    lock.lock()
    guard var current = pending, current.generation == generation else {
      lock.unlock()
      deadline.cancel()
      return
    }
    current.deadline = deadline
    pending = current
    lock.unlock()
  }

  private func finish(generation: UInt64, response: NativeApprovalBridgeXPCResponse) {
    lock.lock()
    guard let current = pending, current.generation == generation else {
      lock.unlock()
      return
    }
    pending = nil
    lock.unlock()
    current.deadline?.cancel()
    current.transport.invalidate()
    current.completion(response)
  }

  private func validateWireResponse(
    _ response: NativeApprovalBridgeXPCWireResponse,
    pending: PendingRequest
  ) -> NativeApprovalBridgeXPCResponse {
    switch response {
    case .response(candidate: let bytes?, code: nil, message: nil):
      guard !bytes.isEmpty,
        bytes.count <= NativeApprovalBridgeCandidateV1.maximumWireBytes
      else { return .failure(ApprovalPublicFailure(.transportUnavailable)) }
      do {
        let candidate = try NativeApprovalBridgeCandidateV1.decode(
          bytes,
          expectedRequest: pending.request,
          metadata: metadata,
          exchangeStartedAt: pending.exchangeStartedAt,
          currentNow: clock()
        )
        return .candidate(candidate)
      } catch {
        return .failure(ApprovalPublicFailure(.transportUnavailable))
      }
    case .response(candidate: nil, code: let code?, message: let message?):
      guard let error = ApprovalHostError.exactPublicFailure(code: code, message: message) else {
        return .failure(ApprovalPublicFailure(.transportUnavailable))
      }
      return .failure(ApprovalPublicFailure(error))
    case .response, .transportUnavailable:
      return .failure(ApprovalPublicFailure(.transportUnavailable))
    }
  }
}

/// The only production transport.  Each request owns its connection and pins
/// the configured host requirement before activation.  A local invalidation
/// races safely with `send`: if it wins, no XPC proxy is contacted.
private final class NSXPCApprovalTransport: ApprovalXPCTransport {
  private let configuration: TrustedHostConfiguration
  private let lock = NSLock()
  private var connection: NSXPCConnection?
  private var invalidated = false

  init(configuration: TrustedHostConfiguration) {
    self.configuration = configuration
  }

  func send(
    canonicalChallenge: Data,
    completion: @escaping (ApprovalXPCWireResponse) -> Void
  ) {
    let connection: NSXPCConnection
    lock.lock()
    guard !invalidated, self.connection == nil else {
      lock.unlock()
      completion(.transportUnavailable)
      return
    }
    connection = NSXPCConnection(
      machServiceName: configuration.machServiceName,
      options: []
    )
    self.connection = connection
    lock.unlock()

    connection.remoteObjectInterface = NSXPCInterface(with: ApprovalHostXPCProtocol.self)
    // The macOS 13 SDK requires this before activation and raises an
    // Objective-C exception for malformed trusted build requirements.
    connection.setCodeSigningRequirement(configuration.expectedHostCodeSigningRequirement)
    let reply = XPCWireReplyOnce(completion)
    connection.interruptionHandler = {
      reply.call(.transportUnavailable)
    }
    connection.invalidationHandler = {
      reply.call(.transportUnavailable)
    }
    guard isActive(connection) else {
      connection.invalidate()
      reply.call(.transportUnavailable)
      return
    }
    connection.activate()
    guard isActive(connection) else {
      connection.invalidate()
      reply.call(.transportUnavailable)
      return
    }
    let proxy =
      connection.remoteObjectProxyWithErrorHandler { _ in
        reply.call(.transportUnavailable)
      } as? ApprovalHostXPCProtocol
    guard let proxy else {
      reply.call(.transportUnavailable)
      return
    }
    guard isActive(connection) else {
      connection.invalidate()
      reply.call(.transportUnavailable)
      return
    }
    proxy.requestApproval(canonicalChallenge) { receipt, code, message in
      reply.call(.response(receipt: receipt, code: code, message: message))
    }
  }

  func invalidate() {
    lock.lock()
    invalidated = true
    let connection = self.connection
    self.connection = nil
    lock.unlock()
    connection?.invalidate()
  }

  private func isActive(_ candidate: NSXPCConnection) -> Bool {
    lock.lock()
    defer { lock.unlock() }
    return !invalidated && connection === candidate
  }
}

/// The distinct transport for the bridge endpoint. It uses the same fixed Mach
/// service and compiled host requirement as the raw client, but it invokes a
/// different Objective-C selector and cannot submit a raw receipt.
private final class NativeApprovalBridgeNSXPCTransport: NativeApprovalBridgeXPCTransport {
  private let configuration: TrustedHostConfiguration
  private let lock = NSLock()
  private var connection: NSXPCConnection?
  private var invalidated = false

  init(configuration: TrustedHostConfiguration) {
    self.configuration = configuration
  }

  func send(
    canonicalRequest: Data,
    completion: @escaping (NativeApprovalBridgeXPCWireResponse) -> Void
  ) {
    let connection: NSXPCConnection
    lock.lock()
    guard !invalidated, self.connection == nil else {
      lock.unlock()
      completion(.transportUnavailable)
      return
    }
    connection = NSXPCConnection(machServiceName: configuration.machServiceName, options: [])
    self.connection = connection
    lock.unlock()

    connection.remoteObjectInterface = NSXPCInterface(with: ApprovalHostXPCProtocol.self)
    connection.setCodeSigningRequirement(configuration.expectedHostCodeSigningRequirement)
    let reply = NativeApprovalBridgeXPCWireReplyOnce(completion)
    connection.interruptionHandler = { reply.call(.transportUnavailable) }
    connection.invalidationHandler = { reply.call(.transportUnavailable) }
    guard isActive(connection) else {
      connection.invalidate()
      reply.call(.transportUnavailable)
      return
    }
    connection.activate()
    guard isActive(connection) else {
      connection.invalidate()
      reply.call(.transportUnavailable)
      return
    }
    let proxy =
      connection.remoteObjectProxyWithErrorHandler { _ in
        reply.call(.transportUnavailable)
      } as? ApprovalHostXPCProtocol
    guard let proxy, isActive(connection) else {
      connection.invalidate()
      reply.call(.transportUnavailable)
      return
    }
    proxy.requestNativeApprovalBridge(canonicalRequest) { candidate, code, message in
      reply.call(.response(candidate: candidate, code: code, message: message))
    }
  }

  func invalidate() {
    lock.lock()
    invalidated = true
    let connection = self.connection
    self.connection = nil
    lock.unlock()
    connection?.invalidate()
  }

  private func isActive(_ candidate: NSXPCConnection) -> Bool {
    lock.lock()
    defer { lock.unlock() }
    return !invalidated && connection === candidate
  }
}

private final class UnavailableNativeApprovalBridgeTransport: NativeApprovalBridgeXPCTransport {
  func send(
    canonicalRequest _: Data,
    completion: @escaping (NativeApprovalBridgeXPCWireResponse) -> Void
  ) {
    completion(.transportUnavailable)
  }

  func invalidate() {}
}

/// A Mach listener for an installed user-level application.  The listener
/// automatically rejects code-signing-mismatched peers before this delegate,
/// while this additional UID check rejects a cross-user client.  Neither check
/// establishes that a human approved a request; that happens only in the
/// subsequent AppKit and LocalAuthentication path.
/// Installed listener construction is module-internal.  A caller must enter
/// through `ApprovalHostInstallation` so fixed build metadata, both code
/// requirements, and the current host identity are checked before this can
/// allocate an XPC listener.
final class AuthenticatedApprovalXPCListener: NSObject, NSXPCListenerDelegate {
  private let configuration: TrustedHostConfiguration
  private let engine: ApprovalHostEngine
  private let interactionGate: InstalledApprovalHostInteractionGate
  private let nativeBridgeConsumerFactory: (any NativeApprovalBridgePostSigningConsumerProviding)?
  private let nativeMetaCampaignPauseBridgeConsumerFactory:
    (any NativeMetaCampaignPauseApprovalBridgePostSigningConsumerProviding)?
  private let listener: NSXPCListener
  private let connections = AuthenticatedApprovalXPCConnectionRegistry()

  init(
    configuration: TrustedHostConfiguration, engine: ApprovalHostEngine,
    interactionGate: InstalledApprovalHostInteractionGate,
    nativeBridgeConsumerFactory: (any NativeApprovalBridgePostSigningConsumerProviding)? = nil,
    nativeMetaCampaignPauseBridgeConsumerFactory:
      (any NativeMetaCampaignPauseApprovalBridgePostSigningConsumerProviding)? = nil
  ) {
    self.configuration = configuration
    self.engine = engine
    self.interactionGate = interactionGate
    self.nativeBridgeConsumerFactory = nativeBridgeConsumerFactory
    self.nativeMetaCampaignPauseBridgeConsumerFactory = nativeMetaCampaignPauseBridgeConsumerFactory
    listener = NSXPCListener(machServiceName: configuration.machServiceName)
    super.init()
    // Must happen before activation.  The trusted requirement is never
    // parsed from a client message or challenge.
    listener.setConnectionCodeSigningRequirement(configuration.expectedClientCodeSigningRequirement)
    listener.delegate = self
  }

  func activate() {
    guard connections.canActivateListener else { return }
    listener.activate()
  }

  func invalidate() {
    guard connections.invalidate() else { return }
    listener.invalidate()
  }

  public func listener(
    _: NSXPCListener,
    shouldAcceptNewConnection newConnection: NSXPCConnection
  ) -> Bool {
    guard connections.canAdmit,
      newConnection.effectiveUserIdentifier == geteuid()
    else {
      newConnection.invalidate()
      return false
    }
    let service = ApprovalHostXPCService(
      engine: engine,
      interactionGate: interactionGate,
      nativeBridgeConsumerFactory: nativeBridgeConsumerFactory,
      nativeMetaCampaignPauseBridgeConsumerFactory: nativeMetaCampaignPauseBridgeConsumerFactory,
      nativeMetaCampaignPauseProtocolConfigurationDigest:
        configuration.approvalProtocolConfigurationV1.configurationDigest
    )
    newConnection.exportedInterface = NSXPCInterface(with: ApprovalHostXPCProtocol.self)
    newConnection.exportedObject = service
    guard
      let identifier = connections.admit(
        service: service,
        connection: newConnection,
        cancelService: { [weak service] in service?.cancelPending() },
        invalidateConnection: { [weak newConnection] in newConnection?.invalidate() }
      )
    else {
      newConnection.invalidate()
      return false
    }
    newConnection.invalidationHandler = { [weak self, weak service] in
      service?.cancelPending()
      self?.connections.remove(identifier)
    }
    guard connections.canActivate(identifier) else {
      newConnection.invalidate()
      return false
    }
    newConnection.activate()
    return true
  }
}

final class ApprovalHostXPCService: NSObject, ApprovalHostXPCProtocol {
  private struct PendingRequest {
    let generation: UInt64
    var handle: (any ApprovalRequestHandle)?
    var cancelled: Bool
  }

  private let engine: ApprovalHostEngine
  private let interactionGate: InstalledApprovalHostInteractionGate
  private let nativeBridgeConsumerFactory: (any NativeApprovalBridgePostSigningConsumerProviding)?
  private let nativeMetaCampaignPauseBridgeConsumerFactory:
    (any NativeMetaCampaignPauseApprovalBridgePostSigningConsumerProviding)?
  /// The listener supplies this only from its already-held reviewed host
  /// configuration. A direct XCTest service without it keeps the V2 selector
  /// closed rather than treating factory metadata as the engine configuration.
  private let nativeMetaCampaignPauseProtocolConfigurationDigest: String?
  private let clock: () -> Date
  private let lock = NSLock()
  private var nextGeneration: UInt64 = 0
  private var pending: PendingRequest?
  private var invalidated = false

  init(
    engine: ApprovalHostEngine,
    interactionGate: InstalledApprovalHostInteractionGate,
    nativeBridgeConsumerFactory: (any NativeApprovalBridgePostSigningConsumerProviding)? = nil,
    nativeMetaCampaignPauseBridgeConsumerFactory:
      (any NativeMetaCampaignPauseApprovalBridgePostSigningConsumerProviding)? = nil,
    nativeMetaCampaignPauseProtocolConfigurationDigest: String? = nil,
    clock: @escaping () -> Date = Date.init
  ) {
    self.engine = engine
    self.interactionGate = interactionGate
    self.nativeBridgeConsumerFactory = nativeBridgeConsumerFactory
    self.nativeMetaCampaignPauseBridgeConsumerFactory = nativeMetaCampaignPauseBridgeConsumerFactory
    self.nativeMetaCampaignPauseProtocolConfigurationDigest =
      nativeMetaCampaignPauseProtocolConfigurationDigest
    self.clock = clock
  }

  func requestApproval(
    _ canonicalChallenge: Data,
    withReply reply: @escaping (Data?, String?, String?) -> Void
  ) {
    let response = XPCServiceReply(reply)
    guard !engine.rawReceiptXPCRejectsMetaSubject(challengeData: canonicalChallenge) else {
      response.failure(.malformedChallenge)
      return
    }
    lock.lock()
    guard !invalidated else {
      lock.unlock()
      response.failure(.hostNotInstalled)
      return
    }
    guard pending == nil else {
      lock.unlock()
      response.failure(.requestAlreadyActive)
      return
    }
    let lease: InstalledApprovalHostInteractionLease
    do {
      lease = try interactionGate.acquire()
    } catch let error as ApprovalHostError {
      lock.unlock()
      response.failure(error)
      return
    } catch {
      lock.unlock()
      response.failure(.hostNotInstalled)
      return
    }
    nextGeneration &+= 1
    let generation = nextGeneration
    pending = PendingRequest(generation: generation, handle: nil, cancelled: false)
    lock.unlock()

    let handle = engine.requestApproval(
      challengeData: canonicalChallenge,
      completion: { [weak self] result in
        self?.complete(generation: generation, result: result, response: response)
      },
      interactionFinished: { lease.finish() }
    )
    lock.lock()
    let shouldCancel: Bool
    if var current = pending, current.generation == generation {
      current.handle = handle
      shouldCancel = current.cancelled
      pending = current
    } else {
      shouldCancel = true
    }
    lock.unlock()
    if shouldCancel { handle.cancel() }
  }

  /// This selector is deliberately separate from both the raw receipt route
  /// and M32/M33 bridge selector. It admits an exact Meta V2 request against
  /// held factory metadata before it acquires an interaction lease, presents
  /// review UI, authenticates, signs, or reaches a protected-store handoff.
  func requestNativeMetaCampaignPauseApprovalBridge(
    _ canonicalRequest: Data,
    withReply reply: @escaping (Data?, String?, String?) -> Void
  ) {
    let response = XPCServiceReply(reply)
    guard let nativeMetaCampaignPauseBridgeConsumerFactory,
      let nativeMetaCampaignPauseProtocolConfigurationDigest
    else {
      response.failure(.hostNotInstalled)
      return
    }
    let exchangeStartedAt = clock()
    let request: NativeMetaCampaignPauseApprovalBridgeRequestV2
    let consumer: any NativeMetaCampaignPauseApprovalBridgePostSigningConsumer
    do {
      request = try NativeMetaCampaignPauseApprovalBridgeRequestV2.decode(canonicalRequest)
      _ = try request.boundChallenge(
        metadata: nativeMetaCampaignPauseBridgeConsumerFactory.metadata,
        exchangeStartedAt: exchangeStartedAt
      )
      guard
        request.protocolConfigurationDigest
          == nativeMetaCampaignPauseProtocolConfigurationDigest
      else { throw NativeApprovalBridgeProtocolError.bindingMismatch }
      consumer = try nativeMetaCampaignPauseBridgeConsumerFactory.makeConsumer(
        request: request,
        exchangeStartedAt: exchangeStartedAt
      )
    } catch {
      response.failure(.malformedChallenge)
      return
    }

    lock.lock()
    guard !invalidated else {
      lock.unlock()
      response.failure(.hostNotInstalled)
      return
    }
    guard pending == nil else {
      lock.unlock()
      response.failure(.requestAlreadyActive)
      return
    }
    let lease: InstalledApprovalHostInteractionLease
    do {
      lease = try interactionGate.acquire()
    } catch let error as ApprovalHostError {
      lock.unlock()
      response.failure(error)
      return
    } catch {
      lock.unlock()
      response.failure(.hostNotInstalled)
      return
    }
    nextGeneration &+= 1
    let generation = nextGeneration
    pending = PendingRequest(generation: generation, handle: nil, cancelled: false)
    lock.unlock()

    let handle = engine.requestNativeMetaCampaignPauseApprovalBridge(
      request: request,
      postSigningConsumer: consumer,
      completion: { [weak self] result in
        self?.completeNativeMetaCampaignPauseBridge(
          generation: generation,
          result: result,
          response: response
        )
      },
      interactionFinished: { lease.finish() }
    )
    lock.lock()
    let shouldCancel: Bool
    if var current = pending, current.generation == generation {
      current.handle = handle
      shouldCancel = current.cancelled
      pending = current
    } else {
      shouldCancel = true
    }
    lock.unlock()
    if shouldCancel { handle.cancel() }
  }

  /// The bridge endpoint is deliberately separate from `requestApproval`.
  /// It admits an exact M32 request before any interaction lease, review UI,
  /// authentication, signing, or protected-store operation. The raw receipt
  /// endpoint never supplies a post-signing consumer and therefore cannot
  /// enter native redemption.
  func requestNativeApprovalBridge(
    _ canonicalRequest: Data,
    withReply reply: @escaping (Data?, String?, String?) -> Void
  ) {
    let response = XPCServiceReply(reply)
    guard let nativeBridgeConsumerFactory else {
      response.failure(.hostNotInstalled)
      return
    }
    let exchangeStartedAt = clock()
    let request: NativeApprovalBridgeRequestV1
    let consumer: any ApprovalPostSigningConsumer
    do {
      request = try NativeApprovalBridgeRequestV1.decode(canonicalRequest)
      _ = try request.boundChallenge(
        metadata: nativeBridgeConsumerFactory.metadata,
        exchangeStartedAt: exchangeStartedAt
      )
      consumer = try nativeBridgeConsumerFactory.makeConsumer(
        request: request,
        exchangeStartedAt: exchangeStartedAt
      )
    } catch {
      response.failure(.malformedChallenge)
      return
    }

    lock.lock()
    guard !invalidated else {
      lock.unlock()
      response.failure(.hostNotInstalled)
      return
    }
    guard pending == nil else {
      lock.unlock()
      response.failure(.requestAlreadyActive)
      return
    }
    let lease: InstalledApprovalHostInteractionLease
    do {
      lease = try interactionGate.acquire()
    } catch let error as ApprovalHostError {
      lock.unlock()
      response.failure(error)
      return
    } catch {
      lock.unlock()
      response.failure(.hostNotInstalled)
      return
    }
    nextGeneration &+= 1
    let generation = nextGeneration
    pending = PendingRequest(generation: generation, handle: nil, cancelled: false)
    lock.unlock()

    let handle = engine.requestNativeApprovalBridge(
      challengeData: request.canonicalChallenge,
      postSigningConsumer: consumer,
      completion: { [weak self] result in
        self?.completeNativeBridge(generation: generation, result: result, response: response)
      },
      interactionFinished: { lease.finish() }
    )
    lock.lock()
    let shouldCancel: Bool
    if var current = pending, current.generation == generation {
      current.handle = handle
      shouldCancel = current.cancelled
      pending = current
    } else {
      shouldCancel = true
    }
    lock.unlock()
    if shouldCancel { handle.cancel() }
  }

  /// Called when the owning connection closes; this service is terminal.
  func cancelPending() {
    lock.lock()
    invalidated = true
    guard var current = pending else {
      lock.unlock()
      return
    }
    current.cancelled = true
    let handle = current.handle
    pending = current
    lock.unlock()
    handle?.cancel()
  }

  deinit { cancelPending() }

  private func complete(
    generation: UInt64,
    result: Result<SignedApprovalReceipt, ApprovalHostError>,
    response: XPCServiceReply
  ) {
    lock.lock()
    guard pending?.generation == generation else {
      lock.unlock()
      return
    }
    pending = nil
    lock.unlock()
    switch result {
    case .success(let receipt): response.success(receipt.canonicalReceipt)
    case .failure(let error): response.failure(error)
    }
  }

  private func completeNativeBridge(
    generation: UInt64,
    result: Result<NativeApprovalBridgeCandidateV1, ApprovalHostError>,
    response: XPCServiceReply
  ) {
    lock.lock()
    guard pending?.generation == generation else {
      lock.unlock()
      return
    }
    pending = nil
    lock.unlock()
    switch result {
    case .success(let candidate): response.successNativeBridgeCandidate(candidate.canonicalBytes)
    case .failure(let error): response.failure(error)
    }
  }

  private func completeNativeMetaCampaignPauseBridge(
    generation: UInt64,
    result: Result<NativeMetaCampaignPauseApprovalBridgeCandidateV2, ApprovalHostError>,
    response: XPCServiceReply
  ) {
    lock.lock()
    guard pending?.generation == generation else {
      lock.unlock()
      return
    }
    pending = nil
    lock.unlock()
    switch result {
    case .success(let candidate):
      response.successNativeMetaCampaignPauseBridgeCandidate(candidate.canonicalBytes)
    case .failure(let error): response.failure(error)
    }
  }
}

private final class XPCWireReplyOnce: @unchecked Sendable {
  private let lock = NSLock()
  private var completed = false
  private let completion: (ApprovalXPCWireResponse) -> Void

  init(_ completion: @escaping (ApprovalXPCWireResponse) -> Void) {
    self.completion = completion
  }

  func call(_ response: ApprovalXPCWireResponse) {
    lock.lock()
    guard !completed else {
      lock.unlock()
      return
    }
    completed = true
    lock.unlock()
    completion(response)
  }
}

private final class NativeApprovalBridgeXPCWireReplyOnce: @unchecked Sendable {
  private let lock = NSLock()
  private var completed = false
  private let completion: (NativeApprovalBridgeXPCWireResponse) -> Void

  init(_ completion: @escaping (NativeApprovalBridgeXPCWireResponse) -> Void) {
    self.completion = completion
  }

  func call(_ response: NativeApprovalBridgeXPCWireResponse) {
    lock.lock()
    guard !completed else {
      lock.unlock()
      return
    }
    completed = true
    lock.unlock()
    completion(response)
  }
}

private final class XPCServiceReply: @unchecked Sendable {
  private let lock = NSLock()
  private var completed = false
  private let reply: (Data?, String?, String?) -> Void

  init(_ reply: @escaping (Data?, String?, String?) -> Void) {
    self.reply = reply
  }

  func success(_ receipt: Data) {
    guard !receipt.isEmpty,
      receipt.count <= AuthenticatedApprovalXPCClient.maximumReceiptBytes
    else {
      failure(.signingFailed)
      return
    }
    call(receipt, nil, nil)
  }

  func successNativeBridgeCandidate(_ candidate: Data) {
    guard !candidate.isEmpty,
      candidate.count <= NativeApprovalBridgeCandidateV1.maximumWireBytes
    else {
      failure(.enrollmentStateUnverified)
      return
    }
    call(candidate, nil, nil)
  }

  func successNativeMetaCampaignPauseBridgeCandidate(_ candidate: Data) {
    guard !candidate.isEmpty,
      candidate.count <= NativeMetaCampaignPauseApprovalBridgeCandidateV2.maximumWireBytes
    else {
      failure(.enrollmentStateUnverified)
      return
    }
    call(candidate, nil, nil)
  }

  func failure(_ error: ApprovalHostError) {
    let publicFailure = ApprovalPublicFailure(error)
    call(nil, publicFailure.code, publicFailure.message)
  }

  private func call(_ receipt: Data?, _ code: String?, _ message: String?) {
    lock.lock()
    guard !completed else {
      lock.unlock()
      return
    }
    completed = true
    lock.unlock()
    reply(receipt, code, message)
  }
}
