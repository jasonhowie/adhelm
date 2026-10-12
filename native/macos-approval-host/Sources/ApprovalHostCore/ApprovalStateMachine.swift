@preconcurrency import Dispatch
import Foundation

public protocol ApprovalCancellation: AnyObject {
    func cancel()
}

public protocol ApprovalRequestHandle: ApprovalCancellation {}

public final class NoopApprovalCancellation: ApprovalCancellation {
    public init() {}
    public func cancel() {}
}

/// A successful authenticator returns the same opaque session to the signer.
/// Platform adapters may keep a fresh LAContext inside it and must invalidate it
/// when the request ends.
public protocol AuthenticatedApprovalSession: AnyObject {
    func invalidate()
}

public enum ApprovalReviewDecision: Sendable {
    case continueToAuthentication
    case cancel
}

public protocol ApprovalReviewer: AnyObject {
    @discardableResult
    func present(
        review: DeterministicApprovalReview,
        completion: @escaping (ApprovalReviewDecision) -> Void
    ) -> any ApprovalCancellation
}

public enum ApprovalAuthenticationResult {
    case authenticated(any AuthenticatedApprovalSession)
    case cancelled
    case unavailable
    case failed
}

public protocol FreshAuthenticator: AnyObject {
    @discardableResult
    func authenticate(
        reason: String,
        completion: @escaping (ApprovalAuthenticationResult) -> Void
    ) -> any ApprovalCancellation
}

public protocol ApprovalDeadlineScheduler: AnyObject {
    @discardableResult
    func schedule(
        after interval: TimeInterval,
        action: @escaping () -> Void
    ) -> any ApprovalCancellation
}

/// The production scheduler is intentionally small and has no persistence.
/// The durable ledger/replay boundary belongs to the future installed verifier,
/// not this in-process UI session.
public final class DispatchApprovalDeadlineScheduler: ApprovalDeadlineScheduler {
    public init() {}

    public func schedule(
        after interval: TimeInterval,
        action: @escaping () -> Void
    ) -> any ApprovalCancellation {
        let item = DispatchWorkItem(block: action)
        DispatchQueue.main.asyncAfter(deadline: .now() + max(0, interval), execute: item)
        return DispatchWorkItemCancellation(item: item)
    }
}

private final class DispatchWorkItemCancellation: ApprovalCancellation {
    private let item: DispatchWorkItem
    init(item: DispatchWorkItem) { self.item = item }
    func cancel() { item.cancel() }
}

private enum ApprovalTerminalValue {
    case receipt(SignedApprovalReceipt)
    case nativeBridgeCandidate(NativeApprovalBridgeCandidateV1)
    case nativeMetaCampaignPauseBridgeCandidate(NativeMetaCampaignPauseApprovalBridgeCandidateV2)
}

/// A closed post-signing route remains explicit at the engine boundary. V1
/// cannot receive a Meta candidate, and the Meta V2 entry cannot receive a V1
/// candidate, even though both use the same callback-scoped session lease.
private enum ApprovalPostSigningRoute {
    case v1(any ApprovalPostSigningConsumer)
    case metaCampaignPauseV2(any NativeMetaCampaignPauseApprovalBridgePostSigningConsumer)
}

public final class ApprovalHostEngine {
    private struct ActiveRequest {
        let generation: UInt64
        let handle: any ApprovalRequestHandle
    }

    private let configuration: TrustedHostConfiguration
    private let reviewer: any ApprovalReviewer
    private let authenticator: any FreshAuthenticator
    private let signer: any ReceiptSigner
    private let identifierGenerator: any ReceiptIdentifierGenerator
    private let scheduler: any ApprovalDeadlineScheduler
    private let clock: () -> Date
    private let lock = NSLock()
    private var activeRequests: [String: ActiveRequest] = [:]
    private var nextRequestGeneration: UInt64 = 0
    // This is deliberately only process-lifetime replay protection.  An
    // installed kernel still needs durable receipt/nonce replay enforcement
    // before it records a direct grant or starts egress.
    private var consumedChallenges: [String: Date] = [:]

