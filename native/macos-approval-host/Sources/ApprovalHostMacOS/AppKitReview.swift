@preconcurrency import AppKit
import ApprovalHostCore
@preconcurrency import Dispatch
import Foundation

/// A concrete local review adapter.  It displays every digest-bound review fact
/// in a selectable scroll view and requires the person to choose a named button
/// before the state machine starts OS authentication.  It does not claim that a
/// click proves comprehension or defeats hostile same-user software.
public final class AppKitApprovalReviewer: ApprovalReviewer {
  public init() {}

  @discardableResult
  public func present(
    review: DeterministicApprovalReview,
    completion: @escaping (ApprovalReviewDecision) -> Void
  ) -> any ApprovalCancellation {
    let cancellation = AppKitModalCancellation()
    let callback = ReviewCallback(completion)
    Task { @MainActor in
      guard !cancellation.isCancelled else { return }
      _ = NSApplication.shared
      let alert = NSAlert()
      alert.messageText = "Review exact approval"
      alert.informativeText =
        "Review every bound fact below. Continuing starts fresh OS authentication; it does not execute a provider operation."
      alert.addButton(withTitle: "Continue to Authenticate")
      alert.addButton(withTitle: "Cancel")
      alert.accessoryView = Self.reviewScrollView(text: review.text)
      cancellation.install(window: alert.window)
      let response = alert.runModal()
      cancellation.clear(window: alert.window)
      guard !cancellation.isCancelled else { return }
      callback.call(response == .alertFirstButtonReturn ? .continueToAuthentication : .cancel)
    }
    return cancellation
  }

  @MainActor
  private static func reviewScrollView(text: String) -> NSScrollView {
    let scrollView = NSScrollView(frame: NSRect(x: 0, y: 0, width: 720, height: 360))
    scrollView.hasVerticalScroller = true
    scrollView.hasHorizontalScroller = false
    scrollView.autohidesScrollers = false
    scrollView.borderType = .bezelBorder
    let textView = NSTextView(frame: scrollView.bounds)
    textView.string = text
    textView.isEditable = false
    textView.isSelectable = true
    textView.isRichText = false
    textView.font = NSFont.monospacedSystemFont(ofSize: 11, weight: .regular)
    textView.textContainerInset = NSSize(width: 8, height: 8)
    textView.textContainer?.widthTracksTextView = true
    textView.isHorizontallyResizable = false
    textView.isVerticallyResizable = true
    scrollView.documentView = textView
    return scrollView
  }
}

/// Enrollment is deliberately separate from approval and XPC.  A future
/// installed app can expose this only from its own local settings screen; no
/// incoming request can create, replace, or select a signing key.
@MainActor
public final class AppKitProtectedKeyEnrollmentController {
  private let store: any ProtectedKeyStore
  private let authenticator: any FreshAuthenticator
  private let requireInteraction: () throws -> Void
  private var requested = false

  init(
    store: any ProtectedKeyStore, authenticator: any FreshAuthenticator,
    requireInteraction: @escaping () throws -> Void
  ) {
    self.store = store
    self.authenticator = authenticator
    self.requireInteraction = requireInteraction
  }

