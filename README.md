# QQ Group Advisor

> A Hachile Project · 面向 QQ 社群规划的 Codex Skill

它会结合经授权的好友列表、群资料、聊天记录、个人经历和运营目标，先判断是否真的需要建群，再给出群定位、治理方式、宣传渠道、宣传文案和群规方案。

它也可能得出以下结论：改造现有群、合并重复群，或者暂时不建群。

## 能做什么

- 扫描多个 QQ 群，或深度分析指定群聊。
- 读取 [QQ Chat Exporter](https://github.com/shuakami/qq-chat-exporter) 导出的 JSON、JSONL、分块 ZIP、TXT、XLSX 和结构化 HTML。
- 通过 [NapCat](https://napneko.github.io/) 获取好友、群、群成员和群消息历史。
- 在“只分析文字”和“扩展内容分析”之间选择。
- 扩展模式支持图片、截图、表情包、链接、文件和可读音频；暂不分析视频。
- 在匿名化与保留原始信息之间选择。
- 判断应该新建、改造、合并还是暂停一个群。
- 生成一个主方案和两个替代方案。
- 设计群定位、治理程序、宣传矩阵、冷启动方案和两版群规。
- 通过 NapCat 准备并执行少量经过逐项确认的操作。

## 群定位与治理

每个方案只能选择一个主要定位，最多增加一个次要定位：

1. 完全放开的闲聊群；
2. 限定主题、有一定规则的聊天群；
3. 同学或同事交流群，允许适量闲聊；
4. 严格的分享、知识或资源群；
5. 纯通告群。

治理方式从以下四种形态中选择：

1. 轻管理，管理者偶尔监督；
2. 管理组或管理小群；
3. 管理组处理日常事务，重大事项由成员投票；
4. 民主委员会，包含选举、任期、罢免和复议程序。

本 Skill 不推荐或排名具体管理员，只设计角色、权限和产生程序。

## 安装

需要 Python 3.10 或更高版本。两个辅助脚本只使用 Python 标准库。

### 安装到 Codex

将仓库克隆到个人技能目录：

```bash
git clone <你的仓库地址> ~/.codex/skills/qq-group-advisor
```

也可以下载 ZIP，将其中的 `qq-group-advisor` 文件夹解压到：

```text
~/.codex/skills/qq-group-advisor
```

安装后确认该目录下直接存在 `SKILL.md`，然后在 Codex 中使用：

```text
$qq-group-advisor
```

## 推荐使用流程

第一次使用时准备两类信息：

- 长期档案：学校、专业、年级、经历、技能、兴趣、可用资源、群运营经验、每周可投入时间和可承受的管理压力。
- 本次任务简报：目标人群、要解决的问题、日期范围、数据范围、时间预算、成功标准、限制条件，以及是否允许改造或合并旧群。

每次分析前还必须明确：

- 数据属于本人，或已经得到数据所有者授权；
- 聊天记录的开始日期和结束日期；
- `anonymized` 或 `full` 隐私模式；
- `text` 或 `extended` 内容模式；
- 全局扫描或指定群深度分析。

可以直接这样发起任务：

```text
请使用 $qq-group-advisor 分析这些 QQ 数据。

授权：这是我自己的 QQ 数据。
日期：2026-01-01 至 2026-01-31。
隐私模式：anonymized。
内容模式：text。
范围：先扫描全部群，再深度分析最相关的 5 个群。
目标：判断我现在是否适合建立一个校内 ACG 活动群。
```

## 准备 QQ Chat Exporter 数据

将一个或多个导出文件转换成统一证据包：

```powershell
python scripts/prepare_qq_data.py export1.json export2.zip `
  --start 2026-01-01 `
  --end 2026-01-31 `
  --privacy anonymized `
  --content text `
  --output evidence.json
```

可选内容模式：

- `text`：只处理文本和结构化元数据；
- `extended`：额外整理非视频媒体候选项。

`extended` 只会建立媒体目录，不代表脚本已经理解图片、表情包或音频。Codex 必须实际读取成功后，才能把媒体内容写入结论。

完整字段和兼容范围见 [数据格式说明](references/data-formats.md)。

## 连接 NapCat

先完成 NapCat 和 OneBot HTTP 服务配置。令牌通过环境变量提供，不要把令牌写进仓库、命令示例、日志或分析报告。

```powershell
$env:NAPCAT_ACCESS_TOKEN = "你的临时令牌"

python scripts/napcat_actions.py `
  --base-url http://127.0.0.1:3000 `
  snapshot `
  --group-id 123456 `
  --start 2026-01-01 `
  --end 2026-01-31 `
  --privacy anonymized `
  --output napcat-snapshot.json
```

安全限制：

- 只连接 `127.0.0.1`、`localhost` 或 `::1` 的 HTTP 服务；
- 日期范围只过滤 NapCat 已经返回的消息，不能保证 API 返回完整历史；
- 需要完整历史时优先使用 QQ Chat Exporter；
- 匿名快照会在写入磁盘前替换已知群号、群名、QQ 号和成员名称。

## 可选的 NapCat 写操作

只允许四种操作：

- 发送群消息；
- 修改群名称；
- 修改群头像；
- 发布群公告。

不支持自动建群、踢人、禁言、任命管理员、审批入群或自动群发。

任何写操作都必须先生成只包含一个动作的 dry-run：

```powershell
python scripts/napcat_actions.py prepare send_group_msg `
  --group-id 123456 `
  --message "完整宣传文案" `
  --output action.json
```

核对 `action.json` 中的目标群、动作和完整内容。只有用户明确确认其中的 `confirmationId` 后，才可以执行：

```powershell
python scripts/napcat_actions.py execute action.json `
  --confirm 0123456789abcdef
```

执行前脚本会通过 `get_group_info` 核对目标群，并在写请求前生成 `.attempt.json`。即使请求超时，也会拒绝使用同一清单重试；此时应先到 QQ 客户端中人工检查结果。

## 输出内容

分析报告固定按照以下顺序生成：

1. 结论；
2. 数据范围、模式与完整性；
3. 可解释评分卡；
4. 主方案；
5. 两个替代方案；
6. 宣传矩阵；
7. 风险、证据缺口和需要人工决定的事项；
8. 可选的 NapCat dry-run。

评分卡不会合并成一个不透明总分。每项都会分别说明判断、证据、反证或缺口，以及它对方案的影响。

详细判断方法见 [社群决策框架](references/decision-framework.md)。

## 项目结构

```text
qq-group-advisor/
├── SKILL.md
├── README.md
├── agents/
│   └── openai.yaml
├── references/
│   ├── data-formats.md
│   └── decision-framework.md
├── scripts/
│   ├── napcat_actions.py
│   └── prepare_qq_data.py
└── tests/
    └── test_scripts.py
```

## 测试

```bash
python tests/test_scripts.py -v
```

测试覆盖 QCE JSON、JSONL 分块目录/ZIP、TXT、XLSX、日期过滤、去重、匿名化、视频跳过，以及 NapCat 目标核验、确认码和防重复执行机制。

## 隐私与使用边界

- 只分析属于账号所有者本人或已经获得明确授权的数据。
- 不根据敏感属性评价成员，也不生成成员画像名单。
- 匿名化可以降低泄露风险，但不构成严格的不可逆匿名保证；公开任何结果前仍应人工检查。
- 未获宣传权限的群，只能建议先联系群主，不能直接发送广告。
- 原始导出、证据包、NapCat 快照和执行清单不应提交到 GitHub。

建议在仓库的 `.gitignore` 中排除：

```gitignore
evidence*.json
napcat-snapshot*.json
action*.json
*.attempt.json
*.result.json
__pycache__/
```

## 已知限制

- 暂不分析视频。
- HTML 只支持可审计的结构化消息 JSON 或聊天记录表格；无法识别时会中止该来源。
- 超大单体 JSON 会整体加载到内存，建议改用 QCE 分块 JSONL。
- NapCat 没有在本项目中被用于自动创建 QQ 群，新群需要在 QQ 客户端手动建立。
- 当前分析依据聊天记录和用户提供的资料形成建议，不能替代用户对现实人际关系和校园环境的判断。

## 相关项目与文档

- [QQ Chat Exporter](https://github.com/shuakami/qq-chat-exporter)
- [NapCatQQ](https://github.com/NapNeko/NapCatQQ)
- [NapCat OneBot API](https://napneko.github.io/onebot/api)
- [OpenAI Skills API](https://developers.openai.com/api/reference/python/resources/skills/methods/create)
