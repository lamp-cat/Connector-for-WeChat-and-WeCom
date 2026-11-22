import Foundation
import Security

// Secrets are accepted/returned only through local inherited pipes, never argv or logs.
func main() -> [String: Any] {
    guard let input = try? JSONSerialization.jsonObject(with: FileHandle.standardInput.readDataToEndOfFile()) as? [String: Any],
          let account = input["account"] as? String,
          account.range(of: "^[a-f0-9]{64}$", options: .regularExpression) != nil else { return ["ok": false] }
    let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: "local.WeComCompanion.database", kSecAttrAccount as String: account]
    if input["op"] as? String == "read" {
        var read = query
        read[kSecReturnData as String] = true
        read[kSecMatchLimit as String] = kSecMatchLimitOne
        var value: CFTypeRef?
        guard SecItemCopyMatching(read as CFDictionary, &value) == errSecSuccess,
              let bytes = value as? Data,
              let keys = try? JSONSerialization.jsonObject(with: bytes) else { return ["ok": true, "keys": [:]] }
        return ["ok": true, "keys": keys]
    }
    guard input["op"] as? String == "write", let keys = input["keys"] as? [String: String],
          keys.allSatisfy({ ["message.db", "session.db", "user.db"].contains($0.key) &&
              ($0.value.isEmpty || $0.value.range(of: "^[a-f0-9]{32}$", options: .regularExpression) != nil) }),
          let bytes = try? JSONSerialization.data(withJSONObject: keys) else { return ["ok": false] }
    let status = SecItemUpdate(query as CFDictionary, [kSecValueData as String: bytes] as CFDictionary)
    if status == errSecSuccess { return ["ok": true] }
    guard status == errSecItemNotFound else { return ["ok": false] }
    var create = query
    create[kSecValueData as String] = bytes
    create[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
    return ["ok": SecItemAdd(create as CFDictionary, nil) == errSecSuccess]
}
let result = main()
if let output = try? JSONSerialization.data(withJSONObject: result) { FileHandle.standardOutput.write(output) }
