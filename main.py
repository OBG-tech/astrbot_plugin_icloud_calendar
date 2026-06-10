"""AstrBot 插件：通过自然语言或指令读写 iCloud 日历（CalDAV）。

设计要点
--------
1. 4 个 ``@filter.llm_tool`` 函数工具（add / list / update / delete），
   大模型在对话中识别到日程意图时会自动调用，无需用户输入 ``/`` 命令。
   工具均为协程并 ``return str``：返回值会被 AstrBot 喂回给 LLM，
   由 LLM 按 SKILL.md 的格式组织最终回复（这是框架推荐用法）。
2. ``/日历`` 指令作为手动入口与连通性自检，直接 ``yield`` 结果给用户。
3. ``@filter.on_llm_request`` 在每次请求时注入「当前时间 + 简明引导」，
   让 LLM 能把“明天/下周三”等相对时间换算成绝对时间，并主动调用工具。
   可在配置中关闭（改用人格 / persona 加载 SKILL.md）。

CalDAV 说明
-----------
- iCloud 需要使用 **App 专用密码**（appleid.apple.com 生成），而非登录密码。
- caldav 库是同步阻塞的，统一通过 ``asyncio.to_thread`` 放到线程池执行，
  并用一把 ``asyncio.Lock`` 串行化，避免同一连接被多线程并发使用。
- 时间一律转换为 UTC 存储（DTSTART:...Z），读取时再转回配置时区，
  这样可避免 VTIMEZONE 兼容性问题，且各端显示的绝对时间一致。
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import date, datetime, timedelta, timezone

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - AstrBot 要求 Python 3.10+
    ZoneInfo = None  # type: ignore

# 第三方依赖延迟到模块级 try 导入，缺失时给出友好提示而不是直接让插件加载崩溃。
try:
    import caldav
    import icalendar

    _IMPORT_ERROR: str | None = None
except Exception as _e:  # noqa: BLE001
    caldav = None  # type: ignore
    icalendar = None  # type: ignore
    _IMPORT_ERROR = str(_e)


DEFAULT_GUIDE = (
    "你已接入用户的 iCloud 日历，可使用以下函数工具进行操作（无需用户输入命令前缀）：\n"
    "- icloud_add_event：添加日程\n"
    "- icloud_list_events：查询日程\n"
    "- icloud_update_event：修改日程（改时间 / 改标题）\n"
    "- icloud_delete_event：删除 / 取消日程\n\n"
    "调用规则：\n"
    "1. 当用户消息出现「时间 + 事项」（如“明天下午3点开会”），或出现查询/修改/删除日程的意图时，"
    "主动调用对应工具，不要反复追问“你确定吗”。\n"
    "2. 调用前先把相对时间换算成绝对时间，格式 YYYY-MM-DDTHH:MM:SS（24 小时制）。"
    "“下午/晚上 3 点”=15:00:00；未给结束时间则不填 end；只有日期没有时间则按 09:00:00。\n"
    "3. search_title 用关键词即可（模糊匹配）；修改/删除时尽量带上 search_date（YYYY-MM-DD）缩小范围。\n"
    "4. 工具返回结果后，用简洁中文转述给用户：新增用 ✅、删除用 🗑️、失败如实说明原因，不要编造结果。"
)

HELP_TEXT = (
    "📅 iCloud 日历助手\n"
    "直接用自然语言即可，例如：“帮我明天下午三点加个项目汇报”“这周有什么安排”。\n\n"
    "也支持指令：\n"
    "• /日历 帮助        —— 显示本帮助\n"
    "• /日历 测试        —— 测试与 iCloud 的连接\n"
    "• /日历 列表 [天数] —— 查看未来若干天的日程（默认 7 天）\n\n"
    "首次使用请在插件配置中填写 Apple ID 与 App 专用密码"
    "（在 appleid.apple.com → 登录与安全 → App 专用密码 生成）。"
)


@register(
    "astrbot_plugin_icloud_calendar",
    "Yufu_Lv",
    "通过自然语言或指令读取、添加、修改、删除 iCloud 日历（CalDAV）",
    "2.0.0",
    "https://github.com/Yufu-Lv/astrbot_plugin_icloud_calendar",
)
class ICloudCalendarPlugin(Star):
    """iCloud 日历读写插件。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._client = None  # caldav.DAVClient
        self._calendars = None  # list[caldav.Calendar]（全部日历，缓存）
        self._write_calendar = None  # caldav.Calendar（新增日程的目标日历，缓存）
        self._lock = asyncio.Lock()
        self._guide = DEFAULT_GUIDE

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def initialize(self) -> None:
        # 优先把同目录下 SKILL.md 的正文作为注入引导（单一事实来源，便于用户自行编辑）。
        try:
            skill_path = os.path.join(os.path.dirname(__file__), "SKILL.md")
            if os.path.exists(skill_path):
                with open(skill_path, encoding="utf-8") as f:
                    body = self._strip_frontmatter(f.read()).strip()
                if body:
                    self._guide = body
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[icloud_calendar] 读取 SKILL.md 失败，使用内置引导：{e}")

        if _IMPORT_ERROR:
            logger.error(
                "[icloud_calendar] 依赖未安装（%s）。请在插件目录执行："
                "pip install -r requirements.txt",
                _IMPORT_ERROR,
            )

    async def terminate(self) -> None:
        self._reset()

    # ------------------------------------------------------------------ #
    # 配置便捷读取
    # ------------------------------------------------------------------ #
    @property
    def _tz(self):
        name = (self.config.get("timezone") or "Asia/Shanghai").strip()
        if ZoneInfo is None:
            return timezone.utc
        try:
            return ZoneInfo(name)
        except Exception:  # noqa: BLE001
            logger.warning(f"[icloud_calendar] 无效时区 {name!r}，回退到 UTC")
            return timezone.utc

    @property
    def _default_duration(self) -> int:
        try:
            return int(self.config.get("default_duration_minutes", 60)) or 60
        except (TypeError, ValueError):
            return 60

    @property
    def _default_days(self) -> int:
        try:
            return int(self.config.get("list_default_days", 7)) or 7
        except (TypeError, ValueError):
            return 7

    @property
    def _max_results(self) -> int:
        try:
            return max(1, int(self.config.get("max_results", 20)))
        except (TypeError, ValueError):
            return 20

    # ------------------------------------------------------------------ #
    # LLM 引导注入
    # ------------------------------------------------------------------ #
    @filter.on_llm_request()
    async def _inject_guidance(self, event: AstrMessageEvent, req) -> None:
        """在每次 LLM 请求前注入当前时间与日历助手引导。"""
        if not self.config.get("inject_guidance", True):
            return
        try:
            now = datetime.now(self._tz)
            weekday = "一二三四五六日"[now.weekday()]
            tz_name = (self.config.get("timezone") or "Asia/Shanghai").strip()
            header = (
                f"\n\n[日历助手] 当前时间：{now.strftime('%Y-%m-%d %H:%M')}"
                f"（星期{weekday}，时区 {tz_name}）。\n"
            )
            req.system_prompt = (getattr(req, "system_prompt", "") or "") + header + self._guide
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[icloud_calendar] 注入引导失败：{e}")

    # ------------------------------------------------------------------ #
    # 函数工具（供 LLM 调用，返回 str → 交给 LLM 组织回复）
    # ------------------------------------------------------------------ #
    @filter.llm_tool("icloud_add_event")
    async def icloud_add_event(
        self,
        event: AstrMessageEvent,
        title: str = "",
        start: str = "",
        end: str = "",
        description: str = "",
    ) -> str:
        """在 iCloud 日历中添加一个新日程/事件。当用户想记录、安排、预约某个带时间的事项时调用。

        Args:
            title(string): 日程标题，例如“项目汇报”“牙医预约”。必填。
            start(string): 开始时间，本地时区，格式 YYYY-MM-DDTHH:MM:SS（24 小时制），例如 2026-06-11T15:00:00。必填。
            end(string): 结束时间，格式同 start。可选；不填则默认开始时间后一段时间（见插件配置）。
            description(string): 备注 / 详情，可选。
        """
        guard = self._dep_guard()
        if guard:
            return guard

        title = (title or "").strip()
        if not title:
            return "缺少日程标题，请补充这个日程叫什么。"

        start_dt = self._parse_dt(start, default_hour=9)
        if start_dt is None:
            return f"无法识别开始时间 {start!r}，请提供形如 2026-06-11T15:00:00 的时间。"

        end_dt = self._parse_dt(end) if (end or "").strip() else None
        if end_dt is None or end_dt <= start_dt:
            end_dt = start_dt + timedelta(minutes=self._default_duration)

        ical = self._build_ical(title, start_dt, end_dt, (description or "").strip())
        try:
            cal_name = await self._run(self._add_sync, ical)
        except Exception as e:  # noqa: BLE001
            return self._err("添加日程", e)

        lines = [
            f"已成功添加日程到 iCloud（日历：{cal_name}）：",
            f"标题：{title}",
            f"开始：{self._fmt(start_dt)}",
            f"结束：{self._fmt(end_dt)}",
        ]
        if (description or "").strip():
            lines.append(f"备注：{description.strip()}")
        return "\n".join(lines)

    @filter.llm_tool("icloud_list_events")
    async def icloud_list_events(
        self,
        event: AstrMessageEvent,
        days: str = "",
        start: str = "",
        end: str = "",
    ) -> str:
        """查询 iCloud 日历中的日程。当用户问“我有什么安排”“这周日程”“最近的计划”等时调用。

        Args:
            days(string): 查询从现在起未来多少天的日程，例如 "7" 表示未来一周。可选，默认 7。
            start(string): 查询起始日期/时间，格式 YYYY-MM-DD 或 YYYY-MM-DDTHH:MM:SS。可选，提供后优先于 days。
            end(string): 查询结束日期/时间，格式同 start。可选，与 start 搭配。
        """
        guard = self._dep_guard()
        if guard:
            return guard

        start_utc, end_utc, label = self._resolve_window(days, start, end)
        try:
            items = await self._run(self._list_sync, start_utc, end_utc)
        except Exception as e:  # noqa: BLE001
            return self._err("查询日程", e)

        if not items:
            return f"{label}没有找到日程安排。"

        lines = [f"{label}共有 {len(items)} 个日程："]
        lines.extend(self._format_lines(items))
        return "\n".join(lines)

    @filter.llm_tool("icloud_update_event")
    async def icloud_update_event(
        self,
        event: AstrMessageEvent,
        search_title: str = "",
        search_date: str = "",
        new_title: str = "",
        new_start: str = "",
        new_end: str = "",
    ) -> str:
        """修改 iCloud 日历中已有的日程（改时间、改标题）。当用户想推迟/提前/改名某个日程时调用。

        Args:
            search_title(string): 用于搜索目标日程的标题关键词（模糊匹配），例如“会议”。必填。
            search_date(string): 目标日程所在日期 YYYY-MM-DD，用于缩小搜索范围。可选但强烈建议提供。
            new_title(string): 新的标题。可选。
            new_start(string): 新的开始时间 YYYY-MM-DDTHH:MM:SS。可选。
            new_end(string): 新的结束时间，格式同 new_start。可选。
        """
        guard = self._dep_guard()
        if guard:
            return guard

        keyword = (search_title or "").strip()
        if not keyword:
            return "请告诉我要修改哪个日程（提供标题关键词）。"
        if not any((v or "").strip() for v in (new_title, new_start, new_end)):
            return "请说明要把这个日程改成什么（新时间或新标题）。"

        start_utc, end_utc, _ = self._resolve_search_window(search_date)
        new_start_dt = self._parse_dt(new_start) if (new_start or "").strip() else None
        new_end_dt = self._parse_dt(new_end) if (new_end or "").strip() else None
        overrides = {
            "new_title": (new_title or "").strip(),
            "new_start": new_start_dt,
            "new_end": new_end_dt,
        }
        try:
            status, payload = await self._run(
                self._search_and_update_sync, keyword, start_utc, end_utc, overrides
            )
        except Exception as e:  # noqa: BLE001
            return self._err("修改日程", e)

        if status == "none":
            return self._not_found(keyword, search_date)
        if status == "many":
            return self._many(keyword, payload)
        return (
            "已修改日程：\n"
            f"标题：{payload['title']}\n"
            f"开始：{self._fmt(payload['start'])}\n"
            f"结束：{self._fmt(payload['end'])}"
        )

    @filter.llm_tool("icloud_delete_event")
    async def icloud_delete_event(
        self,
        event: AstrMessageEvent,
        search_title: str = "",
        search_date: str = "",
    ) -> str:
        """删除 iCloud 日历中的日程。当用户说取消/删除/不去了某个日程时调用。

        Args:
            search_title(string): 目标日程的标题关键词（模糊匹配）。必填。
            search_date(string): 目标日程所在日期 YYYY-MM-DD，用于缩小搜索范围。可选但建议提供。
        """
        guard = self._dep_guard()
        if guard:
            return guard

        keyword = (search_title or "").strip()
        if not keyword:
            return "请告诉我要删除哪个日程（提供标题关键词）。"

        start_utc, end_utc, _ = self._resolve_search_window(search_date)
        try:
            status, payload = await self._run(
                self._search_and_delete_sync, keyword, start_utc, end_utc
            )
        except Exception as e:  # noqa: BLE001
            return self._err("删除日程", e)

        if status == "none":
            return self._not_found(keyword, search_date)
        if status == "many":
            return self._many(keyword, payload)
        return f"已删除日程「{payload['summary']}」（{self._fmt(payload['start'])}）。"

    # ------------------------------------------------------------------ #
    # 指令入口（手动 / 自检），直接回复用户
    # ------------------------------------------------------------------ #
    @filter.command("日历")
    async def calendar_cmd(
        self,
        event: AstrMessageEvent,
        action: str = "",
        arg: str = "",
    ):
        """iCloud 日历助手。用法：/日历 帮助 | /日历 测试 | /日历 列表 [天数]"""
        action = (action or "").strip().lower()

        if action in ("", "帮助", "help", "?", "？"):
            yield event.plain_result(HELP_TEXT)
            return

        guard = self._dep_guard()
        if guard:
            yield event.plain_result(guard)
            return

        if action in ("测试", "test", "status", "状态"):
            try:
                info = await self._run(self._status_sync)
            except Exception as e:  # noqa: BLE001
                yield event.plain_result(self._err("连接 iCloud", e))
                return
            scope = "全部日历" if self.config.get("query_all_calendars", True) else "写入日历"
            lines = [
                "✅ 已连接 iCloud",
                f"写入日历：{info['write']}",
                f"查询范围：{scope}（共 {len(info['calendars'])} 个日历）",
                "未来 7 天各日历日程数：",
            ]
            for c in info["calendars"]:
                cnt = "读取失败" if c["count"] < 0 else f"{c['count']} 个"
                lines.append(f"• {c['name']}：{cnt}")
            yield event.plain_result("\n".join(lines))
            return

        if action in ("列表", "查询", "list", "ls"):
            days = arg.strip() if (arg or "").strip().isdigit() else str(self._default_days)
            start_utc, end_utc, label = self._resolve_window(days, "", "")
            try:
                items = await self._run(self._list_sync, start_utc, end_utc)
            except Exception as e:  # noqa: BLE001
                yield event.plain_result(self._err("查询日程", e))
                return
            if not items:
                yield event.plain_result(f"📅 {label}没有日程。")
                return
            lines = [f"📅 {label}的日程（{len(items)} 个）："]
            lines.extend(self._format_lines(items))
            yield event.plain_result("\n".join(lines))
            return

        yield event.plain_result("未识别的子命令。\n\n" + HELP_TEXT)

    # ================================================================== #
    # 内部：异步调度（线程池 + 串行锁 + 失败重连一次）
    # ================================================================== #
    async def _run(self, fn, *args):
        async with self._lock:
            return await asyncio.to_thread(self._call_with_retry, fn, *args)

    def _call_with_retry(self, fn, *args):
        try:
            return fn(*args)
        except RuntimeError:
            # 配置类错误（未填账号、找不到日历等），无需重试。
            raise
        except Exception as e:  # noqa: BLE001 - 可能是连接/会话过期，重连重试一次
            logger.warning(f"[icloud_calendar] 操作失败，重连后重试一次：{e}")
            self._reset()
            return fn(*args)

    def _reset(self) -> None:
        self._client = None
        self._calendars = None
        self._write_calendar = None

    def _ensure_calendars(self):
        """（同步）确保已连接并返回账户下的全部日历，结果缓存在实例上。"""
        if self._calendars is not None:
            return self._calendars

        username = (self.config.get("username") or "").strip()
        password = (self.config.get("password") or "").strip()
        if not username or not password:
            raise RuntimeError(
                "尚未配置 Apple ID 或 App 专用密码，请在插件配置中填写。"
            )
        url = (self.config.get("caldav_url") or "https://caldav.icloud.com/").strip()

        client = caldav.DAVClient(url=url, username=username, password=password)
        principal = client.principal()
        calendars = principal.calendars()
        if not calendars:
            raise RuntimeError("该 iCloud 账户下没有找到任何日历。")

        self._client = client
        self._calendars = list(calendars)
        return self._calendars

    def _ensure_write_calendar(self):
        """（同步）返回新增日程的目标日历：优先配置的名称，否则第一个。"""
        if self._write_calendar is not None:
            return self._write_calendar

        calendars = self._ensure_calendars()
        name = (self.config.get("calendar_name") or "").strip()
        if name:
            target = None
            for c in calendars:
                if (self._safe_name(c) or "") == name:
                    target = c
                    break
            if target is None:
                available = ", ".join(self._safe_names(calendars))
                raise RuntimeError(
                    f"未找到名为「{name}」的日历。可用日历：{available or '（无法获取名称）'}"
                )
        else:
            target = calendars[0]

        self._write_calendar = target
        return target

    def _query_calendars(self):
        """（同步）查询/搜索时要遍历的日历：默认全部，可在配置中限定为写入日历。"""
        if self.config.get("query_all_calendars", True):
            return self._ensure_calendars()
        return [self._ensure_write_calendar()]

    # ================================================================== #
    # 内部：同步 CalDAV 操作（运行于线程池）
    # ================================================================== #
    def _add_sync(self, ical_text: str):
        cal = self._ensure_write_calendar()
        cal.add_event(ical_text)
        return self._safe_name(cal) or "（默认）"

    def _list_sync(self, start_utc: datetime, end_utc: datetime):
        items = []
        last_err = None
        got_any = False
        for cal in self._query_calendars():
            cal_name = self._safe_name(cal)
            try:
                results = cal.search(
                    start=start_utc, end=end_utc, event=True, expand=True
                )
                got_any = True
            except Exception as e:  # noqa: BLE001 - 单个日历失败不影响其它日历
                last_err = e
                logger.warning(f"[icloud_calendar] 查询日历「{cal_name}」失败：{e}")
                continue
            for r in results:
                for comp in self._iter_vevents(r):
                    info = self._extract(comp)
                    if info:
                        info["calendar"] = cal_name
                        items.append(info)
        if not got_any and last_err is not None:
            raise last_err
        items.sort(key=lambda x: x["sort"])
        return items[: self._max_results]

    def _search_matches(self, keyword: str, start_utc: datetime, end_utc: datetime):
        """跨所有（查询）日历搜索，返回 [(resource, info), ...]，未展开（便于删除/重建）。"""
        kw = keyword.strip().lower()
        matches = []
        last_err = None
        got_any = False
        for cal in self._query_calendars():
            cal_name = self._safe_name(cal)
            try:
                results = cal.search(
                    start=start_utc, end=end_utc, event=True, expand=False
                )
                got_any = True
            except Exception as e:  # noqa: BLE001
                last_err = e
                logger.warning(f"[icloud_calendar] 搜索日历「{cal_name}」失败：{e}")
                continue
            for r in results:
                try:
                    comp = r.icalendar_component
                except Exception:  # noqa: BLE001
                    continue
                if comp is None:
                    continue
                summary = str(comp.get("summary", "") or "")
                if kw and kw not in summary.lower():
                    continue
                info = self._extract(comp)
                if info:
                    info["calendar"] = cal_name
                    matches.append((r, info))
        if not got_any and last_err is not None:
            raise last_err
        matches.sort(key=lambda x: x[1]["sort"])
        return matches[: self._max_results]

    def _search_and_update_sync(self, keyword, start_utc, end_utc, overrides):
        matches = self._search_matches(keyword, start_utc, end_utc)
        if not matches:
            return "none", None
        if len(matches) > 1:
            return "many", [info for _, info in matches]

        resource, info = matches[0]
        title = overrides["new_title"] or info["summary"]
        old_start = info["start"]
        old_end = info["end"]

        new_start = overrides["new_start"]
        new_end = overrides["new_end"]
        if new_start is not None:
            start = new_start
            if new_end is not None:
                end = new_end
            elif isinstance(old_start, datetime) and isinstance(old_end, datetime):
                end = start + (old_end - old_start)  # 保持原时长
            else:
                end = start + timedelta(minutes=self._default_duration)
        else:
            start = old_start
            end = new_end if new_end is not None else old_end

        if isinstance(start, datetime) and isinstance(end, datetime) and end <= start:
            end = start + timedelta(minutes=self._default_duration)

        ical = self._build_ical(title, start, end, info.get("description", ""))
        # 在原事件所属日历里重建，避免修改时把事件挪到别的日历。
        target_cal = getattr(resource, "parent", None) or self._ensure_write_calendar()
        # 先新增、再删除旧的：即使中途出错也不会丢数据（最多产生一条重复）。
        target_cal.add_event(ical)
        resource.delete()
        return "ok", {"title": title, "start": start, "end": end}

    def _search_and_delete_sync(self, keyword, start_utc, end_utc):
        matches = self._search_matches(keyword, start_utc, end_utc)
        if not matches:
            return "none", None
        if len(matches) > 1:
            return "many", [info for _, info in matches]
        resource, info = matches[0]
        resource.delete()
        return "ok", info

    def _status_sync(self):
        now = datetime.now(timezone.utc)
        end = now + timedelta(days=7)
        write_name = self._safe_name(self._ensure_write_calendar()) or "（默认）"
        cals = []
        for cal in self._ensure_calendars():
            name = self._safe_name(cal) or "（未命名）"
            try:
                count = len(
                    cal.search(start=now, end=end, event=True, expand=True)
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[icloud_calendar] 统计日历「{name}」失败：{e}")
                count = -1
            cals.append({"name": name, "count": count})
        return {"write": write_name, "calendars": cals}

    # ================================================================== #
    # 内部：iCal 构造 / 解析 / 格式化
    # ================================================================== #
    def _build_ical(self, title, start, end, description: str) -> str:
        cal = icalendar.Calendar()
        cal.add("prodid", "-//astrbot_plugin_icloud_calendar//CN")
        cal.add("version", "2.0")
        ev = icalendar.Event()
        ev.add("uid", f"{uuid.uuid4()}@astrbot-icloud-calendar")
        ev.add("summary", title)
        ev.add("dtstamp", datetime.now(timezone.utc))
        ev.add("dtstart", self._for_ical(start))
        ev.add("dtend", self._for_ical(end))
        if description:
            ev.add("description", description)
        cal.add_component(ev)
        return cal.to_ical().decode("utf-8")

    def _for_ical(self, value):
        """datetime → UTC（写出为 ...Z）；date → 原样（全天事件）。"""
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=self._tz)
            return value.astimezone(timezone.utc)
        return value

    def _iter_vevents(self, resource):
        try:
            return list(resource.icalendar_instance.walk("VEVENT"))
        except Exception:  # noqa: BLE001
            try:
                comp = resource.icalendar_component
                return [comp] if comp is not None else []
            except Exception:  # noqa: BLE001
                return []

    def _extract(self, comp) -> dict | None:
        try:
            summary = str(comp.get("summary", "") or "（无标题）")
            dtstart = comp.get("dtstart")
            dtend = comp.get("dtend")
            start = self._to_local(dtstart.dt) if dtstart is not None else None
            end = self._to_local(dtend.dt) if dtend is not None else None
            desc = comp.get("description")
            return {
                "summary": summary,
                "start": start,
                "end": end,
                "description": str(desc) if desc is not None else "",
                "sort": self._sort_key(start),
            }
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[icloud_calendar] 解析事件失败：{e}")
            return None

    def _to_local(self, value):
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(self._tz)
        return value  # date（全天）

    def _sort_key(self, start) -> datetime:
        if isinstance(start, datetime):
            return start
        if isinstance(start, date):
            return datetime(start.year, start.month, start.day, tzinfo=self._tz)
        return datetime.max.replace(tzinfo=self._tz)

    def _fmt(self, value) -> str:
        if isinstance(value, datetime):
            v = self._to_local(value)
            weekday = "一二三四五六日"[v.weekday()]
            return v.strftime(f"%Y-%m-%d（周{weekday}）%H:%M")
        if isinstance(value, date):
            weekday = "一二三四五六日"[value.weekday()]
            return value.strftime(f"%Y-%m-%d（周{weekday}）全天")
        return "（时间未知）"

    def _brief(self, info: dict, show_calendar: bool = False) -> str:
        start = info["start"]
        if isinstance(start, datetime):
            s = self._to_local(start)
            ts = s.strftime("%m-%d %H:%M")
        elif isinstance(start, date):
            ts = start.strftime("%m-%d 全天")
        else:
            ts = "时间未知"
        line = f"• {ts}  {info['summary']}"
        if show_calendar and info.get("calendar"):
            line += f"  [{info['calendar']}]"
        return line

    def _format_lines(self, items: list[dict]) -> list[str]:
        """格式化日程列表；当结果跨越多个日历时，额外标注每条所属日历。"""
        distinct = {it.get("calendar") for it in items if it.get("calendar")}
        show_calendar = len(distinct) > 1
        return [self._brief(it, show_calendar) for it in items]

    # ================================================================== #
    # 内部：时间解析与查询窗口
    # ================================================================== #
    def _parse_dt(self, s: str, default_hour: int | None = None):
        """把 ISO 风格的本地时间字符串解析为带时区的 datetime（配置时区）。

        宽松接受：T 或空格分隔、可省略秒、可省略时间（此时用 default_hour）。
        """
        if not s:
            return None
        raw = s.strip()
        if not raw:
            return None
        had_time = (":" in raw) or ("T" in raw and len(raw) > 11)
        norm = (
            raw.replace("/", "-")
            .replace("年", "-")
            .replace("月", "-")
            .replace("日", " ")
            .replace("T", " ")
        )
        norm = " ".join(norm.split())

        dt = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(norm, fmt)
                break
            except ValueError:
                continue
        if dt is None:
            try:
                dt = datetime.fromisoformat(raw)
            except Exception:  # noqa: BLE001
                return None

        if not had_time and default_hour is not None:
            dt = dt.replace(hour=default_hour, minute=0, second=0, microsecond=0)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=self._tz)
        return dt

    def _parse_date(self, s: str):
        dt = self._parse_dt(s)
        return dt.date() if dt is not None else None

    def _resolve_window(self, days: str, start: str, end: str):
        """查询窗口：优先 start/end；否则从现在起未来 days 天。返回 (start_utc, end_utc, 中文标签)。"""
        s_dt = self._parse_dt(start, default_hour=0) if (start or "").strip() else None
        if s_dt is not None:
            e_dt = self._parse_dt(end, default_hour=0) if (end or "").strip() else None
            if e_dt is None:
                n = self._safe_int(days, self._default_days)
                e_dt = s_dt + timedelta(days=n)
            label = f"{s_dt.strftime('%m-%d')} 至 {e_dt.strftime('%m-%d')} "
            return s_dt.astimezone(timezone.utc), e_dt.astimezone(timezone.utc), label

        n = self._safe_int(days, self._default_days)
        now_local = datetime.now(self._tz)
        end_local = now_local + timedelta(days=n)
        label = f"未来 {n} 天"
        return now_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc), label

    def _resolve_search_window(self, search_date: str):
        """搜索窗口：给了日期就锁定当天，否则取 [昨天, +90 天] 的较宽范围。"""
        d = self._parse_date(search_date) if (search_date or "").strip() else None
        if d is not None:
            start_local = datetime(d.year, d.month, d.day, tzinfo=self._tz)
            end_local = start_local + timedelta(days=1)
            return (
                start_local.astimezone(timezone.utc),
                end_local.astimezone(timezone.utc),
                d.strftime("%Y-%m-%d"),
            )
        now_local = datetime.now(self._tz)
        start_local = now_local - timedelta(days=1)
        end_local = now_local + timedelta(days=90)
        return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc), ""

    # ================================================================== #
    # 内部：杂项工具
    # ================================================================== #
    def _dep_guard(self) -> str | None:
        if _IMPORT_ERROR:
            return (
                "iCloud 日历插件依赖未安装（caldav / icalendar）。"
                "请在插件目录执行：pip install -r requirements.txt 后重载插件。"
            )
        return None

    def _err(self, action: str, e: Exception) -> str:
        msg = str(e)
        low = msg.lower()
        if isinstance(e, RuntimeError):
            return f"{action}失败：{msg}"
        if "401" in msg or "unauthorized" in low or "authenticat" in low:
            return (
                f"{action}失败：iCloud 认证未通过。请确认使用的是 App 专用密码"
                "（appleid.apple.com 生成），而非 Apple ID 登录密码。"
            )
        if "name or service not known" in low or "timed out" in low or "connection" in low:
            return f"{action}失败：无法连接 iCloud 服务器，请检查网络。（{msg}）"
        return f"{action}失败：{msg}"

    def _not_found(self, keyword: str, search_date: str) -> str:
        scope = f"{search_date} 的" if (search_date or "").strip() else "近期的"
        return f"没有找到{scope}、标题包含「{keyword}」的日程。"

    def _many(self, keyword: str, infos: list[dict]) -> str:
        lines = [f"找到多个包含「{keyword}」的日程，请告诉我是哪一个（可补充日期）："]
        lines.extend(self._format_lines(infos))
        return "\n".join(lines)

    @staticmethod
    def _safe_int(s, default: int) -> int:
        try:
            return int(str(s).strip())
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_name(cal) -> str:
        try:
            return cal.name or ""
        except Exception:  # noqa: BLE001
            return ""

    @classmethod
    def _safe_names(cls, calendars) -> list[str]:
        out = []
        for c in calendars:
            n = cls._safe_name(c)
            if n:
                out.append(n)
        return out

    @staticmethod
    def _strip_frontmatter(text: str) -> str:
        """去掉 Markdown 文件开头的 YAML frontmatter（--- ... ---）。"""
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) == 3:
                return parts[2]
        return text