    public init(
        configuration: TrustedHostConfiguration,
        reviewer: any ApprovalReviewer,
        authenticator: any FreshAuthenticator,
        signer: any ReceiptSigner,
        identifierGenerator: any ReceiptIdentifierGenerator = SystemReceiptIdentifierGenerator(),
        scheduler: any ApprovalDeadlineScheduler = DispatchApprovalDeadlineScheduler(),
        clock: @escaping () -> Date = Date.init
    ) {
        self.configuration = configuration
        self.reviewer = reviewer
        self.authenticator = authenticator
        self.signer = signer
        self.identifierGenerator = identifierGenerator
        self.scheduler = scheduler
        self.clock = clock
    }

    /// Starts only a typed approval flow.  It has no API for signing arbitrary
    /// caller bytes, selecting a key, enrolling a key, or choosing an XPC peer.
    @discardableResult
    public func requestApproval(
        challengeData: Data,
        completion: @escaping (Result<SignedApprovalReceipt, ApprovalHostError>) -> Void
    ) -> any ApprovalRequestHandle {
        requestApproval(
            challengeData: challengeData, completion: completion, interactionFinished: {}
        )
    }

    /// The installed adapter holds its shared UI admission slot until all
    /// synchronous adapter handoffs have returned and terminal cancellation
    /// handles have been processed. Response delivery alone cannot prove that
    /// a racing present/authenticate call has returned its cancellation handle.
    @discardableResult
    package func requestApproval(
        challengeData: Data,
        completion: @escaping (Result<SignedApprovalReceipt, ApprovalHostError>) -> Void,
        interactionFinished: @escaping () -> Void
    ) -> any ApprovalRequestHandle {
        startRequest(
            challengeData: challengeData,
            postSigningRoute: nil,
            completion: { result in
                switch result {
                case let .success(.receipt(receipt)):
                    completion(.success(receipt))
                case .success(.nativeBridgeCandidate):
                    completion(.failure(.signingFailed))
                case .success(.nativeMetaCampaignPauseBridgeCandidate):
                    completion(.failure(.signingFailed))
                case let .failure(error):
                    completion(.failure(error))
                }
            },
            interactionFinished: interactionFinished
        )
    }

    /// Starts the closed post-signing bridge path. Only the dedicated native
    /// route supplies its consumer; the public receipt API above never calls
    /// this overload and cannot receive a native bridge candidate.
    @discardableResult
    package func requestNativeApprovalBridge(
        challengeData: Data,
        postSigningConsumer: any ApprovalPostSigningConsumer,
        completion: @escaping (Result<NativeApprovalBridgeCandidateV1, ApprovalHostError>) -> Void,
        interactionFinished: @escaping () -> Void
    ) -> any ApprovalRequestHandle {
        startRequest(
            challengeData: challengeData,
            postSigningRoute: .v1(postSigningConsumer),
            completion: { result in
                switch result {
                case let .success(.nativeBridgeCandidate(candidate)):
                    completion(.success(candidate))
                case .success(.receipt):
                    completion(.failure(.signingFailed))
                case .success(.nativeMetaCampaignPauseBridgeCandidate):
                    completion(.failure(.signingFailed))
                case let .failure(error):
                    completion(.failure(error))
                }
            },
            interactionFinished: interactionFinished
        )
    }

