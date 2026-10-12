import ApprovalHostCore
import ApprovalHostMacOS
import Darwin
import Foundation

@main
@MainActor
struct InstalledApprovalHostApp {
  static func main() {
    guard CommandLine.arguments.count == 1 else {
      fputs("APPROVAL_HOST_ARGUMENTS_UNSUPPORTED: This host accepts no launch arguments.\n", stderr)
      Darwin.exit(64)
    }
    do {
      let metadata = try GeneratedApprovalHostBuildMetadata.make()
      try InstalledApprovalHostApplication.run(metadata: metadata)
    } catch let error as ApprovalHostError {
      fputs("\(error.code): \(error.publicMessage)\n", stderr)
      Darwin.exit(78)
    } catch {
      fputs(
        "APPROVAL_HOST_NOT_INSTALLED: The configured local approval host could not start.\n", stderr
      )
      Darwin.exit(78)
    }
  }
}
