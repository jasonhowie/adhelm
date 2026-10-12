import ApprovalHostCore
import Darwin
import Foundation

/// Build-only inspection of compiled constants. This target imports no AppKit,
/// Security, LocalAuthentication or XPC adapter and is never bundled in the app.
@main
struct ApprovalHostBuildInspector {
  static func main() {
    guard CommandLine.arguments.count == 1 else {
      fputs(
        "APPROVAL_BUILD_ARGUMENTS_UNSUPPORTED: Build inspection accepts no arguments.\n", stderr)
      Darwin.exit(64)
    }
    do {
      let metadata = try GeneratedApprovalHostBuildMetadata.make()
      let descriptor = try NativeApprovalHostArtifactDescriptorV1(metadata: metadata)
      FileHandle.standardOutput.write(descriptor.canonicalBytes)
    } catch let error as ApprovalHostError {
      fputs("\(error.code): \(error.publicMessage)\n", stderr)
      Darwin.exit(78)
    } catch {
      fputs("APPROVAL_HOST_NOT_INSTALLED: Compiled build metadata is unavailable.\n", stderr)
      Darwin.exit(78)
    }
  }
}
