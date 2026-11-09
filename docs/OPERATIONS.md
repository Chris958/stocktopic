# Mac mini运维手册

## 常用命令

```bash
./scripts/doctor.sh
launchctl kickstart -k gui/$(id -u)/com.chris958.stocktopic
tail -f logs/stocktopic.err.log
curl http://127.0.0.1:8765/health
```

## 状态解释

- `realtime_collection_enabled=false`：休市、午休、交易日历未知、不在有效窗口，或已通过
  `TUSHARE_RT_K_ENABLED=false` 暂停实时行情。
- `calendar_unknown_fail_closed`：交易日历同步失败，系统主动停止实时采集。
- `coverage abnormal`：Tushare返回的有效沪深主板行情低于当前股票池80%（且至少2000只），本周期不参与计算。
- `data_stale`：全市场累计成交量和成交额没有变化，本周期信号熔断。
- `latest_feishu_error`：最近一次飞书机器人失败的错误码及处理建议；`last_feishu_success_at`记录最近成功发送时间。旧`latest_wecom_error`仅为历史兼容字段。
- `latest_discovery_backfill`：最近一次启动、收盘或手工两交易日发现回补的时间和结果。
- `discovery_input_coverage`：当日KPL涨停/炸板源状态；`empty_strong_feed`或`unavailable`意味着输入覆盖需检查，即使行情采集任务成功。盘中主板可用本地行情+官方涨停价兜底；缺少官方涨停价不能凑数。
- `daily_limit_readiness`：当日有效官方涨停价占当前主板股票池的覆盖；不足95%时保持`incomplete`并每5分钟重试。该比例仅判断源完整性，不改变题材四票准入门槛。
- `market_signal_coverage`：实际主板行情、具备涨停价的行情数量与分市场强势股数量；主板数据不足时采集任务为`degraded`，页面提示“主板发现数据待补”。涨停价齐全但主板行情缺失仍不能算完整。
- KPL `pending_publication`：官方榜单次日06:00后更新前一交易日数据，当日不轮询榜单。次日06:10后独立补录最近两个已结束交易日，失败每30分钟重试，含周六；记录在`completed_sources:日期`及`reconcile_completed_sources`任务。该补录只修复证据，不调用历史AI、不补发机会通知。
- `/api/v1/dashboard.market_cycles`：最多120个本地市场周期，保留最近10个观察日摘要。页面默认显示8个，可展开全部方向。`event_admission`单独标示准入，“数据待补”不能解释为退潮；归档方向再发酵可验证后重新入池。详见[周期修复说明](robot-cycle-repair.md)。
- 正式题材按“大方向＋催化”维护，例如机器人是池名称、特斯拉产品进展是催化。归档继续跟踪，同一批旧数据不会立即撤销归档；次日或当日新增成员/封板形成四股再共振并验证通过，复用原ID恢复。每天五分钟采集后即可更新已存在方向的强势成员，不必等新建四票候选。详见[方向池说明](direction-pool.md)。
- 本地`early_observation`：2–3股共振仅在网页展示，不创建预警、不发机器人通知。AI审查后的`early_watch`、正式题材、机会、风险和数据故障仍推送飞书。
- `ai_usage.policy.discovery_research`记录当前研究预算；`negative_cache_recheck_minutes`与`negative_cache_max_extra_reviews_per_input`记录负面复核频率和单输入上限。`service_runs`的`discovery_ai_recheck`区分新催化复核与接口失败重试。
- `admission_policy`：当前生效的共同事件4只强势股票、沪深主板涨停/炸板、两交易日回补、早期观察/正式题材、60交易日（约90自然日）和3日/30%准入口径。
- `test_pool`：测试票池记录数量和最近一次Tushare正式日线同步任务。
- `integrations.tushare_transport`：当前数据通道，`mcp`表示使用中转，`direct`表示Token直连；
  健康页不会返回MCP URL。
- `ai_usage`：近24小时和7天的AI调用、输入/缓存/输出/推理Token及联网搜索次数；
  `usage_complete=false`表示中转服务没有为全部请求返回usage，不能把当前总数当作完整账单。

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

该任务调用已到发布时间的日度开盘啦接口，并用Tushare `daily` 的最高价和官方涨停价重建主板触板证据；不调用实时 `rt_k`。主板日线补录缺少盘中时间和涨停原因时保持未知，不覆盖已有盘中/KPL记录。运行结果写入 `service_runs` 和 `/health` 的 `latest_discovery_backfill`。手工发现回补仍会运行既有AI准入流程，与上述仅修复数据源的次日任务不同。

测试票池在交易时段复用已有 `rt_k` 五分钟采集确认T+1买入、T+2卖出并更新盘中涨跌；服务启动、
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

需要重新配置OpenAI或飞书机器人时，推荐使用交互脚本，秘密字段输入时不会回显，直接回车会
保留原值：

```bash
./scripts/configure_integrations.sh
```

