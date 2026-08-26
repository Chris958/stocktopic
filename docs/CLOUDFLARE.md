# stock.bnken.com 接入

StockTopic默认只监听 `127.0.0.1:8765`。公网Web App和后续iOS客户端统一使用：

```text
https://stock.bnken.com
```

## Cloudflare Tunnel

不要开放Mac mini路由器端口。建议为StockTopic建立独立Tunnel；如果复用已有Tunnel，
也必须使用独立的Published application路由，不要复用Futu API路径。

在Cloudflare控制台进入：

```text
Networking → Tunnels → 选择Tunnel → Routes
→ Add route → Published application
```

填写：

| 配置 | 内容 |
|---|---|
| Subdomain | `stock` |
| Domain | `bnken.com` |
| Service URL | `http://127.0.0.1:8765` |

保存后等待Tunnel显示Healthy。当前Web App使用StockTopic自身的会话登录；不要在同一主机名
上叠加需要网页登录的Cloudflare Access，否则后续iOS API请求会被Access登录页拦截。

如果本地 `http://127.0.0.1:8765` 正常而公网返回502，优先确认Service URL使用的是
`127.0.0.1`而不是 `localhost`。如果 `cloudflared` 运行在Docker容器中，容器内的
`127.0.0.1`不是Mac主机，此时应改用 `http://host.docker.internal:8765`，或把
`cloudflared` 直接作为Mac服务运行。

建议在Cloudflare中为 `/api/*` 增加限速规则，例如单个IP每分钟不超过60次；不要缓存
`/api/*`，静态文件 `/static/*` 可以使用Cloudflare默认缓存策略。

## 验收

```bash
curl --fail https://stock.bnken.com/health
curl -i https://stock.bnken.com/api/v1/dashboard
```

第一条应返回JSON健康状态，第二条在未登录时必须返回 `401 Unauthorized`。

浏览器打开 `https://stock.bnken.com` 后，使用Mac mini项目 `.env` 中配置的
`ADMIN_USERNAME` 和 `ADMIN_PASSWORD` 登录。密码经过HTTPS传输，只以Basic授权头形式
保存在当前浏览器的 `sessionStorage`；关闭标签页后清除。API响应禁止浏览器缓存。

## iPhone添加到主屏幕

1. 使用Safari打开 `https://stock.bnken.com`；
2. 登录并确认数据正常；
3. 点击分享；
4. 选择“添加到主屏幕”；
5. 从桌面打开“题材情绪”。

Web App在独立窗口运行，但实时推送仍由企业微信承担。

## 上线检查

1. `https://stock.bnken.com/health` 可访问；
2. 未授权访问 `/api/v1/dashboard` 返回401；
3. 正确用户名/密码可以读取数据并完成一次候选题材确认；
4. 错误密码不会进入页面；
5. iPhone Safari和添加到主屏幕两种模式均可登录；
6. Tunnel断开时页面明确显示错误，不把缓存伪装成实时数据；
7. Mac mini重启后StockTopic LaunchAgent和Tunnel均自动恢复。
