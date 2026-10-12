/// Deliberately unconfigured source selection. The artifact builder replaces
/// this file only inside an isolated source archive with strictly generated
/// Swift constants. There is no runtime argument, environment, file or Bundle
/// configuration fallback.
package enum GeneratedApprovalHostBuildMetadata {
  package static func make() throws -> ApprovalHostBuildMetadataV1 {
    throw ApprovalHostError.hostNotInstalled
  }
}