    /// Starts only the distinct package-only Meta campaign PAUSE V2 route.
    /// It re-decodes the request's canonical wire bytes before admitting the
    /// review/authentication flow, then accepts only the exact Meta subject.
    /// No XPC, helper, runtime, provider, or generic receipt route calls this
    /// method in this increment.
    @discardableResult
    package func requestNativeMetaCampaignPauseApprovalBridge(
        request: NativeMetaCampaignPauseApprovalBridgeRequestV2,
        postSigningConsumer: any NativeMetaCampaignPauseApprovalBridgePostSigningConsumer,
        completion: @escaping (Result<NativeMetaCampaignPauseApprovalBridgeCandidateV2, ApprovalHostError>) -> Void,
        interactionFinished: @escaping () -> Void
    ) -> any ApprovalRequestHandle {
        let checkedRequest: NativeMetaCampaignPauseApprovalBridgeRequestV2
        do {
            checkedRequest = try NativeMetaCampaignPauseApprovalBridgeRequestV2.decode(
                request.canonicalBytes
            )
            // The engine holds one reviewed host configuration already. Bind
            // the protocol projection before it can open a human review
            // window. Full build metadata remains the post-signing consumer's
            // responsibility: this package-only engine entry neither selects
            // nor attests an installed host or its metadata.
            guard checkedRequest == request,
                  checkedRequest.protocolConfigurationDigest
                    == configuration.approvalProtocolConfigurationV1.configurationDigest
            else {
                throw NativeApprovalBridgeProtocolError.malformedRequest
            }
        } catch {
            completion(.failure(.malformedChallenge))
            interactionFinished()
            return CompletedApprovalRequest()
        }
        return startRequest(
            challengeData: checkedRequest.canonicalChallenge,
            postSigningRoute: .metaCampaignPauseV2(postSigningConsumer),
            completion: { result in
                switch result {
                case let .success(.nativeMetaCampaignPauseBridgeCandidate(candidate)):
                    completion(.success(candidate))
                case .success(.receipt), .success(.nativeBridgeCandidate):
                    completion(.failure(.signingFailed))
                case let .failure(error):
                    completion(.failure(error))
                }
            },
            interactionFinished: interactionFinished
        )
    }

    /// The raw receipt XPC endpoint uses this bounded canonical header check
    /// before it takes the installed interaction lease. Meta PAUSE support is
    /// limited to local Core review and receipt syntax in M42; it cannot open
    /// an XPC review, authentication, signing, or bridge-consumption flow.
    /// Other malformed or expired input deliberately retains the endpoint's
    /// existing gate-first error precedence. This package-only predicate is
    /// not a general challenge validator or an installed authority decision.
    package func rawReceiptXPCRejectsMetaSubject(challengeData: Data) -> Bool {
        let base = CanonicalJSONLimits.approvalChallenge
        let limits = CanonicalJSONLimits(
            maximumBytes: configuration.limits.maximumChallengeBytes,
            maximumDepth: base.maximumDepth,
            maximumContainerItems: base.maximumContainerItems,
            maximumStringBytes: base.maximumStringBytes
        )
        guard case let .object(fields) = try? CanonicalJSON.parseCanonical(challengeData, limits: limits),
              case let .string(kind)? = fields["kind"],
              kind == ApprovalKind.metaCampaignPausePlan.rawValue
        else { return false }
        return true
    }

    private func startRequest(
        challengeData: Data,
        postSigningRoute: ApprovalPostSigningRoute?,
        completion: @escaping (Result<ApprovalTerminalValue, ApprovalHostError>) -> Void,
        interactionFinished: @escaping () -> Void
    ) -> any ApprovalRequestHandle {
        do {
            let challenge = try ApprovalChallenge.decode(
                challengeData,
                now: clock(),
                limits: configuration.limits
            )
            try validateHostBinding(challenge)
            if let postSigningRoute {
                switch postSigningRoute {
                case .v1:
                    guard Self.isDirectRetainedPlanKind(challenge.kind) else {
                        throw ApprovalHostError.malformedChallenge
                    }
                case .metaCampaignPauseV2:
                    guard challenge.kind == .metaCampaignPausePlan else {
                        throw ApprovalHostError.malformedChallenge
                    }
                }
            }
            let review = try DeterministicApprovalReview.make(for: challenge)
            lock.lock()
            // Decoding is not the authorization handoff.  Recheck under the
            // engine lock before registering an active request so a challenge
            // that expires between parsing and this point cannot open a
            // review/authentication window.
            let now = clock()
            pruneExpiredConsumedChallenges(now: now)
            guard now < challenge.expiresAt else {
                lock.unlock()
                completion(.failure(.challengeExpired))
                interactionFinished()
                return CompletedApprovalRequest()
            }
            // A single host process may have many authenticated XPC peers, but
            // it deliberately exposes only one human review/authentication/
            // signing window at a time.  Per-nonce deduplication alone would
            // let independent connections create unbounded concurrent prompts.
            if !activeRequests.isEmpty {
                lock.unlock()
                completion(.failure(.requestAlreadyActive))
                interactionFinished()
                return CompletedApprovalRequest()
            }
            if consumedChallenges[challenge.replayKey] != nil {
                lock.unlock()
                completion(.failure(.challengeAlreadyConsumed))
                interactionFinished()
                return CompletedApprovalRequest()
            }
            nextRequestGeneration &+= 1
            let generation = nextRequestGeneration
            let request = ApprovalRequest(
                challenge: challenge,
                review: review,
                configuration: configuration,
                reviewer: reviewer,
                authenticator: authenticator,
                signer: signer,
                identifierGenerator: identifierGenerator,
                scheduler: scheduler,
                clock: clock,
                postSigningRoute: postSigningRoute,
                completion: completion,
                consume: { [weak self] in self?.consume(challenge, generation: generation) ?? false },
                terminal: { [weak self] in
                    self?.removeActiveRequest(challenge.replayKey, generation: generation)
                    interactionFinished()
                }
            )
            activeRequests[challenge.replayKey] = ActiveRequest(generation: generation, handle: request)
            lock.unlock()
            request.start()
            return request
        } catch let error as ApprovalHostError {
            completion(.failure(error))
            interactionFinished()
            return CompletedApprovalRequest()
        } catch {
            completion(.failure(.malformedChallenge))
            interactionFinished()
            return CompletedApprovalRequest()
        }
    }