  @discardableResult
  public func requestExplicitEnrollment(
    completion: @escaping (Result<Void, ApprovalHostError>) -> Void
  ) -> any ApprovalCancellation {
    do {
      try requireInteraction()
      guard !requested else { throw ApprovalHostError.requestAlreadyActive }
      requested = true
    } catch let error as ApprovalHostError {
      completion(.failure(error))
      return NoopApprovalCancellation()
    } catch {
      completion(.failure(.hostNotInstalled))
      return NoopApprovalCancellation()
    }
    let callback = EnrollmentCallback(completion)
    let cancellation = EnrollmentCancellation { error in
      callback.call(.failure(error))
    }
    guard !cancellation.isCancelled else { return cancellation }
    // Code identity is checked before an enrollment screen can be shown.
    // The coordinator checks again immediately before every protected
    // Keychain operation, so this UI preflight cannot be reused later.
    do {
      try store.validateConfiguredHostCodeIdentity()
    } catch {
      callback.call(.failure(.signingUnavailable))
      return cancellation
    }
    _ = NSApplication.shared
    let alert = NSAlert()
    alert.messageText = "Create protected approval signing key"
    alert.informativeText =
      "This creates one fixed local approval-host key. It cannot approve a provider action by itself."
    alert.addButton(withTitle: "Create Protected Key")
    alert.addButton(withTitle: "Cancel")
    cancellation.install(window: alert.window)
    let response = alert.runModal()
    cancellation.clear(window: alert.window)
    guard !cancellation.isCancelled else { return cancellation }
    guard response == .alertFirstButtonReturn else {
      callback.call(.failure(.reviewCancelled))
      return cancellation
    }
    let authentication = authenticator.authenticate(
      reason: "create the protected local approval signing key"
    ) { [store] result in
      if EnrollmentAuthenticationGate.invalidatesArrivingSession(
        result,
        whenCancelled: cancellation.isCancelled
      ) {
        return
      }
      guard case .authenticated(let session) = result else {
        callback.call(.failure(Self.authenticationError(result)))
        return
      }
      guard let localSession = session as? LocalAuthenticationApprovalSession else {
        session.invalidate()
        callback.call(.failure(.authenticationUnavailable))
        return
      }
      // Cancellation can race after the callback's initial check.  The
      // session is never retained in that case, and the arriving fresh
      // authentication is invalidated before enrollment work begins.
      guard !cancellation.isCancelled else {
        localSession.invalidate()
        return
      }
      DispatchQueue.global(qos: .userInitiated).async {
        defer { localSession.invalidate() }
        guard cancellation.beginProtectedWork() else { return }
        do {
          _ = try store.enrollInitialPrivateKey(
            authenticatedSession: localSession,
            shouldContinue: { cancellation.mayContinueProtectedWork }
          )
          guard !cancellation.isCancelled else { return }
          callback.call(.success(()))
        } catch {
          callback.call(.failure(cancellation.protectedWorkFailure()))
        }
      }
    }
    cancellation.install(authentication: authentication)
    return cancellation
  }

  /// Revocation is another settings-only local action.  It cannot arrive
  /// through XPC, a challenge, or a provider request, and it never deletes
  /// the fixed key.  The coordinator writes REVOKED first and leaves any
  /// ambiguous Keychain state for explicit local recovery.
  @discardableResult
  public func requestExplicitRevocation(
    completion: @escaping (Result<Void, ApprovalHostError>) -> Void
  ) -> any ApprovalCancellation {
    do {
      try requireInteraction()
      guard !requested else { throw ApprovalHostError.requestAlreadyActive }
      requested = true
    } catch let error as ApprovalHostError {
      completion(.failure(error))
      return NoopApprovalCancellation()
    } catch {
      completion(.failure(.hostNotInstalled))
      return NoopApprovalCancellation()
    }
    let callback = EnrollmentCallback(completion)
    let cancellation = EnrollmentCancellation { error in
      callback.call(.failure(error))
    }
    guard !cancellation.isCancelled else { return cancellation }
    do {
      try store.validateConfiguredHostCodeIdentity()
    } catch {
      callback.call(.failure(.signingUnavailable))
      return cancellation
    }
    _ = NSApplication.shared
    let alert = NSAlert()
    alert.messageText = "Revoke protected approval signing key"
    alert.informativeText =
      "This disables the fixed local signing key. It does not delete or replace the key."
    alert.addButton(withTitle: "Revoke Protected Key")
    alert.addButton(withTitle: "Cancel")
    cancellation.install(window: alert.window)
    let response = alert.runModal()
    cancellation.clear(window: alert.window)
    guard !cancellation.isCancelled else { return cancellation }
    guard response == .alertFirstButtonReturn else {
      callback.call(.failure(.reviewCancelled))
      return cancellation
    }
    let authentication = authenticator.authenticate(
      reason: "revoke the protected local approval signing key"
    ) { [store] result in
      if EnrollmentAuthenticationGate.invalidatesArrivingSession(
        result,
        whenCancelled: cancellation.isCancelled
      ) {
        return
      }
      guard case .authenticated(let session) = result else {
        callback.call(.failure(Self.authenticationError(result)))
        return
      }
      guard let localSession = session as? LocalAuthenticationApprovalSession else {
        session.invalidate()
        callback.call(.failure(.authenticationUnavailable))
        return
      }
      guard !cancellation.isCancelled else {
        localSession.invalidate()
        return
      }
      DispatchQueue.global(qos: .userInitiated).async {
        defer { localSession.invalidate() }
        guard cancellation.beginProtectedWork() else { return }
        do {
          _ = try store.revokeInitialPrivateKey(
            authenticatedSession: localSession,
            shouldContinue: { cancellation.mayContinueProtectedWork }
          )
          guard !cancellation.isCancelled else { return }
          callback.call(.success(()))
        } catch {
          callback.call(.failure(cancellation.protectedWorkFailure()))
        }
      }
    }
    cancellation.install(authentication: authentication)
    return cancellation
  }

