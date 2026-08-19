"""
astrbot_plugin_rate_limiter - 对话频率限制插件 v1.3.0

在原有基础上新增：
- 用户白名单 / 群聊白名单（不受限制）
- 指定用户单独限制次数
- 指定群聊单独限制次数

Author: yuebai
Version: 1.3.0
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
    "1.3.0",
)
class RateLimiter(Star):

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 使用记录: "private:<user_id>" → [timestamp, ...]
        #           "group:<group_id>"  → [timestamp, ...]
        self._usage: dict[str, list[float]] = defaultdict(list)

        # 解析自定义限制配置
        self._custom_user_limits: dict[str, int] = {}
        self._custom_group_limits: dict[str, int] = {}
        self._whitelist_users: set[str] = set()
        self._whitelist_groups: set[str] = set()
        self._parse_config()

    def _parse_config(self):
        """解析白名单和单独限制配置"""
        self._whitelist_users = set(str(x).strip() for x in self.config.get("whitelist_users", []) if str(x).strip())
        self._whitelist_groups = set(str(x).strip() for x in self.config.get("whitelist_groups", []) if str(x).strip())

        # 用户单独限制
        # 支持格式:
        #   601514573:50         → 该用户限50次
        #   601514573,123456789:50 → 两个用户都限50次
        self._custom_user_limits = {}
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
                for uid in ids_part.split(","):
                    uid = uid.strip()
                    if uid:
                        self._custom_user_limits[uid] = limit_val

        # 群聊单独限制
        self._custom_group_limits = {}
        for item in self.config.get("custom_group_limits", []):
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
                for gid in ids_part.split(","):
                    gid = gid.strip()
                    if gid:
                        self._custom_group_limits[gid] = limit_val

        logger.info(
            f"[RateLimiter] 配置加载 | 白名单用户={len(self._whitelist_users)} "
            f"白名单群={len(self._whitelist_groups)} "
            f"单独用户限制={len(self._custom_user_limits)} "
            f"单独群限制={len(self._custom_group_limits)}"
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

    def _format_remain(self, seconds: int) -> str:
        if seconds <= 0:
            return "不到1分钟"
        m, s = divmod(seconds, 60)
        if m == 0:
            return f"{s}秒"
        if s == 0:
            return f"{m}分钟"
        return f"{m}分{s}秒"

    # ── LLM 调用前钩子 ──────────────────────────────────────

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req):
        msg = event.message_obj
        if not msg:
            return

        sender_id = self._get_sender_id(msg)
        group_id = self._get_group_id(msg)

        # ── 白名单检查 ──
        if sender_id in self._whitelist_users:
            return  # 用户白名单，直接放行
        if group_id and group_id in self._whitelist_groups:
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
            limit = self._custom_group_limits.get(group_id, self.config.get("group_chat_limit", 20))
            key = f"group:{group_id}"
            chat_type = "群聊"
            target_name = f"群{group_id}"
        else:
            # ── 私聊 ──
            if not self.config.get("enable_private_limit", True):
                return

            # 优先使用单独限制，否则用全局默认
            limit = self._custom_user_limits.get(sender_id, self.config.get("private_chat_limit", 10))
            key = f"private:{sender_id}"
            chat_type = "私聊"
            target_name = f"用户{sender_id}"

        # 清理过期记录 + 检查
        current_timestamps = self._clean_expired(key, window_seconds)
        current_count = len(current_timestamps)

        if current_count >= limit:
            now = time.time()
            latest = max(current_timestamps)
            remain_seconds = int(window_seconds - (now - latest))
            remain_str = self._format_remain(remain_seconds)

            limit_msg = self.config.get(
                "limit_message",
                "你说话太快了，请休息一下再来找我~（还需等待 {remain}）",
            ).replace("{remain}", remain_str)

            logger.info(
                f"[RateLimiter] 触发限制 | {chat_type} | {target_name} | "
                f"当前={current_count}/{limit} | 窗口={window_minutes}分钟 | "
                f"剩余={remain_str}"
            )

            await event.send(event.plain_result(limit_msg))
            event.stop_event()
            return

        # 未超限：记录 + 放行
        self._usage[key].append(time.time())