    private func removeActiveRequest(_ key: String, generation: UInt64) {
        lock.lock()
        if activeRequests[key]?.generation == generation {
            activeRequests.removeValue(forKey: key)
        }
        lock.unlock()
    }

    private func validateHostBinding(_ challenge: ApprovalChallenge) throws {
        switch challenge.subject {
        case .openAIPausePlan:
            return
        case .ga4KeyEventCreatePlan:
            return
        case .metaCampaignPausePlan:
            return
        case let .delegationPolicyInstall(policy), let .delegationPolicyRevoke(policy, _, _):
            guard policy.configurationHostRef == configuration.hostReference else {
                throw ApprovalHostError.malformedChallenge
            }
        }
    }

    private static func isDirectRetainedPlanKind(_ kind: ApprovalKind) -> Bool {
        switch kind {
        case .openAIPausePlan, .ga4KeyEventCreatePlan:
            true
        case .metaCampaignPausePlan, .delegationPolicyInstall, .delegationPolicyRevoke:
            false
        }
    }

    private func consume(_ challenge: ApprovalChallenge, generation: UInt64) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        // Capture one time while holding the engine lock.  The request-level
        // guard can observe a still-valid challenge just before this handoff;
        // a second clock read at the consume boundary must still reject an
        // expiry rather than consuming the nonce and permitting a protected
        // signing operation after its authorization window.
        let now = clock()
        pruneExpiredConsumedChallenges(now: now)
        guard now < challenge.expiresAt,
              activeRequests[challenge.replayKey]?.generation == generation,
              consumedChallenges[challenge.replayKey] == nil
        else { return false }
        // Retain through expiry even if receipt finalization subsequently
        // fails.  That loss is fail-closed and prevents a signing oracle.
        consumedChallenges[challenge.replayKey] = challenge.expiresAt
        return true
    }

    private func pruneExpiredConsumedChallenges(now: Date) {
        consumedChallenges = consumedChallenges.filter { _, expiresAt in expiresAt > now }
    }
}

private final class CompletedApprovalRequest: ApprovalRequestHandle {
    func cancel() {}
}

private final class ApprovalRequest: ApprovalRequestHandle {
    private enum Phase {
        case created
        case reviewing
        case authenticating
        case signing
        case postSigningConsumption
        case finished
    }

