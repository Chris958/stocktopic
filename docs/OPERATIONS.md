# Mac mini运维手册

## 常用命令

```bash
./scripts/doctor.sh
launchctl kickstart -k gui/$(id -u)/com.chris958.stocktopic
tail -f logs/stocktopic.err.log
curl http://127.0.0.1:8765/health
```

## 状态解释

- `realtime_collection_enabled=false`：休市、午休、交易日历未知或不在有效窗口。
- `calendar_unknown_fail_closed`：交易日历同步失败，系统主动停止实时采集。
- `coverage abnormal`：Tushare返回的有效主板股票不足2000只，本周期不参与计算。
- `data_stale`：全市场累计成交量和成交额没有变化，本周期信号熔断。
- `latest_wecom_error`：最近一次企业微信失败的阶段、errcode和处理建议；空字符串表示最近一次发送成功。
- `latest_discovery_backfill`：最近一次启动、收盘或手工两交易日发现回补的时间和结果。
- `admission_policy`：当前生效的共同事件4只触板、两交易日回补、早期观察/正式题材、60交易日和3日/30%准入口径。
- `test_pool`：测试票池记录数量和最近一次Tushare正式日线同步任务。

## 更新

在项目目录拉取新版本后重新运行安装脚本。脚本复用现有 `.env` 和数据库，不会覆盖凭据或历史数据。

```bash
git pull --ff-only
./scripts/install_macos.sh
```

升级到V4时，安装脚本会把旧版 `MAXIMUM_CANDIDATES_PER_RUN=4` 迁移为 `0`。这不是放宽四票门槛，而是取消候选审查记录的数量截断。

服务重启后会在后台自动回补最近2个交易日。也可以从已登录页面/API手工触发一次：

```bash
curl -u '你的管理用户名' \
  -H 'X-StockTopic-Request: 1' \
  -X POST http://127.0.0.1:8765/api/v1/admin/backfill-discovery
```

`curl` 会交互询问管理密码，不要把密码直接写进命令历史。

该任务只调用日度开盘啦接口，不调用实时 `rt_k`。运行结果写入 `service_runs` 和 `/health` 的 `latest_discovery_backfill`。

测试票池在交易时段复用已有 `rt_k` 五分钟采集确认T+1买入并更新持仓涨跌；服务启动、
08:35和17:10检查正式日线与待结算记录。手工立即回补和结算可执行：

```bash
curl -u '你的管理用户名' \
  -H 'X-StockTopic-Request: 1' \
  -X POST http://127.0.0.1:8765/api/v1/admin/refresh-test-pool
```

该任务调用Tushare `daily`正式日线，不调用 `rt_k`。若出现 `sync_daily_prices` 数据故障，先确认Token具备日线接口权限，并检查返回覆盖是否达到2000只；失败日期不会被标记完成，下一次调度会继续回补。

## 备份与归档

每个交易日收盘后生成SQLite在线备份，保留最近14份。超过120日的全量5分钟快照压缩为 `data/archive/quotes-YYYY-MM-DD.jsonl.gz`，异动、题材、评分、梯队和预警持续保留在数据库。

## Secret轮换

停止服务，编辑 `.env`，再重新启动。不要把 `.env` 内容发到聊天、Issue或GitHub。

需要重新配置OpenAI或企业微信时，推荐使用交互脚本，秘密字段输入时不会回显，直接回车会
保留原值：

```bash
./scripts/configure_integrations.sh
```

企业微信自建应用需要 `CorpID`、`AgentID`、应用 `Secret` 和接收成员的 `UserID`。同时确认：

1. 接收成员位于该自建应用的可见范围内；
2. 应用管理页如果要求“企业可信IP”，已加入Mac mini当前公网出口IPv4；
3. 修改配置并重启后，在网页“预警”页点击“测试企业微信”；
4. 若家庭宽带公网IP发生变化，需要同步更新企业可信IP。

网页测试若返回企业微信错误，系统会区分“获取Token”和“发送消息”，保留上游 `errcode`、
记录最近成功/失败状态并隐藏 `access_token`。网络错误和企业微信5xx会短暂重试，配置类错误不会无效重试。常见排查顺序：

1. 可信IP或出口IP；
2. CorpID和应用Secret是否属于同一企业与同一应用；
3. AgentID是否为该自建应用的数字ID；
4. UserID是否为通讯录账号而不是姓名/手机号；
5. 接收成员是否位于应用可见范围。

常见错误：`60020`为企业可信IP，`40013`为CorpID，`40001`为Secret，`40003`为UserID，
`81013`为应用可见范围。HTTP 502只是StockTopic对上游发送失败的包装，具体原因以页面中的
`errcode`为准。

若旧版本出现 `CERTIFICATE_VERIFY_FAILED`，先更新项目并重新运行安装脚本。系统统一使用随项目
安装的 `certifi` 根证书库，并保持主机名与证书链校验开启；不要使用关闭SSL校验的代码绕过。

使用兼容OpenAI协议的服务时，至少确认以下三项：

```dotenv
OPENAI_API_KEY=你的Key
OPENAI_BASE_URL=https://服务商地址/v1
OPENAI_MODEL=服务商支持的模型名
```

`OPENAI_BASE_URL` 推荐填写到 `/v1`，系统会自动追加 `/responses`；填写完整
`/v1/responses` 也可以。修改后执行：

```bash
launchctl kickstart -k gui/$(id -u)/com.chris958.stocktopic
./scripts/doctor.sh
```

AI请求遇到DNS瞬断、连接超时、HTTP 429或上游5xx时，会按1秒、2秒间隔最多尝试3次。
若仍失败，候选保持在“AI分析失败”状态，后台每30分钟重新审查，不需要重新创建候选。
