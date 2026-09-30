"""
astrbot_plugin_rate_limiter - 对话频率限制插件 v1.6.1

滑动窗口限流：任意 window_minutes 分钟内，私聊/群聊对话次数不超过上限，
超限后不再调用 LLM，由插件直接回复提示。

v1.4.0 变更：
- 超限倒计时改为真实解锁时间：以窗口内最旧一条记录滑出窗口的时刻为准，不再虚报
- 额度用尽（放行最后一条额度）时单独发送一条提醒消息（不与本次回复拼接）
- 提示/提醒后进入静默期（默认 60 秒，可配置 mute_seconds），
  静默期内的消息一律不回复，防连发刷屏；静默期结束后才恢复正常判断
- 白名单、单独限制改为每次请求实时读取配置，WebUI 修改即时生效
- 群聊默认上限 20 → 30
- 限制提示支持 {used} {limit} {remain} 占位

v1.5.0 变更：
- 新增「启用单独用户限制」开关（enable_custom_user_limits），关闭后单独用户限制全部不生效
- 单独用户限制支持两种填法：纯 QQ 号（统一使用 custom_user_limits_count 默认条数）
  或 QQ号:次数（为该用户单独指定条数，覆盖默认）

v1.6.0 变更：
- 群聊不再只统计「@机器人」：引用回复、唤醒前缀等一切能唤起 LLM 的群消息都计入额度，
  堵住「不计数却照常消耗 token」的绕过路径
- 超限后按唤醒方式分流：@机器人 才发提示（保留静默保护防刷屏），
  非 @ 唤醒的消息静默拦截，不回复、不打扰群聊
- 私聊超限仍照旧发提示（私聊没有 @ 概念）
- 修复额度配置为 0 时 _real_unlock_wait 对空记录调用 min() 抛 ValueError、
  异常被框架吞掉导致限流静默失效的问题；额度 <= 0 沿用 AstrBot 内置限流语义，按未启用处理

v1.6.1 变更：
- 修复内存只增不减：_clean_expired 不再留下空壳键，并新增低频 _sweep 全量回收，
  清理早已过期、再也不会被访问的会话记录与静默记录
  （实测 2 万个会话各说一次、窗口过后，修复前一条记录都不回收，白占约 2.8MB）

Author: yuebai
Version: 1.6.1
"""

import time
from collections import defaultdict

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.api.message_components import At


