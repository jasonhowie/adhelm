/// Deliberately absent route selection for ordinary source and the existing
/// V1 artifact profile. A separately reviewed Meta artifact profile replaces
/// this source in an isolated build tree with fixed digest-bound constants.
/// There is no runtime argument, environment, file, or Bundle fallback.
package enum GeneratedMetaCampaignPauseApprovalRouteBinding {
  package static func resolve(
    metadata _: ApprovalHostBuildMetadataV1,
    configuration _: TrustedHostConfiguration
  ) throws -> NativeMetaCampaignPauseApprovalRouteBindingV2? {
    nil
  }
}
