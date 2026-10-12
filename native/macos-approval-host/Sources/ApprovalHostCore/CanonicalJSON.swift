import Foundation

/// The approval protocol has no arbitrary JSON extension point.  This parser
/// accepts only bounded RFC 8259 JSON made of strings, booleans, null, safe
/// integers, arrays, and objects.  It rejects decimal/exponent values because
/// this host's closed approval schemas do not need them and must never silently
/// round an exact budget or version.
public indirect enum CanonicalJSONValue: Equatable, Sendable {
    case object([String: CanonicalJSONValue])
    case array([CanonicalJSONValue])
    case string(String)
    case integer(Int64)
    case bool(Bool)
    case null
}

public struct CanonicalJSONLimits: Equatable, Sendable {
    public let maximumBytes: Int
    public let maximumDepth: Int
    public let maximumContainerItems: Int
    public let maximumStringBytes: Int

    public init(
        maximumBytes: Int = 65_536,
        maximumDepth: Int = 20,
        maximumContainerItems: Int = 256,
        maximumStringBytes: Int = 4_096
    ) {
        self.maximumBytes = maximumBytes
        self.maximumDepth = maximumDepth
        self.maximumContainerItems = maximumContainerItems
        self.maximumStringBytes = maximumStringBytes
    }

    public static let approvalChallenge = CanonicalJSONLimits()
}

public enum CanonicalJSON {
    /// RFC 8785's interoperable integer domain.  The Python canonicalizer used
    /// by the safety kernel rejects values outside this range, so the host must
    /// not approve a value the kernel cannot reproduce.
    public static let maximumSafeInteger: Int64 = 9_007_199_254_740_991

    public static func parseCanonical(
        _ data: Data,
        limits: CanonicalJSONLimits = .approvalChallenge
    ) throws -> CanonicalJSONValue {
        guard !data.isEmpty, data.count <= limits.maximumBytes else {
            throw ApprovalHostError.malformedChallenge
        }
        var parser = Parser(bytes: Array(data), limits: limits)
        let value = try parser.parseDocument()
        let canonical = try encode(value)
        guard canonical == data else {
            throw ApprovalHostError.malformedChallenge
        }
        return value
    }

    public static func encode(_ value: CanonicalJSONValue) throws -> Data {
        var bytes: [UInt8] = []
        try write(value, into: &bytes)
        return Data(bytes)
    }

    public static func sha256Hex(_ data: Data) -> String {
        // CryptoKit is deliberately kept out of the parser.  SHA-256 is
        // implemented through CryptoKit in Receipt.swift, where signing and
        // digest material are handled.
        SHA256Support.hexDigest(data)
    }

    private static func write(_ value: CanonicalJSONValue, into output: inout [UInt8]) throws {
        switch value {
        case let .object(object):
            output.append(0x7B)
            let keys = object.keys.sorted(by: utf16Precedes)
            for (index, key) in keys.enumerated() {
                if index > 0 { output.append(0x2C) }
                try writeString(key, into: &output)
                output.append(0x3A)
                guard let child = object[key] else { throw ApprovalHostError.malformedChallenge }
                try write(child, into: &output)
            }
            output.append(0x7D)
        case let .array(array):
            output.append(0x5B)
            for (index, child) in array.enumerated() {
                if index > 0 { output.append(0x2C) }
                try write(child, into: &output)
            }
            output.append(0x5D)
        case let .string(string):
            try writeString(string, into: &output)
        case let .integer(integer):
            guard integer >= -maximumSafeInteger, integer <= maximumSafeInteger else {
                throw ApprovalHostError.malformedChallenge
            }
            output.append(contentsOf: String(integer).utf8)
        case let .bool(bool):
            output.append(contentsOf: bool ? [0x74, 0x72, 0x75, 0x65] : [0x66, 0x61, 0x6C, 0x73, 0x65])
        case .null:
            output.append(contentsOf: [0x6E, 0x75, 0x6C, 0x6C])
        }
    }