  nonisolated private static func authenticationError(_ result: ApprovalAuthenticationResult)
    -> ApprovalHostError
  {
    switch result {
    case .cancelled: .authenticationCancelled
    case .unavailable: .authenticationUnavailable
    case .failed: .authenticationFailed
    case .authenticated: .authenticationFailed
    }
  }
}

/// A separate local status-menu action for the explicit M36 fixed-global
/// initializer. It neither enrolls nor replaces a key, and it does not make a
/// receipt, bridge candidate, Python handoff, provider action, or XPC route.
/// The configured runtime supplies the one fixed setup store; callers cannot
/// select a Keychain item, configuration, selector, or recovery action.
@MainActor
final class AppKitReceiptRedemptionGlobalSetupController {
  private let store: any ReceiptRedemptionGlobalSetupStore
  private let authenticator: any FreshAuthenticator
  private let requireInteraction: @Sendable () throws -> Void
  private var requested = false

  init(
    store: any ReceiptRedemptionGlobalSetupStore,
    authenticator: any FreshAuthenticator,
    requireInteraction: @escaping @Sendable () throws -> Void
  ) {
    self.store = store
    self.authenticator = authenticator
    self.requireInteraction = requireInteraction
  }

  @discardableResult
  func requestExplicitSetup(
    completion:
      @escaping (Result<ReceiptRedemptionGlobalInitializationResult, ApprovalHostError>)
      -> Void
  ) -> any ApprovalCancellation {
    do {
      try requireInteraction()
      guard !requested else { throw ApprovalHostError.requestAlreadyActive }
      requested = true
    } catch let error as ApprovalHostError {
      completion(.failure(error))
      return NoopApprovalCancellation()
    } catch {
      completion(.failure(.hostNotInstalled))
      return NoopApprovalCancellation()
    }

    let callback = ReceiptRedemptionGlobalSetupCallback(completion)
    let cancellation = ReceiptRedemptionGlobalSetupCancellation { result in
      callback.call(result)
    }
    guard !cancellation.isCancelled else { return cancellation }
    do {
      // This preflight is local UI hygiene only. The fixed store rechecks the
      // same compiled host identity inside the later protected sequence.
      try store.validateConfiguredHostCodeIdentity()
    } catch {
      cancellation.finish(.failure(.signingUnavailable))
      return cancellation
    }

    _ = NSApplication.shared
    let alert = NSAlert()
    alert.messageText = "Set up one-time approval protection"
    alert.informativeText =
      "This records local protection against reusing an approval. It does not approve or execute a provider change."
    alert.addButton(withTitle: "Set Up One-Time Approvals")
    alert.addButton(withTitle: "Cancel")
    cancellation.install(window: alert.window)
    let response = alert.runModal()
    cancellation.clear(window: alert.window)
    guard !cancellation.isCancelled else { return cancellation }
    guard response == .alertFirstButtonReturn else {
      cancellation.finish(.failure(.reviewCancelled))
      return cancellation
    }

    let authentication = authenticator.authenticate(
      reason: "set up protection against reusing local approvals"
    ) { [store, requireInteraction] result in
      if EnrollmentAuthenticationGate.invalidatesArrivingSession(
        result,
        whenCancelled: cancellation.isCancelled
      ) {
        return
      }
      guard case .authenticated(let session) = result else {
        cancellation.finish(.failure(Self.authenticationError(result)))
        return
      }
      guard let localSession = session as? LocalAuthenticationApprovalSession else {
        session.invalidate()
        cancellation.finish(.failure(.authenticationUnavailable))
        return
      }
      guard !cancellation.isCancelled else {
        localSession.invalidate()
        return
      }
      DispatchQueue.global(qos: .userInitiated).async {
        defer { localSession.invalidate() }
        guard cancellation.beginProtectedWork() else { return }
        let result = ReceiptRedemptionGlobalSetupExecution.perform(
          store: store,
          authenticatedSession: localSession,
          shouldContinue: {
            guard cancellation.mayContinueProtectedWork else { return false }
            do {
              try requireInteraction()
              return true
            } catch {
              return false
            }
          }
        )
        cancellation.finish(result)
      }
    }
    cancellation.install(authentication: authentication)
    return cancellation
  }

  nonisolated private static func authenticationError(_ result: ApprovalAuthenticationResult)
    -> ApprovalHostError
  {
    switch result {
    case .cancelled: .authenticationCancelled
    case .unavailable: .authenticationUnavailable
    case .failed: .authenticationFailed
    case .authenticated: .authenticationFailed
    }
  }
}