    private let challenge: ApprovalChallenge
    private let review: DeterministicApprovalReview
    private let configuration: TrustedHostConfiguration
    private let reviewer: any ApprovalReviewer
    private let authenticator: any FreshAuthenticator
    private let signer: any ReceiptSigner
    private let identifierGenerator: any ReceiptIdentifierGenerator
    private let scheduler: any ApprovalDeadlineScheduler
    private let clock: () -> Date
    private let postSigningRoute: ApprovalPostSigningRoute?
    private let completion: (Result<ApprovalTerminalValue, ApprovalHostError>) -> Void
    private let consume: () -> Bool
    private let terminal: () -> Void
    private let lock = NSLock()
    private var phase: Phase = .created
    private var currentCancellation: (any ApprovalCancellation)?
    private var deadlineCancellation: (any ApprovalCancellation)?
    private var authenticatedSession: (any AuthenticatedApprovalSession)?
    private var postSigningLease: ApprovalPostSigningSessionLease?
    private var postSigningHandoffActive = false
    private var deferredTerminalResult: Result<ApprovalTerminalValue, ApprovalHostError>?
    private var adapterHandoffs = 0
    private var terminalReady = false
    private var terminalDelivered = false

    init(
        challenge: ApprovalChallenge,
        review: DeterministicApprovalReview,
        configuration: TrustedHostConfiguration,
        reviewer: any ApprovalReviewer,
        authenticator: any FreshAuthenticator,
        signer: any ReceiptSigner,
        identifierGenerator: any ReceiptIdentifierGenerator,
        scheduler: any ApprovalDeadlineScheduler,
        clock: @escaping () -> Date,
        postSigningRoute: ApprovalPostSigningRoute?,
        completion: @escaping (Result<ApprovalTerminalValue, ApprovalHostError>) -> Void,
        consume: @escaping () -> Bool,
        terminal: @escaping () -> Void
    ) {
        self.challenge = challenge
        self.review = review
        self.configuration = configuration
        self.reviewer = reviewer
        self.authenticator = authenticator
        self.signer = signer
        self.identifierGenerator = identifierGenerator
        self.scheduler = scheduler
        self.clock = clock
        self.postSigningRoute = postSigningRoute
        self.completion = completion
        self.consume = consume
        self.terminal = terminal
    }

    func start() {
        lock.lock()
        guard phase == .created else { lock.unlock(); return }
        // Engine registration and request startup are separate handoffs.  Do
        // not rely on the asynchronously scheduled deadline to close the gap:
        // it could otherwise present a review after expiry.
        guard clock() < challenge.expiresAt else {
            lock.unlock()
            finish(.failure(.challengeExpired))
            return
        }
        phase = .reviewing
        lock.unlock()
        let deadline = scheduler.schedule(after: challenge.expiresAt.timeIntervalSince(clock())) { [weak self] in
            self?.finish(.failure(.timedOut))
        }
        installDeadline(deadline)
        presentReview()
    }

    func cancel() {
        lock.lock()
        let error: ApprovalHostError = phase == .reviewing ? .reviewCancelled : .authenticationCancelled
        lock.unlock()
        finish(.failure(error))
    }

    private func presentReview() {
        guard isCurrent(.reviewing) else { return }
        // The deadline callback is asynchronous.  Check again immediately
        // before handing facts to the reviewer so an already expired challenge
        // cannot open a fresh UI window merely because scheduling raced.
        guard stillWithinChallengeExpiry() else {
            finish(.failure(.challengeExpired))
            return
        }
        guard beginAdapterHandoff(expected: .reviewing) else { return }
        defer { endAdapterHandoff() }
        let cancellation = reviewer.present(review: review) { [weak self] decision in
            self?.reviewFinished(decision)
        }
        installCurrentCancellation(cancellation, expected: .reviewing)
    }

    private func reviewFinished(_ decision: ApprovalReviewDecision) {
        switch decision {
        case .cancel:
            finish(.failure(.reviewCancelled))
        case .continueToAuthentication:
            guard transition(from: .reviewing, to: .authenticating), stillWithinChallengeExpiry() else {
                if !stillWithinChallengeExpiry() { finish(.failure(.challengeExpired)) }
                return
            }
            beginAuthentication()
        }
    }

