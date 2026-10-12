import ApprovalHostCore
import Foundation
@preconcurrency import LocalAuthentication

/// A successful session owns the fresh LAContext until the state machine has
/// either signed its typed receipt or failed.  It is deliberately not
/// serializable and cannot cross XPC.
public final class LocalAuthenticationApprovalSession: AuthenticatedApprovalSession, @unchecked Sendable {
    let context: LAContext

    init(context: LAContext) {
        self.context = context
    }

    public func invalidate() {
        context.invalidate()
    }
}

public final class LocalAuthenticationFreshAuthenticator: FreshAuthenticator {
    public init() {}

    @discardableResult
    public func authenticate(
        reason: String,
        completion: @escaping (ApprovalAuthenticationResult) -> Void
    ) -> any ApprovalCancellation {
        // This is a production adapter only.  Tests use fakes and the package
        // executable never reaches it without a separate installed build config.
        let context = LAContext()
        context.touchIDAuthenticationAllowableReuseDuration = 0
        context.localizedCancelTitle = "Cancel approval"
        let cancellation = LocalAuthenticationCancellation(context: context)
        let session = LocalAuthenticationApprovalSession(context: context)
        let callback = AuthenticationCallback(completion)
        var preflightError: NSError?
        guard context.canEvaluatePolicy(.deviceOwnerAuthentication, error: &preflightError) else {
            callback.call(.unavailable)
            return cancellation
        }
        context.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: reason) { success, error in
            guard !cancellation.isCancelled else {
                callback.call(.cancelled)
                return
            }
            if success {
                callback.call(.authenticated(session))
                return
            }
            callback.call(Self.classify(error))
        }
        return cancellation
    }

    private static func classify(_ error: Error?) -> ApprovalAuthenticationResult {
        guard let error = error as NSError? else { return .failed }
        switch LAError.Code(rawValue: error.code) {
        case .userCancel, .appCancel, .systemCancel:
            return .cancelled
        case .biometryNotAvailable, .passcodeNotSet, .biometryNotEnrolled:
            return .unavailable
        default:
            return .failed
        }
    }
}

private final class LocalAuthenticationCancellation: ApprovalCancellation, @unchecked Sendable {
    private let lock = NSLock()
    private let context: LAContext
    private var cancelled = false

    init(context: LAContext) {
        self.context = context
    }

    var isCancelled: Bool {
        lock.lock()
        defer { lock.unlock() }
        return cancelled
    }

    func cancel() {
        lock.lock()
        let needsInvalidation = !cancelled
        cancelled = true
        lock.unlock()
        if needsInvalidation { context.invalidate() }
    }
}

private final class AuthenticationCallback: @unchecked Sendable {
    private let completion: (ApprovalAuthenticationResult) -> Void
    init(_ completion: @escaping (ApprovalAuthenticationResult) -> Void) { self.completion = completion }
    func call(_ result: ApprovalAuthenticationResult) { completion(result) }
}
