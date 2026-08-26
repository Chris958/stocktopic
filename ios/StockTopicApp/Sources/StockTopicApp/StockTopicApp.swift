import SwiftUI

@main
struct StockTopicApp: App {
    @State private var store = DashboardStore()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environment(store)
        }
    }
}