    private func beginAuthentication() {
        guard beginAdapterHandoff(expected: .authenticating) else { return }
        defer { endAdapterHandoff() }
        let cancellation = authenticator.authenticate(
            reason: "approve the exact reviewed local advertising operation"
        ) { [weak self] result in
            // `finish` removes the engine's strong request handle.  If the
            // caller also dropped its handle, a late LA callback can arrive
            // after this request has deallocated.  The fresh session is still
            // a protected resource and must be invalidated even though there
            // is no request left to advance or complete.
            guard let self else {
                Self.invalidateLateAuthenticatedSession(result)
                return
            }
            self.authenticationFinished(result)
        }
        installCurrentCancellation(cancellation, expected: .authenticating)
    }

    private static func invalidateLateAuthenticatedSession(_ result: ApprovalAuthenticationResult) {
        if case let .authenticated(session) = result {
            session.invalidate()
        }
    }

    private func authenticationFinished(_ result: ApprovalAuthenticationResult) {
        switch result {
        case .cancelled:
            finish(.failure(.authenticationCancelled))
        case .unavailable:
            finish(.failure(.authenticationUnavailable))
        case .failed:
            finish(.failure(.authenticationFailed))
        case let .authenticated(session):
            guard transition(from: .authenticating, to: .signing), stillWithinChallengeExpiry() else {
                session.invalidate()
                if !stillWithinChallengeExpiry() { finish(.failure(.challengeExpired)) }
                return
            }
            // A cancellation or deadline may win between the phase transition
            // and this callback.  Do not retain a freshly authenticated
            // session after that terminal path has already invalidated its
            // session slot; invalidate this arriving session instead.
            guard installAuthenticatedSession(session) else {
                session.invalidate()
                return
            }
            beginSigning(session: session)
        }
    }

    private func beginSigning(session: any AuthenticatedApprovalSession) {
        guard isCurrent(.signing), stillWithinChallengeExpiry() else {
            finish(.failure(.challengeExpired))
            return
        }
        let unsigned: UnsignedApprovalReceipt
        do {
            unsigned = try ApprovalReceiptFactory.makeUnsigned(
                challenge: challenge,
                review: review,
                configuration: configuration,
                now: clock(),
                receiptIdentifier: identifierGenerator.nextReceiptIdentifier()
            )
        } catch let error as ApprovalHostError {
            finish(.failure(error))
            return
        } catch {
            finish(.failure(.signingFailed))
            return
        }
        // The concrete signer, rather than this preparation path, claims the
        // one-shot permit immediately before protected-key work. That lets a
        // cancellation before the handoff end without consuming the nonce,
        // while every post-claim outcome remains fail-closed and nonretryable.
        let permit = ApprovalRequestSigningPermit(request: self, consume: consume)
        guard permit.permitsSigning() else {
            finish(.failure(.authenticationCancelled))
            return
        }
        guard beginAdapterHandoff(expected: .signing) else { return }
        defer { endAdapterHandoff() }
        let cancellation = signer.sign(
            unsigned.signingPayload,
            authenticatedSession: session,
            permit: permit
        ) { [weak self] result in
            self?.signingFinished(unsigned: unsigned, permit: permit, result: result)
        }
        installCurrentCancellation(cancellation, expected: .signing)
    }

    private func signingFinished(
        unsigned: UnsignedApprovalReceipt,
        permit: any ReceiptSigningPermit,
        result: Result<Data, ApprovalHostError>
    ) {
        guard isCurrent(.signing) else { return }
        guard stillWithinChallengeExpiry() else {
            finish(.failure(.challengeExpired))
            return
        }
        switch result {
        case let .success(signature):
            guard permit.hasClaimedSigning() else {
                finish(.failure(.signingFailed))
                return
            }
            do {
                let receipt = try ApprovalReceiptFactory.finalize(unsigned, signature: signature)
                if let postSigningRoute {
                    beginPostSigningConsumption(
                        receipt: receipt,
                        route: postSigningRoute
                    )
                } else {
                    finish(.success(.receipt(receipt)))
                }
            } catch let error as ApprovalHostError {
                finish(.failure(error))
            } catch {
                finish(.failure(.signingFailed))
            }
        case let .failure(error):
            finish(.failure(error))
        }
    }