    private static func writeString(_ string: String, into output: inout [UInt8]) throws {
        output.append(0x22)
        for scalar in string.unicodeScalars {
            switch scalar.value {
            case 0x22: output.append(contentsOf: [0x5C, 0x22])
            case 0x5C: output.append(contentsOf: [0x5C, 0x5C])
            case 0x08: output.append(contentsOf: [0x5C, 0x62])
            case 0x09: output.append(contentsOf: [0x5C, 0x74])
            case 0x0A: output.append(contentsOf: [0x5C, 0x6E])
            case 0x0C: output.append(contentsOf: [0x5C, 0x66])
            case 0x0D: output.append(contentsOf: [0x5C, 0x72])
            case 0x00...0x1F:
                let hexadecimal = String(format: "%04x", scalar.value)
                output.append(contentsOf: [0x5C, 0x75])
                output.append(contentsOf: hexadecimal.utf8)
            default:
                output.append(contentsOf: String(scalar).utf8)
            }
        }
        output.append(0x22)
    }

    private static func utf16Precedes(_ left: String, _ right: String) -> Bool {
        left.utf16.lexicographicallyPrecedes(right.utf16)
    }

    private struct Parser {
        let bytes: [UInt8]
        let limits: CanonicalJSONLimits
        var index = 0

        mutating func parseDocument() throws -> CanonicalJSONValue {
            let value = try parseValue(depth: 0)
            guard index == bytes.count else { throw ApprovalHostError.malformedChallenge }
            return value
        }

        mutating func parseValue(depth: Int) throws -> CanonicalJSONValue {
            guard depth <= limits.maximumDepth, index < bytes.count else {
                throw ApprovalHostError.malformedChallenge
            }
            switch bytes[index] {
            case 0x7B: return try parseObject(depth: depth + 1)
            case 0x5B: return try parseArray(depth: depth + 1)
            case 0x22: return .string(try parseString())
            case 0x74: return try parseLiteral([0x74, 0x72, 0x75, 0x65], result: .bool(true))
            case 0x66: return try parseLiteral([0x66, 0x61, 0x6C, 0x73, 0x65], result: .bool(false))
            case 0x6E: return try parseLiteral([0x6E, 0x75, 0x6C, 0x6C], result: .null)
            case 0x2D, 0x30...0x39: return .integer(try parseInteger())
            default: throw ApprovalHostError.malformedChallenge
            }
        }

        mutating func parseObject(depth: Int) throws -> CanonicalJSONValue {
            try consume(0x7B)
            var result: [String: CanonicalJSONValue] = [:]
            if peek(0x7D) {
                index += 1
                return .object(result)
            }
            var members = 0
            while true {
                guard members < limits.maximumContainerItems, peek(0x22) else {
                    throw ApprovalHostError.malformedChallenge
                }
                let key = try parseString()
                guard result[key] == nil else { throw ApprovalHostError.malformedChallenge }
                try consume(0x3A)
                result[key] = try parseValue(depth: depth)
                members += 1
                if peek(0x7D) {
                    index += 1
                    return .object(result)
                }
                try consume(0x2C)
            }
        }

        mutating func parseArray(depth: Int) throws -> CanonicalJSONValue {
            try consume(0x5B)
            var result: [CanonicalJSONValue] = []
            if peek(0x5D) {
                index += 1
                return .array(result)
            }
            while true {
                guard result.count < limits.maximumContainerItems else {
                    throw ApprovalHostError.malformedChallenge
                }
                result.append(try parseValue(depth: depth))
                if peek(0x5D) {
                    index += 1
                    return .array(result)
                }
                try consume(0x2C)
            }
        }

        mutating func parseLiteral(_ literal: [UInt8], result: CanonicalJSONValue) throws -> CanonicalJSONValue {
            guard index + literal.count <= bytes.count,
                  Array(bytes[index..<(index + literal.count)]) == literal
            else { throw ApprovalHostError.malformedChallenge }
            index += literal.count
            return result
        }

        mutating func parseInteger() throws -> Int64 {
            let start = index
            if peek(0x2D) { index += 1 }
            guard index < bytes.count else { throw ApprovalHostError.malformedChallenge }
            if peek(0x30) {
                index += 1
                guard index == bytes.count || !(0x30...0x39).contains(bytes[index]) else {
                    throw ApprovalHostError.malformedChallenge
                }
            } else {
                guard (0x31...0x39).contains(bytes[index]) else { throw ApprovalHostError.malformedChallenge }
                index += 1
                while index < bytes.count, (0x30...0x39).contains(bytes[index]) { index += 1 }
            }
            guard index == bytes.count || (bytes[index] != 0x2E && bytes[index] != 0x45 && bytes[index] != 0x65),
                  let integer = Int64(String(bytes: bytes[start..<index], encoding: .ascii) ?? ""),
                  integer >= -CanonicalJSON.maximumSafeInteger,
                  integer <= CanonicalJSON.maximumSafeInteger
            else { throw ApprovalHostError.malformedChallenge }
            return integer
        }

