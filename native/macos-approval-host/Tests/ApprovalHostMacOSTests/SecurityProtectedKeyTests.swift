import Security
import XCTest

@testable import ApprovalHostMacOS

final class SecurityProtectedKeyTests: XCTestCase {
  /// This test exercises only the pure attribute guard.  It neither queries
  /// nor creates a Keychain item.
  func testRejectsWrongOrAmbiguousKeyAttributesBeforeSigning() {
    let standard: [CFString: Any] = [
      kSecAttrKeyType: kSecAttrKeyTypeECSECPrimeRandom,
      kSecAttrKeySizeInBits: 256,
      kSecAttrKeyClass: kSecAttrKeyClassPrivate,
      kSecAttrCanSign: true,
    ]
    XCTAssertTrue(
      SecurityKeyAttributes.isExactConfiguredPrivateP256(standard, storage: .keychainP256))
    XCTAssertFalse(
      SecurityKeyAttributes.isExactConfiguredPrivateP256(
        [
          kSecAttrKeyType: kSecAttrKeyTypeRSA,
          kSecAttrKeySizeInBits: 256,
          kSecAttrKeyClass: kSecAttrKeyClassPrivate,
          kSecAttrCanSign: true,
        ], storage: .keychainP256))
    XCTAssertFalse(
      SecurityKeyAttributes.isExactConfiguredPrivateP256(
        [
          kSecAttrKeyType: kSecAttrKeyTypeECSECPrimeRandom,
          kSecAttrKeySizeInBits: 384,
          kSecAttrKeyClass: kSecAttrKeyClassPrivate,
          kSecAttrCanSign: true,
        ], storage: .keychainP256))
    XCTAssertFalse(
      SecurityKeyAttributes.isExactConfiguredPrivateP256(
        [
          kSecAttrKeyType: kSecAttrKeyTypeECSECPrimeRandom,
          kSecAttrKeySizeInBits: true,
          kSecAttrKeyClass: kSecAttrKeyClassPrivate,
          kSecAttrCanSign: true,
        ], storage: .keychainP256))
    XCTAssertFalse(
      SecurityKeyAttributes.isExactConfiguredPrivateP256(
        [
          kSecAttrKeyType: kSecAttrKeyTypeECSECPrimeRandom,
          kSecAttrKeySizeInBits: NSNumber(value: 256.5),
          kSecAttrKeyClass: kSecAttrKeyClassPrivate,
          kSecAttrCanSign: true,
        ], storage: .keychainP256))
    XCTAssertFalse(
      SecurityKeyAttributes.isExactConfiguredPrivateP256(standard, storage: .secureEnclaveP256))
    XCTAssertTrue(
      SecurityKeyAttributes.isExactConfiguredPrivateP256(
        [
          kSecAttrKeyType: kSecAttrKeyTypeECSECPrimeRandom,
          kSecAttrKeySizeInBits: 256,
          kSecAttrKeyClass: kSecAttrKeyClassPrivate,
          kSecAttrCanSign: true,
          kSecAttrTokenID: kSecAttrTokenIDSecureEnclave,
        ], storage: .secureEnclaveP256))

    let accessGroup = "ABCDE12345.com.aptsonic.adhelm"
    var grouped = standard
    grouped[kSecAttrAccessGroup] = accessGroup
    XCTAssertTrue(
      SecurityKeyAttributes.isExactConfiguredPrivateP256(
        grouped,
        storage: .keychainP256,
        accessGroup: accessGroup
      ))
    XCTAssertFalse(
      SecurityKeyAttributes.isExactConfiguredPrivateP256(
        grouped,
        storage: .keychainP256,
        accessGroup: "ABCDE12345.com.example.other"
      ))
    XCTAssertFalse(
      SecurityKeyAttributes.isExactConfiguredPrivateP256(
        grouped,
        storage: .keychainP256
      ))
  }
}