    /// Holds the exact fresh session only while the one package-only consumer
    /// synchronously performs the protected redemption handoff. A terminal
    /// cancellation closes the continuation gate immediately, but leaves
    /// session invalidation to `completePostSigningHandoff` so a concurrent
    /// protected operation never races an LAContext invalidation.
    private func beginPostSigningConsumption(
        receipt: SignedApprovalReceipt,
        route: ApprovalPostSigningRoute
    ) {
        let lease: ApprovalPostSigningSessionLease
        lock.lock()
        guard phase == .signing,
              clock() < challenge.expiresAt,
              let session = authenticatedSession
        else {
            lock.unlock()
            finish(.failure(.challengeExpired))
            return
        }
        phase = .postSigningConsumption
        currentCancellation = nil
        lease = ApprovalPostSigningSessionLease(
            session: session,
            requestMayContinue: { [weak self] in
                self?.mayContinuePostSigningConsumption() ?? false
            }
        )
        postSigningLease = lease
        postSigningHandoffActive = true
        adapterHandoffs += 1
        lock.unlock()

        let result: Result<ApprovalTerminalValue, ApprovalHostError>
        switch route {
        case let .v1(consumer):
            result = consumer.consume(
                signedReceipt: receipt,
                expectedChallenge: challenge,
                sessionLease: lease
            ).map(ApprovalTerminalValue.nativeBridgeCandidate)
        case let .metaCampaignPauseV2(consumer):
            result = consumer.consume(
                signedReceipt: receipt,
                expectedChallenge: challenge,
                sessionLease: lease
            ).map(ApprovalTerminalValue.nativeMetaCampaignPauseBridgeCandidate)
        }
        if !lease.wasUsed {
            finish(.failure(.signingFailed))
        } else if !lease.shouldContinue() {
            // The protected consumer must pass its own continuation gate at
            // every boundary, but the engine owns the final expiry/cancellation
            // decision before it commits a candidate for delivery.
            finish(.failure(
                stillWithinChallengeExpiry() ? .authenticationCancelled : .challengeExpired
            ))
        } else {
            switch result {
            case let .success(candidate):
                finish(.success(candidate))
            case let .failure(error):
                finish(.failure(error))
            }
        }
        completePostSigningHandoff(lease)
    }

