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
- `coverage abnormal`：Tushare返回的有效主板与创业板行情低于当前股票池80%（且至少2000只），本周期不参与计算。
- `data_stale`：全市场累计成交量和成交额没有变化，本周期信号熔断。
- `latest_wecom_error`：最近一次企业微信群机器人失败的阶段、errcode和处理建议；空字符串表示最近一次发送成功。
- `latest_discovery_backfill`：最近一次启动、收盘或手工两交易日发现回补的时间和结果。
- `admission_policy`：当前生效的共同事件4只强势股票、主板涨停/炸板与创业板涨幅超过10%、两交易日回补、早期观察/正式题材、60交易日（约90自然日）和3日/30%准入口径。
- `test_pool`：测试票池记录数量和最近一次Tushare正式日线同步任务。
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

该任务调用日度开盘啦接口，并用Tushare `daily` 的当日最高价重建创业板涨幅超过10%的强势信号；不调用实时 `rt_k`。运行结果写入 `service_runs` 和 `/health` 的 `latest_discovery_backfill`。

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

需要重新配置OpenAI或企业微信群机器人时，推荐使用交互脚本，秘密字段输入时不会回显，直接回车会
保留原值：

```bash
./scripts/configure_integrations.sh
```

同一脚本也用于配置猫爪数据API Key。Key只保存在本机`.env`：

```dotenv
NUMCAT_API_KEY=你的猫爪数据Key
```

不要把Key粘贴到聊天、日志或GitHub。配置完成后可先测试海鸥股份最近可用交易日：

```bash
.venv/bin/python scripts/analyze_level2.py --code 603269.SH
```

指定历史交易日：

```bash
.venv/bin/python scripts/analyze_level2.py --code 603269.SH --date 20260901
```

健康检查中的`integrations.numcat_level2=true`仅代表Key已配置；逐笔权限、日期覆盖及原始字段映射以这条真实请求结果为准。

新版只需要企业微信群机器人生成的完整Webhook：

```dotenv
WECOM_BOT_WEBHOOK=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=你的机器人Key
```

在目标企业微信群中添加机器人、复制完整Webhook，然后运行配置脚本并在网页“预警”页点击
“测试群机器人”。系统不再读取旧版 `WECOM_CORP_ID`、`WECOM_AGENT_ID`、`WECOM_SECRET`、
`WECOM_TO_USER`，也不调用获取Token和自建应用消息接口，因此不需要企业可信IP。

Webhook中的Key等同密码：只能放在权限为600的`.env`中，不要发送到聊天、日志、Issue或GitHub。
系统只接受`https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...`格式，错误信息在进入页面或日志前会隐藏Key。网络错误、429和上游5xx会短暂重试；`93000`通常表示Webhook或机器人Key无效。

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
如果服务升级或重启时正处于AI准入请求，启动时会把遗留的`analyzing`状态恢复为
`awaiting_ai`并立即重试；运行期间超过15分钟的孤立`analyzing`状态也会由看门狗重新调度。
`/health.latest_ai_recovery`会记录最近一次启动恢复数量。

AI任务用量、预算上限、已实施降耗和分任务模型配置见[AI Token用量说明](AI_TOKEN_USAGE.md)。