生产环境使用官方Token直连时，在交互脚本中填写 `TUSHARE_TOKEN`，并在MCP URL提示处输入
`-` 清空中转配置。没有 `rt_k` 权限时，对“启用rt_k盘中实时行情”回答 `n`。保存后脚本会
重启服务。随后运行：

```bash
./scripts/doctor.sh
```

确认 `TUSHARE_PROVIDER` 返回 `status=ok` 且 `transport=direct`，健康页显示
`tushare_rt_k_enabled=false`。关闭 `rt_k` 不会关闭 `moneyflow`、`moneyflow_dc`、
`moneyflow_ths` 盘后主力资金任务。

资金模块复用同一Tushare数据通道；`moneyflow`、`moneyflow_dc`、`moneyflow_ths`需分别具有
权限。调度与迁移见 [资金流说明](FUND_FLOW.md)。

通知已统一切换飞书，旧`WECOM_BOT_WEBHOOK`不再参与发送，也不会自动回退。
在Mac上仅更新通知配置并重启，其他数据源、AI及管理账号保持原值：

```bash
./scripts/configure_integrations.sh --feishu-only
```

Webhook采用隐藏输入，写入权限为600的`.env`。若机器人未开启签名校验，签名密钥提示直接回车。
在网页“预警”页点击“测试飞书机器人”验证实际发送。配置项、错误码及历史送达兼容说明见
[飞书通知说明](NOTIFICATIONS.md)。

若旧版本出现 `CERTIFICATE_VERIFY_FAILED`，先更新项目并重新运行安装脚本。系统统一使用随项目
安装的 `certifi` 根证书库，并保持主机名与证书链校验开启；不要使用关闭SSL校验的代码绕过。

使用兼容OpenAI协议的服务时，至少确认以下三项：

```dotenv
OPENAI_API_KEY=你的Key
OPENAI_BASE_URL=https://服务商地址/v1
OPENAI_MODEL=gpt-5.6-sol
```

所有AI任务固定使用 `gpt-5.6-sol`，不再按催化、准入、语义聚类分别配置模型。旧版 `.env` 中的任务级模型变量在升级安装时会统一迁移为 `gpt-5.6-sol`，运行时不参与模型选择。

`OPENAI_BASE_URL` 推荐填写到 `/v1`，系统会自动追加 `/responses`；填写完整
`/v1/responses` 也可以。修改后执行：

```bash
launchctl kickstart -k gui/$(id -u)/com.chris958.stocktopic
./scripts/doctor.sh
```

官方和中转均优先请求完整控制参数。不兼容时回退到保留输出上限的模式；若中转也拒绝
`max_output_tokens`，请求失败，不发送无上限请求。读取超时不立即重发，以免重复计费。
同一天、同一准入证据最多请求3次，重试至少间隔30/60分钟；新增证据可立即送审。
如果服务升级或重启时正处于AI准入请求，启动时会把遗留的`analyzing`状态恢复为
`awaiting_ai`并按剩余预算重试；运行期间超过15分钟的孤立`analyzing`状态也会由看门狗重新调度。
`/health.latest_ai_recovery`会记录最近一次启动恢复数量。

AI任务用量、预算上限、已实施降耗和统一模型配置见[AI Token用量说明](AI_TOKEN_USAGE.md)。

正式关注名单的计算状态见 metadata 中的 `direction_member_scope`。
`applied` 同时记录参与计算的五个交易日及停用关系数；
`incomplete_coverage` 表示交易日历或强势行情覆盖不足，暂缓移出。
题材详情的 `?view=focus` 只加载活跃成员；默认接口保留历史关系供复盘。
同方向的已确认池启动时合并，原记录保留合并目标；光伏与绿色电力共用一个方向 ID。


## 0.12.8 题材详情性能与强度排序

题材首次展开使用 `/api/v1/themes/<ID>?view=core`，先返回活跃成员行情；
资金与催化/准入资料分别请求 `/<ID>/fund-flows`、`/<ID>/research`。
某个补充模块失败会单独显示重试，行情继续可用。原 full/focus 接口保留完整结果。
已完成的详情在当前浏览器内缓存30秒（最多24个），每分钟刷新仍强制更新；
修改题材后失效缓存，切换题材取消旧请求，退出登录清空缓存。

日志 `theme_detail_timing` 包含 path、request_id、成员数、响应字节及分段毫秒：
`detail_queue` 为进入路由后的线程等待，`board_history` 为涨停/异动查询，
`member_quotes` 为行情和指标，`research` 为催化与审查，`fund_flows` 为资金，
`json_response` 为编码，`headers_ready/app_total` 为应用处理与发送阶段。
浏览器 Network 的 Server-Timing 可直接查看各段。应用进入前的连接排队和网络下载
需要与客户端首字节/总耗时一起判断，不能仅凭数据库脚本用时推断。

本版新增四个幂等索引，无新表、无业务数据迁移。首次启动需为历史数据建立索引；
后续启动复用。涨停历史每股最多读取8条，资金每个对象只解析盘中/盘后最新各一份。
新增索引不会清理历史记录。上线响应时间与 Token 下降比例仍须按真实数据观察。
