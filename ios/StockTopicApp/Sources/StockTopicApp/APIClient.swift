import Foundation
import Observation
import Security

enum APIError: LocalizedError {
    case invalidURL
    case unauthorized
    case server(Int)
    case invalidResponse

    var errorDescription: String? {
        switch self {
        case .invalidURL: "服务地址不正确"
        case .unauthorized: "App API Token无效"
        case .server(let code): "服务器错误（\(code)）"
        case .invalidResponse: "服务器返回无法识别的数据"
        }
    }
}

struct APIClient {
    let baseURL: String
    let token: String

    func dashboard() async throws -> DashboardResponse {
        guard let url = URL(string: baseURL.trimmingCharacters(in: CharacterSet(charactersIn: "/")) + "/api/v1/dashboard") else {
            throw APIError.invalidURL
        }
        var request = URLRequest(url: url)
        request.timeoutInterval = 15
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw APIError.invalidResponse }
        if http.statusCode == 401 { throw APIError.unauthorized }
        guard (200..<300).contains(http.statusCode) else { throw APIError.server(http.statusCode) }
        do { return try JSONDecoder.stockTopic.decode(DashboardResponse.self, from: data) }
        catch { throw APIError.invalidResponse }
    }
}

@MainActor
@Observable
final class DashboardStore {
    var dashboard: DashboardResponse?
    var isLoading = false
    var errorMessage: String?
    var baseURL = UserDefaults.standard.string(forKey: "baseURL") ?? "http://127.0.0.1:8765"
    var token = KeychainStore.read(key: "appAPIToken") ?? ""

    func refresh() async {
        guard !token.isEmpty else {
            errorMessage = "请先在设置中填写App API Token"
            return
        }
        isLoading = true
        defer { isLoading = false }
        do {
            dashboard = try await APIClient(baseURL: baseURL, token: token).dashboard()
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func saveSettings(url: String, token: String) {
        baseURL = url
        self.token = token
        UserDefaults.standard.set(url, forKey: "baseURL")
        KeychainStore.save(token, key: "appAPIToken")
    }
}

enum KeychainStore {
    static func save(_ value: String, key: String) {
        let data = Data(value.utf8)
        SecItemDelete([kSecClass: kSecClassGenericPassword, kSecAttrAccount: key] as CFDictionary)
        SecItemAdd([
            kSecClass: kSecClassGenericPassword,
            kSecAttrAccount: key,
            kSecValueData: data,
            kSecAttrAccessible: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        ] as CFDictionary, nil)
    }

    static func read(key: String) -> String? {
        var result: CFTypeRef?
        let status = SecItemCopyMatching([
            kSecClass: kSecClassGenericPassword,
            kSecAttrAccount: key,
            kSecReturnData: true,
            kSecMatchLimit: kSecMatchLimitOne
        ] as CFDictionary, &result)
        guard status == errSecSuccess, let data = result as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }
}

