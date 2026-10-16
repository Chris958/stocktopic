# 飞书通知

## 触发范围

- 本地2–3股`early_observation`继续保存、显示和计数，不创建`local_observation`预警，不发送通知。
- 至少4股经过AI准入后的`early_watch`和`formal`题材、已有机会、风险及数据故障继续通知。
- 通知统一由飞书自定义机器人发送；未配置飞书时仅保存业务预警，不回退企业微信。
- 本次不改变发现、AI或正式准入门槛，不补发旧预警，也不重发此前已发送的提醒。

## Mac配置

更新到最新main后运行：

```bash
./scripts/configure_integrations.sh --feishu-only
```

只会提示飞书Webhook及可选签名密钥，秘密输入不回显，原有行情、AI、账号及数据库配置保留。
配置以原子替换方式保存到权限为600的`.env`，脚本随后重启launchd服务。

```dotenv
FEISHU_BOT_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/REPLACE_WITH_LOCAL_SECRET
FEISHU_BOT_SECRET=
```

若飞书机器人开启了签名校验，填写对应签名密钥；否则留空。Webhook和签名密钥不能写入Git、
页面、日志或PR。示例地址仅为占位符，实际地址只在本机配置。

升级安装只补齐空配置项，不会将旧企业微信地址当作飞书地址。旧`WECOM_BOT_WEBHOOK`已停用。
网页“预警”页的“测试飞书机器人”调用受原有认证和CSRF保护的
`POST /api/v1/admin/feishu-test`。旧`wecom-test`路径仅保留兼容别名，也只发送飞书。

## 送达与排错

- `health.integrations.notification_channel=feishu`、`feishu_group_robot=true`表示已配置。
- `latest_feishu_error`保存最近发送失败及处理建议；`last_feishu_success_at`保存最近接口成功时间。
- 飞书业务`code=0`或旧协议`StatusCode=0`才标记成功，HTTP 200本身不算成功。
- 关键词不匹配`19024`、IP白名单拒绝`19022`、签名错误`19021`及失效地址不盲目重试。
- 网络错误、HTTP 429/5xx和限流最多共尝试3次；完整JSON请求（含签名和转义）不超过20KB。
- 现有预警去重键继续生效；接口成功只表示机器人接受，不证明用户已阅读。

页面显示具体发送通道。旧数据库列`pushed_wecom`作为兼容送达标志复用，API增加`pushed`和
`push_channel`，每条新预警通道记录在现有metadata，历史记录仍标记企业微信。无数据库迁移。

协议参考：[飞书自定义机器人官方指南](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot)。
