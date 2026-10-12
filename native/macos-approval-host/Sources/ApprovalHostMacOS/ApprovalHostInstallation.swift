import ApprovalHostCore
import Foundation

/// The checked-in foundation has no installed build metadata.  The only
/// package-scoped construction path is defined by the installed bootstrap,
/// which performs fixed-metadata and host-code preflight before it constructs
/// a store, engine, or listener.
public enum ApprovalHostInstallation {
  /// The checked-in executable has no generated trusted constants.  Refuse
  /// rather than sourcing a service name, code requirement, or key tag from
  /// an argument, file, environment variable, or challenge.
  public static func refuseUnconfiguredLaunch() throws {
    throw ApprovalHostError.hostNotInstalled
  }
}