private class AppKitModalCancellation: ApprovalCancellation, @unchecked Sendable {
  private let lock = NSLock()
  private weak var window: NSWindow?
  private var cancelled = false

  var isCancelled: Bool {
    lock.lock()
    defer { lock.unlock() }
    return cancelled
  }

  func install(window: NSWindow?) {
    lock.lock()
    self.window = window
    let shouldClose = cancelled
    lock.unlock()
    if shouldClose { closeWindowOnMain() }
  }

  func clear(window: NSWindow?) {
    lock.lock()
    if self.window === window { self.window = nil }
    lock.unlock()
  }

  func cancel() {
    lock.lock()
    cancelled = true
    lock.unlock()
    closeWindowOnMain()
  }

  private func closeWindowOnMain() {
    DispatchQueue.main.async { [weak self] in
      guard let window = self?.windowToClose() else { return }
      // Closing a window alone does not reliably end NSAlert.runModal.
      // Abort the modal session first so a cancelled review cannot leave
      // an invisible main-thread modal loop alive.
      NSApplication.shared.abortModal()
      window.close()
    }
  }

  private func windowToClose() -> NSWindow? {
    lock.lock()
    defer { lock.unlock() }
    return window
  }
}

/// A small pure seam for the cancellation race around an asynchronous fresh
/// authentication callback.  XCTest can use a fake session without invoking
/// LocalAuthentication or displaying a window.
enum EnrollmentAuthenticationGate {
  static func invalidatesArrivingSession(
    _ result: ApprovalAuthenticationResult,
    whenCancelled: Bool
  ) -> Bool {
    guard whenCancelled else { return false }
    if case .authenticated(let session) = result {
      session.invalidate()
    }
    return true
  }
}

/// Pure phase gate: cancellation before protected work guarantees no handoff;
/// once work starts, a cancelled caller must inspect state rather than assume
/// that no PENDING/ACTIVE/REVOKED record was durably written.
final class EnrollmentOperationState: @unchecked Sendable {
  private let lock = NSLock()
  private var started = false
  private var cancelled = false

  func beginProtectedWork() -> Bool {
    lock.lock()
    defer { lock.unlock() }
    guard !cancelled, !started else { return false }
    started = true
    return true
  }

  var mayContinueProtectedWork: Bool {
    lock.lock()
    defer { lock.unlock() }
    return started && !cancelled
  }

  func cancel() -> ApprovalHostError {
    lock.lock()
    defer { lock.unlock() }
    cancelled = true
    return started ? .enrollmentStateUnverified : .reviewCancelled
  }

  func protectedWorkFailure() -> ApprovalHostError {
    lock.lock()
    defer { lock.unlock() }
    // The worker can observe cancellation before the cancellation callback
    // reaches the one-shot completion gate. Both paths must report the same
    // uncertain local state after a protected-work handoff.
    if cancelled { return started ? .enrollmentStateUnverified : .reviewCancelled }
    return .signingUnavailable
  }
}

private final class EnrollmentCancellation: AppKitModalCancellation, @unchecked Sendable {
  private let terminalCancellation: (ApprovalHostError) -> Void
  private let operationState = EnrollmentOperationState()
  private let terminalLock = NSLock()
  private var reportedTerminalCancellation = false
  private let authenticationLock = NSLock()
  private var authentication: (any ApprovalCancellation)?

  init(terminalCancellation: @escaping (ApprovalHostError) -> Void) {
    self.terminalCancellation = terminalCancellation
  }

  func beginProtectedWork() -> Bool { operationState.beginProtectedWork() }

  var mayContinueProtectedWork: Bool { operationState.mayContinueProtectedWork }

  func protectedWorkFailure() -> ApprovalHostError { operationState.protectedWorkFailure() }

  func install(authentication: any ApprovalCancellation) {
    authenticationLock.lock()
    self.authentication = authentication
    let shouldCancel = isCancelled
    authenticationLock.unlock()
    if shouldCancel { authentication.cancel() }
  }

  override func cancel() {
    let terminalError = operationState.cancel()
    super.cancel()
    authenticationLock.lock()
    let authentication = authentication
    authenticationLock.unlock()
    authentication?.cancel()
    terminalLock.lock()
    let shouldReport = !reportedTerminalCancellation
    reportedTerminalCancellation = true
    terminalLock.unlock()
    if shouldReport { terminalCancellation(terminalError) }
  }
}

