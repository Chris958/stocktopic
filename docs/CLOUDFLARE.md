# iPhone外网访问

本地MVP默认只监听 `127.0.0.1:8765`。需要在外网使用iOS App时，建议为本系统创建独立Cloudflare Tunnel主机名，例如 `theme-api.bnken.com`，转发到 `http://127.0.0.1:8765`。

不要开放Mac mini路由器端口。Tunnel应设置为独立入口，不复用Futu API的路径与鉴权。公网请求仍必须携带本系统生成的 `APP_API_TOKEN`；管理网页使用HTTP Basic认证，并建议在Cloudflare端再增加Access单用户策略。

上线前必须验证：

1. `https://theme-api.bnken.com/health` 可访问；
2. 未授权访问 `/api/v1/dashboard` 返回401；
3. Bearer Token可以读取数据；
4. Tunnel断开时iOS明确显示离线，不显示缓存为实时数据；
5. Mac mini恢复后LaunchAgent和Tunnel均自动连接。
