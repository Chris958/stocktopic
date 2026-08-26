import XCTest
@testable import StockTopic

final class StockTopicAppTests: XCTestCase {
    func testAPIErrorMessagesExist() {
        XCTAssertNotNil(APIError.unauthorized.errorDescription)
    }
}

