"""
astrbot_plugin_rate_limiter - 对话频率限制插件 v1.4.0

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

Author: yuebai
Version: 1.4.0
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
    "1.4.0",
)
class RateLimiter(Star):

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 使用记录: "private:<user_id>" → [timestamp, ...]
        #           "group:<group_id>"  → [timestamp, ...]
        self._usage: dict[str, list[float]] = defaultdict(list)

        # 静默截止: key → 时间戳。到期前该 key 的消息一律不回复
        self._muted_until: dict[str, float] = {}

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
        return self._parse_custom_limits("custom_user_limits")

    def _custom_group_limits(self) -> dict[str, int]:
        return self._parse_custom_limits("custom_group_limits")

    def _log_config_summary(self):
        logger.info(
            f"[RateLimiter] 配置加载 | 白名单用户={len(self._whitelist_users())} "
            f"白名单群={len(self._whitelist_groups())} "
            f"单独用户限制={len(self._custom_user_limits())} "
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
        now = time.time()
        self._usage[key] = [t for t in self._usage[key] if now - t < window_seconds]
        return self._usage[key]

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

        # ── 白名单检查 ──
        if sender_id in self._whitelist_users():
            return  # 用户白名单，直接放行
        if group_id and group_id in self._whitelist_groups():
            return  # 群白名单，直接放行

        window_minutes = self.config.get("window_minutes", 60)
        window_seconds = window_minutes * 60

        if group_id:
            # ── 群聊 ──
            if not self.config.get("enable_group_limit", True):
                return
            if not self._is_at_bot(msg):
                return

            # 优先使用单独限制，否则用全局默认
            limit = self._custom_group_limits().get(group_id, self.config.get("group_chat_limit", 30))
            key = f"group:{group_id}"
            chat_type = "群聊"
            target_name = f"群{group_id}"
        else:
            # ── 私聊 ──
            if not self.config.get("enable_private_limit", True):
                return

            # 优先使用单独限制，否则用全局默认
            limit = self._custom_user_limits().get(sender_id, self.config.get("private_chat_limit", 10))
            key = f"private:{sender_id}"
            chat_type = "私聊"
            target_name = f"用户{sender_id}"

        # 清理过期记录 + 检查
        current_timestamps = self._clean_expired(key, window_seconds)
        current_count = len(current_timestamps)

        if current_count >= limit:
            # ── 已超限 ──
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

        # 这条是额度内最后一条：单独发一条提醒（不影响本次正常回复流程）
        if current_count >= limit:
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
