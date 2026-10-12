import ApprovalHostCore
import ApprovalHostMacOS
import Darwin
import Foundation

@main
struct ApprovalHostFoundationApp {
    static func main() {
        do {
            try ApprovalHostInstallation.refuseUnconfiguredLaunch()
        } catch let error as ApprovalHostError {
            fputs("\(error.code): \(error.publicMessage)\n", stderr)
            Darwin.exit(78)
        } catch {
            fputs("APPROVAL_HOST_NOT_INSTALLED: no trusted build configuration.\n", stderr)
            Darwin.exit(78)
        }
    }
}
