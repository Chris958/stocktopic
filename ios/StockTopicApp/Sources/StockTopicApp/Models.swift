import Foundation

struct DashboardResponse: Decodable {
    let health: Health
    let themes: [Theme]
    let anomalies: [Anomaly]
    let alerts: [AlertItem]
}

struct Health: Decodable {
    let chinaTime: String
    let universeCount: Int
    let market: Market
    let latestQuoteRun: ServiceRun?

    struct Market: Decodable {
        let isOpenDay: Bool?
        let session: String
        let realtimeCollectionEnabled: Bool
        let reason: String
    }
}

struct ServiceRun: Decodable {
    let startedAt: String
    let status: String
    let rowCount: Int
    let detail: String?
}

struct Theme: Decodable, Identifiable {
    let id: Int
    let provisionalName: String
    let suggestedName: String?
    let finalName: String?
    let sharedTag: String
    let status: String
    let discoveryReason: String
    let day1Date: String
    let members: [ThemeMember]
    let score: ThemeScore?

    var name: String { finalName ?? suggestedName ?? provisionalName }
}

struct ThemeMember: Decodable, Identifiable {
    let code: String
    let name: String
    let active: Int
    let role: String?
    var id: String { code }
}

struct ThemeScore: Decodable {
    let heat: Double
    let persistence: Double
    let entryRisk: Double
    let lifecycle: String
    let confidence: Double
    let leaderCode: String?
    let leaderThemeDivergence: Int
    let details: ScoreDetails
}

struct ScoreDetails: Decodable {
    let dayNumber: Int
    let averagePct: Double
    let strongCount: Int
    let negativeCount: Int
}

struct Anomaly: Decodable, Identifiable {
    let id: Int
    let capturedAt: String
    let code: String
    let name: String
    let direction: String
    let severity: Double
    let pctChange: Double
    let change5m: Double
    let reasons: [String]
}

struct AlertItem: Decodable, Identifiable {
    let id: Int
    let createdAt: String
    let category: String
    let severity: String
    let title: String
    let body: String
}

extension JSONDecoder {
    static let stockTopic: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }()
}
