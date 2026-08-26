# iOS客户端

当前没有Apple Developer Program账号，因此项目不启用APNs和TestFlight。可以在Mac mini安装Xcode后使用个人Apple Account真机调试。

项目文件由XcodeGen生成：

```bash
brew install xcodegen
cd ios/StockTopicApp
xcodegen generate
open StockTopic.xcodeproj
```

在Xcode的Signing & Capabilities中选择你的Personal Team，然后连接iPhone运行。首次打开在“设置”中填写 `https://stock.bnken.com` 和 `.env` 内的 `APP_API_TOKEN`。Token保存在iPhone Keychain。
