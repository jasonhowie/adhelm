import ApprovalHostCore
import ApprovalHostMacOS
import Darwin
import Foundation

@main
struct InstalledApprovalClient {
  static func main() {
    guard CommandLine.arguments.count == 1 else {
      fputs(
        "APPROVAL_CLIENT_ARGUMENTS_UNSUPPORTED: This client accepts no launch arguments.\n", stderr)
      Darwin.exit(64)
    }
    do {
      let metadata = try GeneratedApprovalHostBuildMetadata.make()
      try NativeApprovalBridgeHelperRunner.run(metadata: metadata)
    } catch let error as ApprovalHostError {
      fputs("\(error.code): \(error.publicMessage)\n", stderr)
      Darwin.exit(78)
    } catch {
      fputs(
        "APPROVAL_CLIENT_UNVERIFIED: The private native approval exchange did not complete.\n",
        stderr)
      Darwin.exit(70)
    }
  }
}
