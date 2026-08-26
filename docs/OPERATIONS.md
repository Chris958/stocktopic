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

## 更新

在项目目录拉取新版本后重新运行安装脚本。脚本复用现有 `.env` 和数据库，不会覆盖凭据或历史数据。

```bash
git pull --ff-only
./scripts/install_macos.sh
```

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