        mutating func parseString() throws -> String {
            try consume(0x22)
            var result = String.UnicodeScalarView()
            var encodedLength = 0
            while index < bytes.count {
                let byte = bytes[index]
                index += 1
                switch byte {
                case 0x22:
                    let string = String(result)
                    guard string.lengthOfBytes(using: .utf8) <= limits.maximumStringBytes else {
                        throw ApprovalHostError.malformedChallenge
                    }
                    return string
                case 0x5C:
                    guard index < bytes.count else { throw ApprovalHostError.malformedChallenge }
                    let escape = bytes[index]
                    index += 1
                    switch escape {
                    case 0x22: result.append("\"")
                    case 0x5C: result.append("\\")
                    case 0x2F: result.append("/")
                    case 0x62: result.append("\u{08}")
                    case 0x66: result.append("\u{0C}")
                    case 0x6E: result.append("\n")
                    case 0x72: result.append("\r")
                    case 0x74: result.append("\t")
                    case 0x75:
                        let scalar = try parseEscapedUnicodeScalar()
                        result.append(scalar)
                    default: throw ApprovalHostError.malformedChallenge
                    }
                case 0x00...0x1F:
                    throw ApprovalHostError.malformedChallenge
                case 0x20...0x7F:
                    let scalar = UnicodeScalar(byte)
                    result.append(scalar)
                default:
                    let length = utf8SequenceLength(first: byte)
                    guard index - 1 + length <= bytes.count,
                          let scalarString = String(bytes: bytes[(index - 1)..<(index - 1 + length)], encoding: .utf8),
                          scalarString.unicodeScalars.count == 1,
                          let scalar = scalarString.unicodeScalars.first
                    else { throw ApprovalHostError.malformedChallenge }
                    index += length - 1
                    result.append(scalar)
                }
                encodedLength += 1
                guard encodedLength <= limits.maximumStringBytes else { throw ApprovalHostError.malformedChallenge }
            }
            throw ApprovalHostError.malformedChallenge
        }

        mutating func parseEscapedUnicodeScalar() throws -> UnicodeScalar {
            let first = try parseHexQuad()
            if (0xD800...0xDBFF).contains(first) {
                guard index + 2 <= bytes.count, bytes[index] == 0x5C, bytes[index + 1] == 0x75 else {
                    throw ApprovalHostError.malformedChallenge
                }
                index += 2
                let second = try parseHexQuad()
                guard (0xDC00...0xDFFF).contains(second) else { throw ApprovalHostError.malformedChallenge }
                let combined = 0x10000 + ((first - 0xD800) << 10) + (second - 0xDC00)
                guard let scalar = UnicodeScalar(combined) else { throw ApprovalHostError.malformedChallenge }
                return scalar
            }
            guard !(0xDC00...0xDFFF).contains(first), let scalar = UnicodeScalar(first) else {
                throw ApprovalHostError.malformedChallenge
            }
            return scalar
        }

        mutating func parseHexQuad() throws -> UInt32 {
            guard index + 4 <= bytes.count else { throw ApprovalHostError.malformedChallenge }
            var result: UInt32 = 0
            for _ in 0..<4 {
                let byte = bytes[index]
                index += 1
                let digit: UInt32
                switch byte {
                case 0x30...0x39: digit = UInt32(byte - 0x30)
                case 0x41...0x46: digit = UInt32(byte - 0x41 + 10)
                case 0x61...0x66: digit = UInt32(byte - 0x61 + 10)
                default: throw ApprovalHostError.malformedChallenge
                }
                result = result * 16 + digit
            }
            return result
        }

        func utf8SequenceLength(first: UInt8) -> Int {
            switch first {
            case 0xC2...0xDF: 2
            case 0xE0...0xEF: 3
            case 0xF0...0xF4: 4
            default: 0
            }
        }

        mutating func consume(_ expected: UInt8) throws {
            guard peek(expected) else { throw ApprovalHostError.malformedChallenge }
            index += 1
        }

        func peek(_ expected: UInt8) -> Bool {
            index < bytes.count && bytes[index] == expected
        }
    }
}