@register(
    "astrbot_plugin_rate_limiter",
    "yuebai",
    "对话频率限制：限制私聊/群聊对话次数，支持白名单和单独限制",
    "1.6.1",
)
class RateLimiter(Star):

    # 每处理这么多次请求，做一次全量内存回收
    _SWEEP_INTERVAL = 200

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 使用记录: "private:<user_id>" → [timestamp, ...]
        #           "group:<group_id>"  → [timestamp, ...]
        self._usage: dict[str, list[float]] = defaultdict(list)

        # 静默截止: key → 时间戳。到期前该 key 的消息一律不回复
        self._muted_until: dict[str, float] = {}

        # 全量回收计数器（见 _sweep）
        self._sweep_counter = 0

        self._log_config_summary()

    # ── 配置读取（每次请求实时读，WebUI 改完即时生效） ────────

    def _whitelist_users(self) -> set[str]:
        return set(str(x).strip() for x in self.config.get("whitelist_users", []) if str(x).strip())

    def _whitelist_groups(self) -> set[str]:
        return set(str(x).strip() for x in self.config.get("whitelist_groups", []) if str(x).strip())

    def _parse_custom_limits(self, cfg_key: str) -> dict[str, int]:
        """解析 'id:次数' 或 'id1,id2:次数' 配置"""
        result: dict[str, int] = {}
        for item in self.config.get(cfg_key, []):
            item = str(item).strip()
            if not item:
                continue
            if ":" in item:
                ids_part, limit_part = item.rsplit(":", 1)
                try:
                    limit_val = int(limit_part.strip())
                except ValueError:
                    logger.warning(f"[RateLimiter] 无效的限制次数: {item}")
                    continue
                for uid in ids_part.split(","):
                    uid = uid.strip()
                    if uid:
                        result[uid] = limit_val
        return result

    def _custom_user_limits(self) -> dict[str, int]:
        """
        单独用户限制。

        未启用（enable_custom_user_limits=false）时返回空，全部走全局私聊限制。
        列表项两种填法：
          - 纯 QQ 号            → 使用 custom_user_limits_count 默认条数
          - QQ号:次数 / 批量     → 单独指定条数，覆盖默认
        """
        if not self.config.get("enable_custom_user_limits", True):
            return {}
        default_count = int(self.config.get("custom_user_limits_count", 20) or 20)
        result: dict[str, int] = {}
        for item in self.config.get("custom_user_limits", []):
            item = str(item).strip()
            if not item:
                continue
            if ":" in item:
                ids_part, limit_part = item.rsplit(":", 1)
                try:
                    limit_val = int(limit_part.strip())
                except ValueError:
                    logger.warning(f"[RateLimiter] 无效的限制次数: {item}")
                    continue
            else:
                ids_part = item
                limit_val = default_count
            for uid in ids_part.split(","):
                uid = uid.strip()
                if uid:
                    result[uid] = limit_val
        return result

    def _custom_group_limits(self) -> dict[str, int]:
        return self._parse_custom_limits("custom_group_limits")

    def _log_config_summary(self):
        logger.info(
            f"[RateLimiter] 配置加载 | 白名单用户={len(self._whitelist_users())} "
            f"白名单群={len(self._whitelist_groups())} "
            f"单独用户限制={'开' if self.config.get('enable_custom_user_limits', True) else '关'}"
            f"({len(self._custom_user_limits())}人, 默认{self.config.get('custom_user_limits_count', 20)}条/时) "
            f"单独群限制={len(self._custom_group_limits())} | "
            f"群上限={self.config.get('group_chat_limit', 30)}/时 "
            f"私聊上限={self.config.get('private_chat_limit', 10)}/时 "
            f"窗口={self.config.get('window_minutes', 60)}分钟 "
            f"静默={int(self._mute_seconds())}秒"
        )

    # ── 辅助方法 ────────────────────────────────────────────

    def _get_sender_id(self, msg) -> str:
        if not hasattr(msg, "sender") or not msg.sender:
            return "unknown"
        sender = msg.sender
        return getattr(sender, "user_id", None) or getattr(sender, "sender_id", None) or "unknown"

    def _get_group_id(self, msg) -> str:
        if not hasattr(msg, "group_id"):
            return ""
        return msg.group_id or ""

    def _is_at_bot(self, msg) -> bool:
        bot_id = str(getattr(msg, "self_id", ""))
        if not bot_id:
            return False
        message_chain = getattr(msg, "message", []) or []
        for comp in message_chain:
            if isinstance(comp, At):
                if str(getattr(comp, "qq", "")) == bot_id:
                    return True
        return False

    def _clean_expired(self, key: str, window_seconds: float) -> list[float]:
        """
        取该会话窗口内仍然有效的记录。

        这里刻意用 get() 而不是直接下标访问 defaultdict：一个从没来过、
        或者记录已经全部过期的会话，不再留下一个空壳键。
        """
        now = time.time()
        kept = [t for t in self._usage.get(key, ()) if now - t < window_seconds]
        if kept:
            self._usage[key] = kept
        else:
            self._usage.pop(key, None)
        return kept

    def _sweep(self, window_seconds: float):
        """
        全量内存回收：删掉已经过期、大概率再也不会被访问的会话记录。

        只靠 _clean_expired 是不够的——它仅清理「本次说话的那个会话」，
        那些聊过一次就再没出现过的用户/群，其键会一直留在字典里只增不减。
        这里按低频（每 _SWEEP_INTERVAL 次请求）扫一遍全部键，把它们回收掉。
        扫描量与会话数同级，单次开销可忽略。
        """
        now = time.time()
        # 记录都是按时间 append 的，最后一条即最新一条；最新一条都过期了，整条记录都已过期
        for k in [k for k, v in self._usage.items() if not v or v[-1] < now - window_seconds]:
            del self._usage[k]
        for k in [k for k, ts in self._muted_until.items() if ts <= now]:
            del self._muted_until[k]

    def _mute_seconds(self) -> float:
        """额度提示后的静默秒数：期间消息不回复，防连发刷屏。"""
        return max(0, int(self.config.get("mute_seconds", 60)))

    def _format_remain(self, seconds: float) -> str:
        seconds = max(1, int(seconds))
        m, s = divmod(seconds, 60)
        if m == 0:
            return f"{s}秒"
        if s == 0:
            return f"{m}分钟"
        return f"{m}分{s}秒"

    def _real_unlock_wait(self, timestamps: list[float], window_seconds: float) -> float:
        """
        真实解锁等待秒数。

        满额拦截时窗口内恰好 limit 条记录（只有 count < limit 才会放行并 +1），
        因此最旧一条记录滑出窗口的瞬间，count 降到 limit-1，下一条消息即可放行。
        解锁时刻 = 最旧记录时间戳 + 窗口长度。
        """
        unlock_ts = min(timestamps) + window_seconds
        return max(0, unlock_ts - time.time())

    def _compose_message(self, template: str, used: int, limit: int, remain: float) -> str:
        return (
            template.replace("{used}", str(used))
            .replace("{limit}", str(limit))
            .replace("{remain}", self._format_remain(remain))
        )

    def _default_limit_message(self) -> str:
        return "本小时对话次数已用完（{used}/{limit}），最早 {remain} 后可继续聊天~"

    def _mute(self, key: str):
        self._muted_until[key] = time.time() + self._mute_seconds()

    def _is_muted(self, key: str) -> bool:
        return time.time() < self._muted_until.get(key, 0)

    # ── LLM 调用前钩子 ──────────────────────────────────────

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req):
        msg = event.message_obj
        if not msg:
            return

        sender_id = self._get_sender_id(msg)
        group_id = self._get_group_id(msg)

        window_minutes = self.config.get("window_minutes", 60)
        window_seconds = window_minutes * 60

        # 低频全量回收：把早已过期、再也不会被访问的会话记录清掉
        self._sweep_counter += 1
        if self._sweep_counter >= self._SWEEP_INTERVAL:
            self._sweep_counter = 0
            self._sweep(window_seconds)

        # ── 白名单检查 ──
        if sender_id in self._whitelist_users():
            return  # 用户白名单，直接放行
        if group_id and group_id in self._whitelist_groups():
            return  # 群白名单，直接放行

        if group_id:
            # ── 群聊 ──
            if not self.config.get("enable_group_limit", True):
                return

            # 优先使用单独限制，否则用全局默认
            limit = self._custom_group_limits().get(group_id, self.config.get("group_chat_limit", 30))
            key = f"group:{group_id}"
            chat_type = "群聊"
            target_name = f"群{group_id}"
            # 只有「@机器人」的消息才向群内发提示；引用回复、唤醒前缀这类背景唤醒
            # 超限后一律静默拦截，不在群里刷屏
            notify_user = self._is_at_bot(msg)
        else:
            # ── 私聊 ──
            if not self.config.get("enable_private_limit", True):
                return

            # 优先使用单独限制，否则用全局默认
            limit = self._custom_user_limits().get(sender_id, self.config.get("private_chat_limit", 10))
            key = f"private:{sender_id}"
            chat_type = "私聊"
            target_name = f"用户{sender_id}"
            # 私聊没有 @ 概念，超限必须让用户知道
            notify_user = True

        # 额度 <= 0 沿用 AstrBot 内置限流（RateLimitStage）的语义：视为未启用，不做限制。
        # 同时避免下面 _real_unlock_wait 在空记录上调用 min() 抛 ValueError。
        if limit <= 0:
            logger.info(
                f"[RateLimiter] 跳过 | {chat_type} | {target_name} | "
                f"额度配置为 {limit}，按未启用处理"
            )
            return

        # 清理过期记录 + 检查
        current_timestamps = self._clean_expired(key, window_seconds)
        current_count = len(current_timestamps)

        if current_count >= limit:
            # ── 已超限 ──
            if not notify_user:
                # 非 @ 唤醒（引用回复 / 唤醒前缀）：静默拦截，不回复，LLM 收不到
                logger.info(
                    f"[RateLimiter] 静默拦截(未@) | {chat_type} | {target_name} | "
                    f"当前={current_count}/{limit} | 窗口={window_minutes}分钟"
                )
                event.stop_event()
                return

            if self._is_muted(key):
                # 静默期：不回复（刚提示过，防连发刷屏）
                logger.debug(
                    f"[RateLimiter] 静默 | {chat_type} | {target_name} | "
                    f"超限后 {int(self._mute_seconds())} 秒内不重复提示"
                )
                event.stop_event()
                return

            remain = self._real_unlock_wait(current_timestamps, window_seconds)
            limit_msg = self._compose_message(
                self.config.get("limit_message", self._default_limit_message()),
                used=current_count,
                limit=limit,
                remain=remain,
            )

            logger.info(
                f"[RateLimiter] 触发限制 | {chat_type} | {target_name} | "
                f"当前={current_count}/{limit} | 窗口={window_minutes}分钟 | "
                f"解锁还需={self._format_remain(remain)}"
            )

            self._mute(key)
            await event.send(event.plain_result(limit_msg))
            event.stop_event()
            return

        # ── 未超限：记录 + 放行 ──
        now = time.time()
        self._usage[key].append(now)
        current_count += 1

        # 这条是额度内最后一条：单独发一条提醒（不影响本次正常回复流程）。
        # 仅 @机器人 / 私聊 才提醒；非 @ 唤醒用满额度时保持安静，也不进静默期，
        # 否则会把后续 @机器人 的提示一起静默掉。
        if notify_user and current_count >= limit:
            remain = self._real_unlock_wait(self._usage[key], window_seconds)
            quota_msg = self._compose_message(
                self.config.get("limit_message", self._default_limit_message()),
                used=current_count,
                limit=limit,
                remain=remain,
            )

            logger.info(
                f"[RateLimiter] 额度用尽提醒 | {chat_type} | {target_name} | "
                f"{current_count}/{limit} | 窗口={window_minutes}分钟 | "
                f"解锁还需={self._format_remain(remain)}"
            )

            self._mute(key)
            await event.send(event.plain_result(quota_msg))
