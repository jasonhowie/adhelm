import ApprovalHostCore
@preconcurrency import Foundation

/// A package-only result for the dedicated Meta campaign PAUSE V2 bridge
/// selector. It contains only a candidate bound to one exact V2 request or a
/// compact public failure. It has no separate raw-receipt or key-retrieval
/// operation, session or permit exposure, caller-supplied trust input,
/// provider capability, or transport-detail surface. The candidate itself is
/// private reusable approval material and is revalidated by its next bounded
/// consumer.
package enum NativeMetaCampaignPauseApprovalBridgeXPCResponse: Sendable, Equatable {
  case candidate(NativeMetaCampaignPauseApprovalBridgeCandidateV2)
  case failure(ApprovalPublicFailure)
}

/// Offline-only transport result for the distinct Meta V2 selector. Production
/// callers use the fixed Mach service and compiled host requirement; no typed
/// client initializer accepts a caller-selected endpoint or identity.
enum NativeMetaCampaignPauseApprovalBridgeXPCWireResponse: Equatable {
  case response(candidate: Data?, code: String?, message: String?)
  case transportUnavailable
}

protocol NativeMetaCampaignPauseApprovalBridgeXPCTransport: AnyObject {
  func send(
    canonicalRequest: Data,
    completion: @escaping (NativeMetaCampaignPauseApprovalBridgeXPCWireResponse) -> Void
  )

  func invalidate()
}

/// The package-only V2 client sends only a structurally rebuilt Meta request
/// and accepts only an exact V2 candidate revalidated against that request,
/// held metadata, and the local exchange clock. V1 and raw receipt
/// clients have separate types and selectors.
package final class AuthenticatedNativeMetaCampaignPauseApprovalBridgeClient {
  private struct PendingRequest {
    let generation: UInt64
    let request: NativeMetaCampaignPauseApprovalBridgeRequestV2
    let exchangeStartedAt: Date
    let transport: any NativeMetaCampaignPauseApprovalBridgeXPCTransport
    let completion: (NativeMetaCampaignPauseApprovalBridgeXPCResponse) -> Void
    var deadline: (any ApprovalCancellation)?
  }

  private let metadata: ApprovalHostBuildMetadataV1
  private let configuration: TrustedHostConfiguration?
  private let transportFactory: () -> any NativeMetaCampaignPauseApprovalBridgeXPCTransport
  private let scheduler: any ApprovalDeadlineScheduler
  private let clock: () -> Date
  private let lock = NSLock()
  private var pending: PendingRequest?
  private var nextGeneration: UInt64 = 0

  package init(metadata: ApprovalHostBuildMetadataV1) {
    self.metadata = metadata
    if let configuration = try? metadata.makeTrustedHostConfiguration() {
      self.configuration = configuration
      transportFactory = {
        NativeMetaCampaignPauseApprovalBridgeNSXPCTransport(configuration: configuration)
      }
    } else {
      configuration = nil
      transportFactory = { UnavailableNativeMetaCampaignPauseApprovalBridgeTransport() }
    }
    scheduler = DispatchApprovalDeadlineScheduler()
    clock = Date.init
  }

  /// Internal XCTest seam. Production callers receive the fixed initializer;
  /// test code still provides only a typed V2 request to `request`.
  init(
    metadata: ApprovalHostBuildMetadataV1,
    transportFactory: @escaping () -> any NativeMetaCampaignPauseApprovalBridgeXPCTransport,
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
    _ request: NativeMetaCampaignPauseApprovalBridgeRequestV2,
    completion: @escaping (NativeMetaCampaignPauseApprovalBridgeXPCResponse) -> Void
  ) {
    guard configuration != nil else {
      completion(.failure(ApprovalPublicFailure(.hostNotInstalled)))
      return
    }
    let exchangeStartedAt = clock()
    let checkedRequest: NativeMetaCampaignPauseApprovalBridgeRequestV2
    let challenge: ApprovalChallenge
    do {
      checkedRequest = try NativeMetaCampaignPauseApprovalBridgeRequestV2.decode(
        request.canonicalBytes
      )
      guard checkedRequest == request else {
        throw NativeApprovalBridgeProtocolError.malformedRequest
      }
      challenge = try checkedRequest.boundChallenge(
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
      request: checkedRequest,
      exchangeStartedAt: exchangeStartedAt,
      transport: transport,
      completion: completion,
      deadline: nil
    )
    lock.unlock()

    transport.send(canonicalRequest: checkedRequest.canonicalBytes) { [weak self] response in
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

  private func received(
    _ response: NativeMetaCampaignPauseApprovalBridgeXPCWireResponse,
    generation: UInt64
  ) {
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

  private func finish(
    generation: UInt64,
    response: NativeMetaCampaignPauseApprovalBridgeXPCResponse
  ) {
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
    _ response: NativeMetaCampaignPauseApprovalBridgeXPCWireResponse,
    pending: PendingRequest
  ) -> NativeMetaCampaignPauseApprovalBridgeXPCResponse {
    switch response {
    case .response(candidate: let bytes?, code: nil, message: nil):
      guard !bytes.isEmpty,
        bytes.count <= NativeMetaCampaignPauseApprovalBridgeCandidateV2.maximumWireBytes
      else { return .failure(ApprovalPublicFailure(.transportUnavailable)) }
      do {
        let candidate = try NativeMetaCampaignPauseApprovalBridgeCandidateV2.decode(
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

/// The only production transport for the V2 selector. It shares the reviewed
/// Mach service and host requirement with the fixed V1 client, but uses one
/// different Objective-C selector and can never send a V1 request.
private final class NativeMetaCampaignPauseApprovalBridgeNSXPCTransport:
  NativeMetaCampaignPauseApprovalBridgeXPCTransport
{
  private let configuration: TrustedHostConfiguration
  private let lock = NSLock()
  private var connection: NSXPCConnection?
  private var invalidated = false

  init(configuration: TrustedHostConfiguration) {
    self.configuration = configuration
  }

  func send(
    canonicalRequest: Data,
    completion: @escaping (NativeMetaCampaignPauseApprovalBridgeXPCWireResponse) -> Void
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
    let reply = NativeMetaCampaignPauseApprovalBridgeXPCWireReplyOnce(completion)
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
    proxy.requestNativeMetaCampaignPauseApprovalBridge(canonicalRequest) {
      candidate,
      code,
      message in
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

private final class UnavailableNativeMetaCampaignPauseApprovalBridgeTransport:
  NativeMetaCampaignPauseApprovalBridgeXPCTransport
{
  func send(
    canonicalRequest _: Data,
    completion: @escaping (NativeMetaCampaignPauseApprovalBridgeXPCWireResponse) -> Void
  ) {
    completion(.transportUnavailable)
  }

  func invalidate() {}
}

private final class NativeMetaCampaignPauseApprovalBridgeXPCWireReplyOnce: @unchecked Sendable {
  private let lock = NSLock()
  private var completed = false
  private let completion: (NativeMetaCampaignPauseApprovalBridgeXPCWireResponse) -> Void

  init(_ completion: @escaping (NativeMetaCampaignPauseApprovalBridgeXPCWireResponse) -> Void) {
    self.completion = completion
  }

  func call(_ response: NativeMetaCampaignPauseApprovalBridgeXPCWireResponse) {
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