private final class ReviewCallback: @unchecked Sendable {
  private let completion: (ApprovalReviewDecision) -> Void
  init(_ completion: @escaping (ApprovalReviewDecision) -> Void) { self.completion = completion }
  func call(_ decision: ApprovalReviewDecision) { completion(decision) }
}

private final class EnrollmentCallback: @unchecked Sendable {
  private let lock = NSLock()
  private var completed = false
  private let completion: (Result<Void, ApprovalHostError>) -> Void
  init(_ completion: @escaping (Result<Void, ApprovalHostError>) -> Void) {
    self.completion = completion
  }
  func call(_ result: Result<Void, ApprovalHostError>) {
    lock.lock()
    guard !completed else {
      lock.unlock()
      return
    }
    completed = true
    lock.unlock()
    completion(result)
  }
}

/// One closed terminal state for the explicit setup action. Cancellation wins
/// while the protected setup call is in flight, so a later success cannot
/// claim that a completed V2 marker/global write was safely observed by the
/// local UI. A cancellation after a terminal result is harmless and does not
/// overwrite that already-delivered outcome.
final class ReceiptRedemptionGlobalSetupOperationState: @unchecked Sendable {
  private let lock = NSLock()
  private var protectedWorkStarted = false
  private var terminal = false
  private var cancelled = false

  func beginProtectedWork() -> Bool {
    lock.lock()
    defer { lock.unlock() }
    guard !terminal, !cancelled, !protectedWorkStarted else { return false }
    protectedWorkStarted = true
    return true
  }

  var mayContinueProtectedWork: Bool {
    lock.lock()
    defer { lock.unlock() }
    return protectedWorkStarted && !cancelled && !terminal
  }

  func cancel() -> ApprovalHostError? {
    lock.lock()
    defer { lock.unlock() }
    guard !terminal else { return nil }
    cancelled = true
    terminal = true
    return protectedWorkStarted ? .enrollmentStateUnverified : .reviewCancelled
  }

  func finish(
    _ result: Result<ReceiptRedemptionGlobalInitializationResult, ApprovalHostError>
  ) -> Result<ReceiptRedemptionGlobalInitializationResult, ApprovalHostError>? {
    lock.lock()
    defer { lock.unlock() }
    guard !terminal else { return nil }
    terminal = true
    if cancelled { return .failure(.enrollmentStateUnverified) }
    return result
  }
}

private final class ReceiptRedemptionGlobalSetupCancellation: AppKitModalCancellation,
  @unchecked Sendable
{
  private let state = ReceiptRedemptionGlobalSetupOperationState()
  private let completion:
    (Result<ReceiptRedemptionGlobalInitializationResult, ApprovalHostError>)
      -> Void
  private let authenticationLock = NSLock()
  private var authentication: (any ApprovalCancellation)?

  init(
    completion:
      @escaping (Result<ReceiptRedemptionGlobalInitializationResult, ApprovalHostError>)
      -> Void
  ) {
    self.completion = completion
  }

  func beginProtectedWork() -> Bool { state.beginProtectedWork() }

  var mayContinueProtectedWork: Bool { state.mayContinueProtectedWork }

  func finish(_ result: Result<ReceiptRedemptionGlobalInitializationResult, ApprovalHostError>) {
    guard let terminal = state.finish(result) else { return }
    completion(terminal)
  }

  func install(authentication: any ApprovalCancellation) {
    authenticationLock.lock()
    self.authentication = authentication
    let shouldCancel = isCancelled
    authenticationLock.unlock()
    if shouldCancel { authentication.cancel() }
  }

  override func cancel() {
    let terminal = state.cancel()
    super.cancel()
    authenticationLock.lock()
    let authentication = authentication
    authenticationLock.unlock()
    authentication?.cancel()
    if let terminal { completion(.failure(terminal)) }
  }
}

private final class ReceiptRedemptionGlobalSetupCallback: @unchecked Sendable {
  private let lock = NSLock()
  private var completed = false
  private let completion:
    (Result<ReceiptRedemptionGlobalInitializationResult, ApprovalHostError>)
      -> Void

  init(
    _ completion:
      @escaping (Result<ReceiptRedemptionGlobalInitializationResult, ApprovalHostError>)
      -> Void
  ) {
    self.completion = completion
  }

  func call(_ result: Result<ReceiptRedemptionGlobalInitializationResult, ApprovalHostError>) {
    lock.lock()
    guard !completed else {
      lock.unlock()
      return
    }
    completed = true
    lock.unlock()
    completion(result)
  }
}