    private func mayContinuePostSigningConsumption() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        return phase == .postSigningConsumption && clock() < challenge.expiresAt
    }

    /// Completes the deferred terminal delivery after the synchronous
    /// consumer returns. The session is invalidated exactly here, never by a
    /// racing cancellation while the consumer holds its callback lease.
    private func completePostSigningHandoff(_ lease: ApprovalPostSigningSessionLease) {
        let result: Result<ApprovalTerminalValue, ApprovalHostError>
        let session: (any AuthenticatedApprovalSession)?
        lock.lock()
        guard postSigningHandoffActive, postSigningLease === lease else {
            lock.unlock()
            return
        }
        postSigningHandoffActive = false
        postSigningLease = nil
        lease.close()
        if phase != .finished {
            phase = .finished
            deferredTerminalResult = .failure(.signingFailed)
            currentCancellation = nil
            deadlineCancellation = nil
        }
        guard let deferredTerminalResult else {
            lock.unlock()
            return
        }
        result = deferredTerminalResult
        self.deferredTerminalResult = nil
        session = authenticatedSession
        authenticatedSession = nil
        lock.unlock()

        session?.invalidate()
        completion(result)

        lock.lock()
        terminalReady = true
        adapterHandoffs -= 1
        let deliverTerminal = claimTerminalIfDrained()
        lock.unlock()
        if deliverTerminal { terminal() }
    }

    private func stillWithinChallengeExpiry() -> Bool { clock() < challenge.expiresAt }

    private func installAuthenticatedSession(_ session: any AuthenticatedApprovalSession) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard phase == .signing else { return false }
        authenticatedSession = session
        return true
    }

    fileprivate func permitsProtectedSigning() -> Bool {
        isCurrent(.signing) && stillWithinChallengeExpiry()
    }

    fileprivate func claimProtectedSigning(_ consume: () -> Bool) -> Bool {
        // Hold the request phase lock while claiming the engine's generation-
        // bound nonce. A terminal cancellation cannot interleave between the
        // phase check and the one-shot handoff.
        lock.lock()
        defer { lock.unlock() }
        guard phase == .signing, clock() < challenge.expiresAt else { return false }
        return consume()
    }

    private func isCurrent(_ expected: Phase) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        return phase == expected
    }

    private func transition(from expected: Phase, to next: Phase) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard phase == expected else { return false }
        phase = next
        currentCancellation = nil
        return true
    }

    private func installDeadline(_ cancellation: any ApprovalCancellation) {
        lock.lock()
        let shouldCancel = phase == .finished
        if !shouldCancel { deadlineCancellation = cancellation }
        lock.unlock()
        if shouldCancel { cancellation.cancel() }
    }

    private func installCurrentCancellation(_ cancellation: any ApprovalCancellation, expected: Phase) {
        lock.lock()
        let shouldCancel = phase != expected
        if !shouldCancel { currentCancellation = cancellation }
        lock.unlock()
        if shouldCancel { cancellation.cancel() }
    }

    private func finish(_ result: Result<ApprovalTerminalValue, ApprovalHostError>) {
        let cancellation: (any ApprovalCancellation)?
        let deadline: (any ApprovalCancellation)?
        let session: (any AuthenticatedApprovalSession)?
        let deferSessionInvalidation: Bool
        lock.lock()
        guard phase != .finished else {
            // A post-signing consumer may have returned a candidate while the
            // request is still in its synchronous handoff window. A racing
            // cancellation, expiry, or other terminal failure wins over that
            // not-yet-delivered candidate and closes the consumer's gate.
            if postSigningHandoffActive,
               case .success = deferredTerminalResult,
               case .failure = result
            {
                postSigningLease?.close()
                deferredTerminalResult = result
            }
            lock.unlock()
            return
        }
        phase = .finished
        cancellation = currentCancellation
        deadline = deadlineCancellation
        currentCancellation = nil
        deadlineCancellation = nil
        if postSigningHandoffActive {
            postSigningLease?.close()
            deferredTerminalResult = result
            session = nil
            deferSessionInvalidation = true
        } else {
            session = authenticatedSession
            authenticatedSession = nil
            deferSessionInvalidation = false
        }
        lock.unlock()
        cancellation?.cancel()
        deadline?.cancel()
        if deferSessionInvalidation { return }
        session?.invalidate()
        completion(result)
        lock.lock()
        terminalReady = true
        let deliverTerminal = claimTerminalIfDrained()
        lock.unlock()
        if deliverTerminal { terminal() }
    }

    private func beginAdapterHandoff(expected: Phase) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard phase == expected else { return false }
        adapterHandoffs += 1
        return true
    }

    private func endAdapterHandoff() {
        lock.lock()
        adapterHandoffs -= 1
        let deliverTerminal = claimTerminalIfDrained()
        lock.unlock()
        if deliverTerminal { terminal() }
    }

    /// Called only under the phase lock. Completion and adapter callbacks may
    /// reenter; exactly one path can release the engine generation/admission.
    private func claimTerminalIfDrained() -> Bool {
        guard terminalReady, adapterHandoffs == 0, !terminalDelivered else { return false }
        terminalDelivered = true
        return true
    }
}

private final class ApprovalRequestSigningPermit: ReceiptSigningPermit, @unchecked Sendable {
    private weak var request: ApprovalRequest?
    private let consume: () -> Bool
    private let lock = NSLock()
    private var claimed = false

    init(request: ApprovalRequest, consume: @escaping () -> Bool) {
        self.request = request
        self.consume = consume
    }

    func permitsSigning() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard !claimed else { return false }
        guard let request else { return false }
        return request.permitsProtectedSigning()
    }

    func claimSigning() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard !claimed,
              let request,
              request.claimProtectedSigning(consume)
        else { return false }
        claimed = true
        return true
    }

    func hasClaimedSigning() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        return claimed
    }
}
