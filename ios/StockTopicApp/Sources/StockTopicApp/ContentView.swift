import SwiftUI

struct ContentView: View {
    @Environment(DashboardStore.self) private var store

    var body: some View {
        TabView {
            NavigationStack { ThemesView() }
                .tabItem { Label("题材", systemImage: "square.stack.3d.up") }
            NavigationStack { AlertsView() }
                .tabItem { Label("预警", systemImage: "bell.badge") }
            NavigationStack { SettingsView() }
                .tabItem { Label("设置", systemImage: "gearshape") }
        }
        .task { await store.refresh() }
    }
}

struct ThemesView: View {
    @Environment(DashboardStore.self) private var store

    var body: some View {
        List {
            if let health = store.dashboard?.health {
                Section {
                    HStack {
                        Circle().fill(health.market.realtimeCollectionEnabled ? .green : .secondary).frame(width: 8, height: 8)
                        Text(health.market.realtimeCollectionEnabled ? "盘中采集中" : "\(health.market.session) · 待机")
                        Spacer()
                        Text(health.chinaTime.prefix(16)).foregroundStyle(.secondary)
                    }
                }
            }
            let confirmed = store.dashboard?.themes.filter { $0.status == "confirmed" } ?? []
            let pending = store.dashboard?.themes.filter { $0.status == "pending" } ?? []
            let watching = store.dashboard?.themes.filter { $0.status == "watching" } ?? []
            let rejected = store.dashboard?.themes.filter { $0.status == "rejected" } ?? []
            Section("早期观察") {
                if watching.isEmpty { Text("暂无证据待确认题材").foregroundStyle(.secondary) }
                ForEach(watching) { theme in ThemeRow(theme: theme) }
            }
            Section("正式题材") {
                if confirmed.isEmpty { ContentUnavailableView("暂无正式题材", systemImage: "chart.xyaxis.line") }
                ForEach(confirmed) { theme in ThemeRow(theme: theme) }
            }
            Section("AI准入审查中") {
                if pending.isEmpty { Text("暂无达到四只触板门槛的候选").foregroundStyle(.secondary) }
                ForEach(pending) { theme in ThemeRow(theme: theme) }
            }
            Section("未入池记录") {
                if rejected.isEmpty { Text("暂无未通过记录").foregroundStyle(.secondary) }
                ForEach(rejected) { theme in ThemeRow(theme: theme) }
            }
        }
        .navigationTitle("题材情绪")
        .refreshable { await store.refresh() }
        .overlay { if store.isLoading && store.dashboard == nil { ProgressView() } }
        .alert("连接异常", isPresented: .constant(store.errorMessage != nil)) {
            Button("知道了") { store.errorMessage = nil }
        } message: { Text(store.errorMessage ?? "") }
    }
}

struct ThemeRow: View {
    let theme: Theme
    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack { Text(theme.name).font(.headline); Spacer(); Text(theme.sharedTag).font(.caption).foregroundStyle(.secondary) }
            if let score = theme.score {
                HStack(spacing: 18) {
                    ScoreCell(name: "Heat", value: score.heat, color: .red)
                    ScoreCell(name: "持续", value: score.persistence, color: .green)
                    ScoreCell(name: "风险", value: score.entryRisk, color: .orange)
                }
                Text("\(score.lifecycle) · Day \(score.details.dayNumber)\(score.leaderThemeDivergence == 1 ? " · 龙头—板块背离" : "")")
                    .font(.caption).foregroundStyle(.secondary)
            } else {
                Text(theme.admissionReason ?? "等待新颖性、催化和持续性审查")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Text(theme.members.filter { $0.active == 1 }.map(\.name).joined(separator: " · "))
                .font(.caption).foregroundStyle(.secondary).lineLimit(2)
        }.padding(.vertical, 5)
    }
}

struct ScoreCell: View {
    let name: String
    let value: Double
    let color: Color
    var body: some View { VStack(alignment: .leading) { Text(name).font(.caption2).foregroundStyle(.secondary); Text(value, format: .number.precision(.fractionLength(0))).font(.title3).foregroundStyle(color) } }
}

struct AlertsView: View {
    @Environment(DashboardStore.self) private var store
    var body: some View {
        List(store.dashboard?.alerts ?? []) { item in
            VStack(alignment: .leading, spacing: 5) { Text(item.title); Text(item.body).font(.caption).foregroundStyle(.secondary) }
        }.navigationTitle("预警").refreshable { await store.refresh() }
    }
}

struct SettingsView: View {
    @Environment(DashboardStore.self) private var store
    @State private var url = ""
    @State private var token = ""
    var body: some View {
        Form {
            Section("Mac mini服务") {
                TextField("https://theme-api.example.com", text: $url).textInputAutocapitalization(.never).keyboardType(.URL)
                SecureField("APP_API_TOKEN", text: $token)
                Button("保存并测试") { store.saveSettings(url: url, token: token); Task { await store.refresh() } }
            }
            Section { Text("Token保存在本机Keychain。未开通Apple Developer Program前，重要通知由企业微信承担。") }
        }.navigationTitle("设置").onAppear { url = store.baseURL; token = store.token }
    }
}
