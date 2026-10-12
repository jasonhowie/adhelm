import Foundation

/// Public failures are intentionally compact.  The host must never send a raw
/// parser, authentication, Security-framework, or XPC error back to a caller.
public enum ApprovalHostError: Error, Equatable, Sendable {
  case malformedChallenge
  case challengeExpired
  case challengeNotYetValid
  case reviewCancelled
  case authenticationCancelled
  case authenticationUnavailable
  case authenticationFailed
  case signingUnavailable
  case signingFailed
  case enrollmentStateUnverified
  case timedOut
  case requestAlreadyActive
  case challengeAlreadyConsumed
  case hostNotInstalled
  case transportUnavailable

  public var code: String {
    switch self {
    case .malformedChallenge: "MALFORMED_APPROVAL_CHALLENGE"
    case .challengeExpired: "APPROVAL_CHALLENGE_EXPIRED"
    case .challengeNotYetValid: "APPROVAL_CHALLENGE_NOT_YET_VALID"
    case .reviewCancelled: "APPROVAL_REVIEW_CANCELLED"
    case .authenticationCancelled: "APPROVAL_AUTHENTICATION_CANCELLED"
    case .authenticationUnavailable: "APPROVAL_AUTHENTICATION_UNAVAILABLE"
    case .authenticationFailed: "APPROVAL_AUTHENTICATION_FAILED"
    case .signingUnavailable: "APPROVAL_SIGNING_UNAVAILABLE"
    case .signingFailed: "APPROVAL_SIGNING_FAILED"
    case .enrollmentStateUnverified: "APPROVAL_ENROLLMENT_STATE_UNVERIFIED"
    case .timedOut: "APPROVAL_TIMED_OUT"
    case .requestAlreadyActive: "APPROVAL_REQUEST_ALREADY_ACTIVE"
    case .challengeAlreadyConsumed: "APPROVAL_CHALLENGE_ALREADY_CONSUMED"
    case .hostNotInstalled: "APPROVAL_HOST_NOT_INSTALLED"
    case .transportUnavailable: "APPROVAL_TRANSPORT_UNAVAILABLE"
    }
  }

  public var publicMessage: String {
    switch self {
    case .malformedChallenge:
      "The approval challenge does not meet the closed host contract."
    case .challengeExpired:
      "The approval challenge expired before approval could complete."
    case .challengeNotYetValid:
      "The approval challenge is outside the permitted clock window."
    case .reviewCancelled:
      "The local review was cancelled."
    case .authenticationCancelled:
      "OS authentication was cancelled."
    case .authenticationUnavailable:
      "OS authentication is unavailable on this host."
    case .authenticationFailed:
      "OS authentication did not succeed."
    case .signingUnavailable:
      "The protected receipt-signing key is unavailable."
    case .signingFailed:
      "The protected receipt could not be signed."
    case .enrollmentStateUnverified:
      "Protected-key state could not be verified. Inspect local enrollment state before retrying."
    case .timedOut:
      "The approval request timed out."
    case .requestAlreadyActive:
      "An approval request with this identifier is already active."
    case .challengeAlreadyConsumed:
      "This approval challenge was already consumed by a prior signing attempt."
    case .hostNotInstalled:
      "This compiled foundation has no installed trusted-host configuration."
    case .transportUnavailable:
      "The approved local transport is unavailable."
    }
  }

  /// Accept only an exact pair emitted by this closed host.  XPC clients use
  /// this when decoding a peer response so an arbitrary remote code or
  /// message can never become caller-visible diagnostic text.
  public static func exactPublicFailure(code: String, message: String) -> ApprovalHostError? {
    guard code.lengthOfBytes(using: .utf8) <= 128,
      message.lengthOfBytes(using: .utf8) <= 512
    else { return nil }
    let error: ApprovalHostError
    switch code {
    case "MALFORMED_APPROVAL_CHALLENGE": error = .malformedChallenge
    case "APPROVAL_CHALLENGE_EXPIRED": error = .challengeExpired
    case "APPROVAL_CHALLENGE_NOT_YET_VALID": error = .challengeNotYetValid
    case "APPROVAL_REVIEW_CANCELLED": error = .reviewCancelled
    case "APPROVAL_AUTHENTICATION_CANCELLED": error = .authenticationCancelled
    case "APPROVAL_AUTHENTICATION_UNAVAILABLE": error = .authenticationUnavailable
    case "APPROVAL_AUTHENTICATION_FAILED": error = .authenticationFailed
    case "APPROVAL_SIGNING_UNAVAILABLE": error = .signingUnavailable
    case "APPROVAL_SIGNING_FAILED": error = .signingFailed
    case "APPROVAL_ENROLLMENT_STATE_UNVERIFIED": error = .enrollmentStateUnverified
    case "APPROVAL_TIMED_OUT": error = .timedOut
    case "APPROVAL_REQUEST_ALREADY_ACTIVE": error = .requestAlreadyActive
    case "APPROVAL_CHALLENGE_ALREADY_CONSUMED": error = .challengeAlreadyConsumed
    case "APPROVAL_HOST_NOT_INSTALLED": error = .hostNotInstalled
    case "APPROVAL_TRANSPORT_UNAVAILABLE": error = .transportUnavailable
    default: return nil
    }
    return error.publicMessage == message ? error : nil
  }
}

public struct ApprovalPublicFailure: Codable, Equatable, Sendable {
  public let code: String
  public let message: String

  public init(code: String, message: String) {
    self.code = code
    self.message = message
  }

  public init(_ error: ApprovalHostError) {
    code = error.code
    message = error.publicMessage
  }
}
