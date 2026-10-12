@preconcurrency import AppKit
import ApprovalHostCore
import CoreGraphics
import Darwin
import Foundation

/// Actual host application lifecycle for a separately signed and registered
/// deployment. Generated metadata is its sole configuration source. Building
/// the app never calls this entry point or establishes installed authority.
@MainActor
package enum InstalledApprovalHostApplication {
  package static func run(metadata: ApprovalHostBuildMetadataV1) throws {
    try run(
      metadata: metadata,
      desktopAvailable: interactiveDesktopAvailable,
      prepareRuntime: { try ApprovalHostInstallation.bootstrapInstalledHost(metadata: $0) },
      runUserInterface: { runtime in
        let application = AppKitInstalledApprovalHostApplication(runtime: runtime)
        try withExtendedLifetime(application) { try application.run() }
      }
    )
  }

  /// Module-internal lifecycle seam. Tests use fake desktop/runtime/UI
  /// collaborators; an installed product cannot inject them.
  static func run(
    metadata: ApprovalHostBuildMetadataV1,
    desktopAvailable: () -> Bool,
    prepareRuntime: (ApprovalHostBuildMetadataV1) throws -> InstalledApprovalHostRuntime,
    runUserInterface: (InstalledApprovalHostRuntime) throws -> Void
  ) throws {
    guard desktopAvailable() else { throw ApprovalHostError.authenticationUnavailable }
    let runtime = try prepareRuntime(metadata)
    defer { runtime.shutdown() }
    try runUserInterface(runtime)
  }

  private static func interactiveDesktopAvailable() -> Bool {
    guard let session = CGSessionCopyCurrentDictionary() as? [String: Any],
      session[kCGSessionOnConsoleKey as String] as? Bool == true,
      session[kCGSessionLoginDoneKey as String] as? Bool == true,
      let userID = session[kCGSessionUserIDKey as String] as? NSNumber,
      userID.uint64Value == UInt64(getuid())
    else { return false }
    return true
  }
}

