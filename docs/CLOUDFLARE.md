# stock.bnken.com 接入

StockTopic默认只监听 `127.0.0.1:8765`。公网Web App和后续iOS客户端统一使用：

```text
https://stock.bnken.com
```

## Cloudflare Tunnel

不要开放Mac mini路由器端口。如果同一台Mac mini已经有为 `futu-api.bnken.com` 服务的
`cloudflared` 系统服务，直接复用这个Tunnel并增加第二条Published application路由。
不要在同一台Mac上再次执行 `cloudflared service install`；一台主机只需要一个服务，
同一Tunnel可以发布多个hostname和origin。

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

保留原有 `futu-api.bnken.com` 路由不变；新增的是：

```text
stock.bnken.com → http://127.0.0.1:8765
```

保存后等待Tunnel显示Healthy。当前Web App使用StockTopic自身的会话登录；不要在同一主机名
上叠加需要网页登录的Cloudflare Access，否则后续iOS API请求会被Access登录页拦截。

如果本地 `http://127.0.0.1:8765` 正常而公网返回502，优先确认Service URL使用的是
`127.0.0.1`而不是 `localhost`。如果 `cloudflared` 运行在Docker容器中，容器内的
`127.0.0.1`不是Mac主机，此时应改用 `http://host.docker.internal:8765`，或把
`cloudflared` 直接作为Mac服务运行。

## Tunnel Token泄露或轮换

Tunnel Token允许持有者启动该Tunnel的连接器，不能写入文档、聊天或GitHub。Token一旦泄露，
在Cloudflare控制台进入 `Networking → Tunnels → 选择对应Tunnel → Overview`，执行
`Rotate token`/`Refresh token`。如果泄露的是刚创建且从未成功安装的冗余Tunnel，可以先确认
它不是 `futu-api.bnken.com` 正在使用的Tunnel，再删除该冗余Tunnel。

只有当正在运行的Tunnel本身完成Token轮换后，才需要在维护窗口执行：

```bash
sudo cloudflared service uninstall
sudo cloudflared service install <NEW_TOKEN>
```

不要在未确认Tunnel身份时卸载现有服务，否则会同时中断 `futu-api.bnken.com`。

建议在Cloudflare中为 `/api/*` 增加限速规则，例如单个IP每分钟不超过60次；不要缓存
`/api/*`，静态文件 `/static/*` 可以使用Cloudflare默认缓存策略。

## 验收

```bash
curl --fail https://stock.bnken.com/health
curl -i https://stock.bnken.com/api/v1/dashboard
```

第一条应返回JSON健康状态，第二条在未登录时必须返回 `401 Unauthorized`。

浏览器打开 `https://stock.bnken.com` 后，使用Mac mini项目 `.env` 中配置的
`ADMIN_USERNAME` 和 `ADMIN_PASSWORD` 登录。密码只在登录请求中经过HTTPS传输，验证后由
服务端签发30天有效的签名会话Cookie；Cookie启用 `HttpOnly`、`Secure` 和
`SameSite=Strict`，前端脚本不能读取原始密码或会话值。主动退出、会话过期，或管理员密码/
`APP_API_TOKEN` 变更后需要重新登录。API响应禁止浏览器缓存。

## iPhone添加到主屏幕

1. 使用Safari打开 `https://stock.bnken.com`；
2. 登录并确认数据正常；
3. 点击分享；
4. 选择“添加到主屏幕”；
5. 从桌面打开“题材情绪”。

Web App在独立窗口运行，实时推送由企业微信群机器人Webhook承担。

## 上线检查

1. `https://stock.bnken.com/health` 可访问；
2. 未授权访问 `/api/v1/dashboard` 返回401；
3. 正确用户名/密码可以读取重点题材、置顶或归档题材，并测试企业微信群机器人；
4. 错误密码不会进入页面；
5. iPhone Safari和添加到主屏幕两种模式均可登录；
6. Tunnel断开时页面明确显示错误，不把缓存伪装成实时数据；
7. Mac mini重启后StockTopic LaunchAgent和Tunnel均自动恢复。
