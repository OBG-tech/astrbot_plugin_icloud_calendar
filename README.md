# astrbot_plugin_icloud_calendar

> 让你的 AstrBot 机器人用「自然语言」读写 **iCloud 日历**。
> 给机器人发一句“明天下午三点开会”，它就会自动帮你记进 iCloud；问它“这周有啥安排”，它会列给你看。

基于 CalDAV 协议直连 iCloud，支持 **添加 / 查询 / 修改 / 删除** 日程，并提供 4 个
LLM 函数工具（`@filter.llm_tool`），大模型在对话中识别到日程意图时会**自动调用**，
无需输入任何命令前缀。

---

## ✨ 功能

| 能力 | 自然语言示例 | 背后工具 |
|------|--------------|----------|
| 添加 | “帮我明天下午三点加个项目汇报，大概一个半小时” | `icloud_add_event` |
| 查询 | “这周有什么安排？” | `icloud_list_events` |
| 修改 | “把周五的会议推到下周一同一时间” | `icloud_update_event` |
| 删除 | “下周三体检取消掉” | `icloud_delete_event` |

> - **多日历**：查询 / 修改 / 删除默认覆盖账户下**全部日历**（可用 `query_all_calendars` 关闭）；
>   新增日程写入 `calendar_name` 指定的「写入日历」（留空＝第一个）。修改会在事件原所属日历内重建。
> - 修改采用「先新建后删除」的方式实现（规避部分服务端对 CalDAV 原地更新的兼容性问题）。

---

## 📦 安装

1. 把本目录放到 AstrBot 的插件目录：`AstrBot/data/plugins/astrbot_plugin_icloud_calendar/`
   （或在 WebUI 插件市场填入仓库地址安装）。
2. 安装依赖（AstrBot 一般会自动安装 `requirements.txt`；如未自动安装，手动执行）：
   ```bash
   pip install -r requirements.txt
   ```
   依赖：`caldav`、`icalendar`、`tzdata`。
3. 在 AstrBot WebUI 重载 / 启用插件。

> 需要 AstrBot 支持 `@filter.llm_tool` 的较新版本，并已在「服务提供商」中配置可用的
> LLM，且为会话开启了「函数工具 / function-calling」。

---

## 🔑 获取 iCloud「App 专用密码」（必须）

iCloud 不接受用 Apple ID 登录密码做 CalDAV 认证，必须使用 **App 专用密码**：

1. 浏览器打开 <https://appleid.apple.com> 并登录。
2. 进入 **登录与安全 → App 专用密码**（需已开启两步验证）。
3. 点「生成 App 专用密码」，命名如 `astrbot`，复制形如 `abcd-efgh-ijkl-mnop` 的密码。

---

## ⚙️ 配置

在 AstrBot WebUI 的插件配置页填写（对应 `_conf_schema.json`）：

| 配置项 | 说明 | 默认 |
|--------|------|------|
| `username` | Apple ID（iCloud 邮箱） | 空 |
| `password` | 上一步生成的 **App 专用密码** | 空 |
| `calendar_name` | **写入日历**名称（新增日程写到哪个日历）；留空＝账户下第一个日历 | 空 |
| `query_all_calendars` | 查询/修改/删除是否覆盖**全部日历**；关闭则只在写入日历内操作 | `true` |
| `timezone` | IANA 时区 | `Asia/Shanghai` |
| `caldav_url` | CalDAV 地址；大陆账户异常可试 `https://caldav.icloud.com.cn/` | `https://caldav.icloud.com/` |
| `default_duration_minutes` | 未给结束时间时的默认时长 | `60` |
| `list_default_days` | 查询默认天数 | `7` |
| `max_results` | 单次返回最大条数 | `20` |
| `inject_guidance` | 自动注入「当前时间 + 工具引导」 | `true` |

填好后，建议先发 `/日历 测试` 验证连接。

---

## 💬 使用

### 1）自然语言（推荐）

直接和机器人说话即可，例如：

- “帮我记一下，后天上午十点去医院复查”
- “我这周有什么安排？”
- “把明天的例会改到下午四点”
- “周五的聚餐取消了”

机器人会自动调用相应工具完成操作并简洁回复。

### 2）指令（手动 / 自检）

| 指令 | 作用 |
|------|------|
| `/日历 帮助` | 查看帮助 |
| `/日历 测试` | 测试与 iCloud 的连接 |
| `/日历 列表 [天数]` | 列出未来若干天的日程（默认 7 天） |

---

## 🧠 关于 SKILL.md

[`SKILL.md`](./SKILL.md) 定义了“何时、如何”调用这些工具的引导规则。

- 当 `inject_guidance` 开启时，插件会在每次 LLM 请求前，自动注入
  **当前时间** + `SKILL.md` 正文（若文件缺失则使用内置精简引导），
  使大模型能正确换算“明天/下周三”等相对时间并主动调用工具。
- 你也可以把 `SKILL.md` 的内容粘贴进 AstrBot 的 **人格(Persona)** system prompt，
  然后把 `inject_guidance` 关掉，效果类似。

想调整触发策略或输出风格，直接编辑 `SKILL.md` 后重载插件即可。

---

## ❓ 常见问题

- **提示认证失败 / 401**：确认用的是 *App 专用密码* 而非登录密码；确认两步验证已开启。
- **找不到日历**：`calendar_name` 与 iCloud 中的名称需完全一致；留空则用第一个日历。
- **时间差了几个小时**：检查 `timezone` 配置是否为你所在时区。
- **机器人不主动调用工具**：确认会话启用了 function-calling、`inject_guidance` 为开，
  或把 `SKILL.md` 配置为人格。
- **修改/删除了整组重复日程**：对“重复(周期)事件”当前按整组处理，建议对一次性事件使用；
  搜索时尽量带上 `search_date` 精确定位。

---

## 🛠 技术说明

- 时间统一转换为 **UTC** 存储（`DTSTART:...Z`），读取时再转回配置时区，
  避免 VTIMEZONE 兼容问题，并保证各端显示的绝对时间一致。
- `caldav` 为同步阻塞库，插件统一用 `asyncio.to_thread` 放入线程池执行，
  并以一把锁串行化，避免阻塞事件循环。
- 4 个工具均为协程并 `return str`：返回值会被 AstrBot 喂回给 LLM，
  由 LLM 按 `SKILL.md` 的格式组织最终回复。