/// Local settings are reachable only through this app's status menu. Incoming
/// XPC challenges have no enrollment/revocation/menu action surface.
@MainActor
private final class AppKitInstalledApprovalHostApplication: NSObject, NSApplicationDelegate,
  NSMenuDelegate
{
  private enum KeyOperation: Sendable {
    case enroll
    case setupOneTimeApprovals
    case revoke
  }

  private enum KeyOperationSuccess: Sendable {
    case enrolled
    case oneTimeApprovalsInitialized
    case oneTimeApprovalsAlreadySetUp
    case revoked

    var alertText: String {
      switch self {
      case .enrolled: "Approval key created"
      case .oneTimeApprovalsInitialized: "One-time approval protection set up"
      case .oneTimeApprovalsAlreadySetUp: "One-time approval protection already set up"
      case .revoked: "Approval key revoked"
      }
    }

    var informativeText: String {
      switch self {
      case .enrolled, .revoked:
        "This local key operation does not execute a provider change."
      case .oneTimeApprovalsInitialized, .oneTimeApprovalsAlreadySetUp:
        "This records protection against reusing an approval. It does not approve or execute a provider change."
      }
    }
  }

  private let runtime: InstalledApprovalHostRuntime
  private var statusItem: NSStatusItem?
  private var enrollItem: NSMenuItem?
  private var setupOneTimeApprovalsItem: NSMenuItem?
  private var revokeItem: NSMenuItem?
  private var operationID: UUID?
  private var interactionLease: InstalledApprovalHostInteractionLease?
  private var pendingCancellation: (any ApprovalCancellation)?
  private var needsInspection = false
  private var stopping = false

  init(runtime: InstalledApprovalHostRuntime) {
    self.runtime = runtime
    super.init()
  }

  func run() throws {
    // Runtime creation has already compiled both fixed requirements and
    // verified the current host before the first NSApplication/UI access.
    let application = NSApplication.shared
    guard application.setActivationPolicy(.accessory) else {
      throw ApprovalHostError.authenticationUnavailable
    }
    application.delegate = self
    installStatusMenu()
    defer {
      stop()
      if let statusItem { NSStatusBar.system.removeStatusItem(statusItem) }
      statusItem = nil
      application.delegate = nil
    }
    withExtendedLifetime(self) { application.run() }
  }

  func applicationWillTerminate(_ notification: Notification) {
    stop()
  }

  private func installStatusMenu() {
    let menu = NSMenu(title: "AdHelm Approval Host")
    menu.autoenablesItems = false
    menu.delegate = self
    let heading = NSMenuItem(title: "Local approval settings", action: nil, keyEquivalent: "")
    heading.isEnabled = false
    menu.addItem(heading)
    menu.addItem(.separator())
    enrollItem = addItem("Set Up Approval Key…", action: #selector(enroll), to: menu)
    setupOneTimeApprovalsItem = addItem(
      "Set Up One-Time Approvals…",
      action: #selector(setUpOneTimeApprovals),
      to: menu
    )
    revokeItem = addItem("Revoke Approval Key…", action: #selector(revoke), to: menu)
    menu.addItem(.separator())
    _ = addItem("Quit Approval Host", action: #selector(quit), to: menu)
    let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    item.button?.title = "AdHelm"
    item.button?.toolTip = "Local approval settings"
    item.menu = menu
    statusItem = item
  }

  private func addItem(_ title: String, action: Selector, to menu: NSMenu) -> NSMenuItem {
    let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
    item.target = self
    menu.addItem(item)
    return item
  }

  @objc private func enroll() { begin(.enroll) }
  @objc private func setUpOneTimeApprovals() { begin(.setupOneTimeApprovals) }
  @objc private func revoke() { begin(.revoke) }
  @objc private func quit() { NSApplication.shared.terminate(nil) }

  func menuWillOpen(_ menu: NSMenu) { updateControls() }

  private func begin(_ operation: KeyOperation) {
    guard !stopping, !needsInspection, operationID == nil else { return }
    let lease: InstalledApprovalHostInteractionLease
    do { lease = try runtime.acquireLocalInteraction() } catch {
      updateControls()
      return
    }
    do {
      interactionLease = lease
      let identifier = UUID()
      operationID = identifier
      updateControls()
      NSApplication.shared.activate(ignoringOtherApps: true)
      let finish: (Result<KeyOperationSuccess, ApprovalHostError>) -> Void = { [weak self] result in
        Task { @MainActor in
          self?.finish(identifier: identifier, result: result)
        }
      }
      let cancellation: any ApprovalCancellation
      switch operation {
      case .enroll:
        let controller = try runtime.makeEnrollmentController(interactionLease: lease)
        cancellation = controller.requestExplicitEnrollment { result in
          finish(result.map { _ in .enrolled })
        }
      case .setupOneTimeApprovals:
        let controller = try runtime.makeReceiptRedemptionGlobalSetupController(
          interactionLease: lease
        )
        cancellation = controller.requestExplicitSetup { result in
          finish(
            result.map {
              switch $0 {
              case .initialized: .oneTimeApprovalsInitialized
              case .alreadyInitialized: .oneTimeApprovalsAlreadySetUp
              }
            })
        }
      case .revoke:
        let controller = try runtime.makeEnrollmentController(interactionLease: lease)
        cancellation = controller.requestExplicitRevocation { result in
          finish(result.map { _ in .revoked })
        }
      }
      // A modal or synchronous callback may complete before installation.
      // Never retain a cancellation handle for an already-ended generation.
      if operationID == identifier, !stopping {
        pendingCancellation = cancellation
      } else {
        cancellation.cancel()
      }
    } catch let error as ApprovalHostError {
      operationID = nil
      pendingCancellation = nil
      interactionLease = nil
      updateControls()
      defer { lease.finish() }
      show(error: error)
    } catch {
      operationID = nil
      pendingCancellation = nil
      interactionLease = nil
      updateControls()
      defer { lease.finish() }
      show(error: .signingUnavailable)
    }
  }

  private func finish(
    identifier: UUID,
    result: Result<KeyOperationSuccess, ApprovalHostError>
  ) {
    guard !stopping, operationID == identifier else { return }
    operationID = nil
    pendingCancellation = nil
    let lease = interactionLease
    interactionLease = nil
    defer { lease?.finish() }
    switch result {
    case .success(let success):
      updateControls()
      let alert = NSAlert()
      alert.messageText = success.alertText
      alert.informativeText = success.informativeText
      alert.addButton(withTitle: "OK")
      alert.runModal()
    case .failure(let error):
      if error == .enrollmentStateUnverified { needsInspection = true }
      updateControls()
      if error != .reviewCancelled, error != .authenticationCancelled { show(error: error) }
    }
  }

  private func updateControls() {
    let enabled =
      !stopping && !needsInspection && operationID == nil
      && runtime.isInteractionAvailable
    enrollItem?.isEnabled = enabled
    setupOneTimeApprovalsItem?.isEnabled = enabled
    revokeItem?.isEnabled = enabled
  }

  private func show(error: ApprovalHostError) {
    guard !stopping else { return }
    let alert = NSAlert()
    alert.messageText = "Local approval setup did not complete"
    alert.informativeText = error.publicMessage
    alert.addButton(withTitle: "OK")
    alert.runModal()
  }

  private func stop() {
    guard !stopping else { return }
    stopping = true
    operationID = nil
    let cancellation = pendingCancellation
    pendingCancellation = nil
    updateControls()
    cancellation?.cancel()
    runtime.shutdown()
    interactionLease?.finish()
    interactionLease = nil
  }
}
