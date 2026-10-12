import CryptoKit
import Foundation

enum SHA256Support {
    static func hexDigest(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    static func digest(_ data: Data) -> Data {
        Data(SHA256.hash(data: data))
    }
}
