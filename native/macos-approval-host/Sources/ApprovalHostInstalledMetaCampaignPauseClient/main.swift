import ApprovalHostCore
import ApprovalHostMacOS
import Darwin
import Foundation

@main
struct InstalledMetaCampaignPauseApprovalClient {
  static func main() {
    guard CommandLine.arguments.count == 1 else {
      fputs(
        "APPROVAL_META_CAMPAIGN_PAUSE_CLIENT_ARGUMENTS_UNSUPPORTED: This client accepts no launch arguments.\n",
        stderr
      )
      Darwin.exit(64)
    }
    do {
      // This default source is intentionally unconfigured. It must be checked
      // before constructing the runner or touching pipes, identity, XPC, or
      // any OS collaborator.
      let metadata = try GeneratedApprovalHostBuildMetadata.make()
      try NativeMetaCampaignPauseApprovalBridgeHelperRunner.run(metadata: metadata)
    } catch let error as ApprovalHostError {
      fputs("\(error.code): \(error.publicMessage)\n", stderr)
      Darwin.exit(78)
    } catch {
      fputs(
        "APPROVAL_META_CAMPAIGN_PAUSE_CLIENT_UNVERIFIED: The private Meta campaign PAUSE approval exchange did not complete.\n",
        stderr
      )
      Darwin.exit(70)
    }
  }
}
