# 数据格式与连接说明

## 1. 数据来源优先级

1. 用户提供且范围清楚的 QQChatExporter（QCE）导出；
2. 本机 NapCat/OneBot 只读快照；
3. 用户填写的长期档案和本次任务简报；
4. 对缺失信息的明确人工说明。

不要直接解析 QQ 本地数据库。数据库兼容和解密交给 QCE；实时/结构化读取交给用户已配置好的 NapCat。

## 2. QCE 支持范围

### JSON

单文件 JSON 识别根节点中的 `chatInfo` 和 `messages`。消息主要读取：

- `id`、`seq`、`timestamp`/`time`；
- `sender.uid`、`sender.uin`、`sender.name`、`sender.nickname`、`sender.groupCard`；
- `type`；
- `content.text`、`content.elements`、`content.resources`、`content.mentions`；
- `recalled`、`system`。

解析器不会凭空补全缺字段。超大单体 JSON 会整体载入内存；当文件明显过大，应在 QCE 中改用分块 JSONL。

### 分块目录和 ZIP

识别 `manifest.json` 中的 `chunked.chunks[]`，读取各项的 `relativePath` 或 `fileName`，逐行解析 `chunks/*.jsonl`。ZIP 直接读取而不解压，并拒绝绝对路径或含 `..` 的条目。

### TXT

识别 QCE V5 标记、聊天名称/类型，以及重复消息块：发送者行、`时间:`、`内容:`，可选 `资源:`、`提及:`、`回复:`。自由编写的 TXT 不会被猜作 QCE 导出。

### XLSX

使用 Python 标准库读取 OOXML，寻找包含 `时间`、`发送者`、`消息内容` 的工作表。兼容典型列：`序号`、`时间`、`发送者`、`发送者QQ号`、`群头衔`、`消息类型`、`消息内容`、`是否撤回`、`资源数量`。

XLSX 的资源数量不等于媒体路径，所以扩展内容覆盖通常低于 JSON/HTML 导出。

### HTML

仅接受两种可审计结构：`application/json` 脚本中包含 `messages`，或表格首行包含 `时间`、`发送者`、`消息内容`。若 QCE 版本输出了另一种 DOM 结构，解析器会中止该来源；先保留原文件，再根据真实样本扩展解析器。

## 3. 证据包

```powershell
python scripts/prepare_qq_data.py export1.json export2.zip `
  --start 2026-01-01 --end 2026-01-31 `
  --privacy anonymized --content extended `
  --max-samples-per-chat 200 --output evidence.json
```

输出 `qq-group-advisor-evidence-v1` 包含：请求日期、模式、来源警告、每个会话的消息数/参与人数/活跃日期/头部成员占比/小时分布、均匀抽样消息和媒体候选。

- 去重优先用 `群标识 + 消息 id`；没有 id 时使用时间、发送者和文本哈希。
- 日期按运行机器的本地时区，首尾日期均包含。
- `anonymized` 会稳定替换当前证据包内的群和人员标识，并替换文本中的已知姓名、QQ 号及 5–12 位独立数字。匿名化降低风险但不是形式化不可逆匿名保证；输出前仍需人工抽查。
- `full` 保留导出中的身份字段。只在明确需要时使用，证据包同样视为敏感文件。
- 抽样用于大范围比较，不代表完整语义。得出关键结论前可回到已授权原文核对。

## 4. 内容模式

`text` 只保留文本、时间、发送者代号、消息类型、撤回/系统标识和汇总。

`extended` 还建立非视频媒体候选目录：图片、截图、表情包、链接、文件、语音/音频。脚本不会自动理解这些文件；分析者必须逐个读取实际存在且获授权的媒体，并在报告中写明：候选数、成功读取数、失败数、跳过数及失败原因。视频一律统计为跳过。

## 5. NapCat 只读快照

令牌只从临时环境变量读取，不写入命令参数、清单、结果或日志：

```powershell
$env:NAPCAT_ACCESS_TOKEN = "临时令牌"
python scripts/napcat_actions.py --base-url http://127.0.0.1:3000 snapshot `
  --group-id 123456 --group-id 789012 `
  --start 2026-01-01 --end 2026-01-31 --privacy anonymized `
  --output napcat-snapshot.json
```

使用的 OneBot API：`get_friend_list`、`get_group_list`，以及每个选定群的 `get_group_info`、`get_group_member_list`、`get_group_msg_history`。基础 URL 只允许本机 HTTP 地址。日期范围会过滤 API 已返回的历史消息，但 API 返回量未必覆盖完整区间；需要完整历史时使用 QCE 导出。`anonymized` 在落盘前替换已知群名、群号、成员名、QQ 号和文本中的身份串，`full` 保留原值。

## 6. NapCat 写操作

白名单只有：

| 命令动作 | NapCat API | 必填内容 |
|---|---|---|
| `send_group_msg` | `send_group_msg` | 群号、完整消息 |
| `set_group_name` | `set_group_name` | 群号、完整新名称 |
| `set_group_portrait` | `set_group_portrait` | 群号、头像文件/URL |
| `send_group_notice` | `_send_group_notice` | 群号、完整公告 |

先 `prepare` 生成只含一个动作的 dry-run 清单。清单的确认码由基础 URL、动作、端点和完整参数计算；任何修改都会使旧确认码失效。

```powershell
python scripts/napcat_actions.py prepare set_group_name `
  --group-id 123456 --group-name "新名称" --output rename.json
```

向用户展示清单全文。用户明确确认该确认码后才执行：

```powershell
python scripts/napcat_actions.py execute rename.json --confirm 0123456789abcdef
```

执行前脚本先调用 `get_group_info`，只有返回群号与 dry-run 目标完全一致才继续。随后在写请求发出前写入相邻的 `.attempt.json`；不论是否拿到响应，都拒绝同一清单再次执行。收到响应时另写 `.result.json`。若超时、连接中断或结果含糊，可能发生“服务端已执行但客户端没拿到响应”；此时先由用户在 QQ 中人工核实，禁止重跑。

## 7. 数据完整性标记

报告至少区分：完整、部分、未知、读取失败。以下情况必须降低置信度：日期太短、只选活跃群、缺少沉默群/好友关系、QCE 与 NapCat 时间范围不一致、XLSX 无媒体路径、扩展模式媒体未实际读取、撤回消息不可见、群成员列表与聊天时间不匹配。
