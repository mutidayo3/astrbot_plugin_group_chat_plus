"""
Web 配置面板 —— AstrBot 插件可视化视图后端 API

原先的独立面板服务器（自带 JWT 登录 / IP 防护 / 静态资源服务）已迁移为
AstrBot 4.29 内置的插件可视化视图后端：通过 ``context.register_web_api`` 注册路由，
使用 ``astrbot.api.web`` 的请求/响应 helper。登录鉴权、页面与静态资源由 AstrBot
Dashboard 统一负责；插件侧只保留业务逻辑与 IP 封禁 / 访问日志。
"""

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.web import error_response, file_response, json_response, request

from ..utils.probability_manager import ProbabilityManager

# 插件唯一识别名，必须与 metadata.yaml 的 name 一致
PLUGIN_NAME = "astrbot_plugin_group_chat_plus"

# 合法 session 名称：仅允许字母、数字、下划线、短横线、点号、感叹号
# 禁止 .. / \ 等路径遍历字符
_SAFE_SESSION_RE = re.compile(r"^[A-Za-z0-9_\-!.]+$")

# 当前请求的访问日志备注（handler 内部设置，wrapper 统一写入访问日志）

class PanelApi:
    """插件可视化视图后端 API。

    ``plugin`` 为 main.py 中的 Star 实例，业务逻辑大量依赖其属性与方法。
    """

    # ---- 仅允许在 AstrBot 传统配置界面修改的兼容保留配置 ----
    _WEB_CONFIG_READONLY_KEYS = {
        "enable_web_panel",
        "web_panel_port",
        "web_panel_host",
    }

    _CHAT_HISTORY_ALLOWED_KEYS = {
        "message_str",
        "platform_name",
        "timestamp",
        "type",
        "group_id",
        "self_id",
        "session_id",
        "message_id",
        "sender",
    }
    _CHAT_HISTORY_ALLOWED_SENDER_KEYS = {"user_id", "nickname"}
    _CHAT_HISTORY_SCALAR_TYPES = (str, int, float, bool, type(None))

    def __init__(self, plugin):
        self.plugin = plugin

        # 使用主插件已确定的 canonical 数据目录，避免不同重启路径漂移
        self.data_dir = self._resolve_data_dir(
            getattr(plugin, "plugin_data_dir", None)
        )
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # 重启/重载状态追踪（供前端轮询）
        self._restart_status = {
            "operation": None,  # "restart" | "reload" | None
            "status": "idle",  # "idle" | "pending" | "in_progress" | "success" | "failed"
            "error": None,  # str | None
            "triggered_at": None,  # float | None
            "completed_at": None,  # float | None
        }
        self._deferred_tasks: set = set()

    # ==================== 路由注册 ====================

    def register(self, context) -> None:
        """向 AstrBot 注册插件可视化视图后端 API。"""
        routes = [
            # ---- 配置 ----
            ("/config", ["GET", "POST"], self._route_config, "读取或保存插件配置"),
            ("/config/reload", ["POST"], self._handle_reload, "保存配置并重载插件"),
            ("/config/download", ["GET"], self._handle_config_download, "下载配置文件"),
            # ---- 运行数据 ----
            ("/data/sessions", ["GET"], self._handle_data_sessions, "获取会话列表"),
            (
                "/data/attention/<session>",
                ["GET"],
                self._handle_data_attention,
                "获取会话注意力数据",
            ),
            ("/data/mood/<session>", ["GET"], self._handle_data_mood, "获取会话情绪数据"),
            (
                "/data/probability/<session>",
                ["GET"],
                self._handle_data_probability,
                "获取会话概率数据",
            ),
            ("/data/proactive", ["GET"], self._handle_data_proactive, "获取主动对话状态"),
            ("/data/overview", ["GET"], self._handle_data_overview, "获取运行总览"),
            ("/data/status", ["GET"], self._handle_data_status, "获取功能开关状态"),
            (
                "/data/session-detail/<session>",
                ["GET"],
                self._handle_session_detail,
                "获取会话详情",
            ),
            # ---- 会话 ----
            ("/session/list", ["GET"], self._handle_session_list, "获取会话列表"),
            (
                "/session/reset/<session>",
                ["POST"],
                self._handle_session_reset,
                "重置会话数据",
            ),
            (
                "/session/clear-image-cache",
                ["POST"],
                self._handle_clear_image_cache,
                "清除图片描述缓存",
            ),
            (
                "/session/clean-ghosts",
                ["POST"],
                self._handle_clean_ghost_sessions,
                "清理孤立会话文件",
            ),
            (
                "/session/chat-history/<session>",
                ["GET", "POST"],
                self._route_chat_history,
                "读取或保存聊天记录",
            ),
            (
                "/session/image-cache",
                ["GET"],
                self._handle_get_image_cache,
                "获取图片缓存状态",
            ),
            # ---- 指令 ----
            ("/commands/reset", ["POST"], self._handle_cmd_reset, "执行全局重置"),
            (
                "/commands/reset-here",
                ["POST"],
                self._handle_cmd_reset_here,
                "重置指定会话",
            ),
            (
                "/commands/clear-image-cache",
                ["POST"],
                self._handle_cmd_clear_image_cache,
                "清除全部图片缓存",
            ),
            (
                "/commands/restart-status",
                ["GET"],
                self._handle_restart_status,
                "获取重载/重启状态",
            ),
            # ---- 文件 ----
            ("/files/list", ["GET"], self._handle_file_list, "列出插件数据文件"),
            ("/files/read", ["GET"], self._handle_file_read, "读取文件内容"),
            ("/files/save", ["POST"], self._handle_file_save, "保存文件内容"),
            ("/files/delete", ["POST"], self._handle_file_delete, "删除文件"),
        ]

        for suffix, methods, handler, desc in routes:
            context.register_web_api(
                f"/{PLUGIN_NAME}{suffix}",
                self._wrap(handler),
                methods,
                desc,
            )

    def _wrap(self, handler):
        """统一异常兜底，返回 JSON 错误。"""

        async def _entry(**kwargs):
            try:
                return await handler(**kwargs)
            except Exception as e:  # noqa: BLE001 - 统一兜底 JSON 错误
                logger.error(
                    f"🌐 Web API 执行失败 [{request.method} {request.path}]: {e}",
                    exc_info=True,
                )
                return error_response(f"服务器内部错误: {e}", status_code=500)

        return _entry

    async def close(self) -> None:
        """插件终止时的清理。

        延迟重载/重启任务使用独立的 HTTP 会话，设计上需要跨 terminate() 存活
        （其中一种触发方式正是调用 Dashboard 的重载 API，若在此取消会把正在执行的
        reload 协程打断），因此这里不取消任务，只释放引用集合。

        同时从共享的 ``registered_web_apis`` 中移除本插件注册的路由，避免插件被
        禁用后旧实例的 handler 仍可被外部调用。AstrBot 的重载顺序是
        terminate() → load()，因此这里移除后新实例会重新注册。
        """
        self._deferred_tasks = set()
        try:
            context = getattr(self.plugin, "context", None)
            apis = getattr(context, "registered_web_apis", None)
            if isinstance(apis, list):
                prefix = f"/{PLUGIN_NAME}/"
                apis[:] = [
                    api
                    for api in apis
                    if not (isinstance(api, tuple) and str(api[0]).startswith(prefix))
                ]
        except Exception as e:  # noqa: BLE001 - 清理失败不应阻塞插件卸载
            logger.debug(f"🌐 注销 Web 管理面板路由失败: {e}")

    # ---- 多方法端点分发 ----

    async def _route_config(self):
        """GET /config 读取配置，POST /config 批量保存配置。"""
        if request.method == "POST":
            return await self._handle_put_config()
        return await self._handle_get_config()

    async def _route_chat_history(self, session: str):
        """GET 读取聊天记录，POST 保存聊天记录。"""
        if request.method == "POST":
            return await self._handle_put_chat_history(session)
        return await self._handle_get_chat_history(session)

    # ==================== 配置缓存辅助 ====================

    def _invalidate_config_cache(self) -> None:
        """配置更新时清除缓存。"""
        if hasattr(self, "_config_file_cache"):
            del self._config_file_cache

    # ==================== 业务实现 ====================

    def _resolve_data_dir(self, data_dir: str | None) -> Path:
        """解析 Web 面板使用的 canonical 插件数据目录"""
        if data_dir:
            return Path(data_dir).resolve()

        plugin_data_dir = getattr(self.plugin, "plugin_data_dir", None)
        if plugin_data_dir:
            return Path(plugin_data_dir).resolve()

        try:
            from astrbot.core.star.star_tools import StarTools

            return Path(StarTools.get_data_dir()).resolve()
        except Exception:
            fallback = Path(__file__).parent.parent / "data"
            return fallback.resolve()

    @staticmethod
    def _split_compound_key(key: str) -> tuple[str | None, str | None, str | None]:
        """将复合 key 拆为 (platform, chat_type, chat_id)。
        格式: {platform}_{chat_type}_{chat_id}，chat_id 自身可含下划线。
        非复合格式返回 (None, None, key)。
        """
        parts = key.split("_", 2)
        if len(parts) >= 3:
            return parts[0], parts[1], parts[2]
        return None, None, key

    @staticmethod
    def _extract_chat_id(key: str) -> str:
        """从任意格式 key 中提取纯 chat_id。"""
        _, _, chat_id = PanelApi._split_compound_key(key)
        return chat_id

    def _build_chat_id_to_compound_map(self) -> dict[str, set[str]]:
        """构建 chat_id → {compound_keys} 的反向映射。

        扫描复合 key 来源（attention/cooldown/frequency/proactive）和文件路径，
        为每个 chat_id 收集所有对应的 compound key。
        用于将纯 chat_id 来源的运行时数据关联到正确的 canonical 会话。
        """
        cid_map: dict[str, set[str]] = {}

        def _add(key: str):
            plat, ctype, cid = self._split_compound_key(key)
            if plat and ctype:
                cid_map.setdefault(cid, set()).add(key)

        # 复合 key 运行时来源
        for key in self._safe_get_attention_map():
            _add(key)
        for key in self._safe_get_proactive_states():
            _add(key)
        if (
            hasattr(self.plugin, "frequency_adjuster")
            and self.plugin.frequency_adjuster
        ):
            if hasattr(self.plugin.frequency_adjuster, "check_states"):
                for key in self.plugin.frequency_adjuster.check_states:
                    _add(key)
        try:
            from ..utils.attention_manager import AttentionManager

            for key in getattr(AttentionManager, "_conversation_activity_map", {}):
                _add(key)
            for key in getattr(AttentionManager, "_fatigue_attention_block", {}):
                _add(key)
        except Exception:
            pass
        try:
            from ..utils.cooldown_manager import CooldownManager

            for key in getattr(CooldownManager, "_cooldown_map", {}):
                _add(key)
            for key in getattr(CooldownManager, "_pending_cooldown_map", {}):
                _add(key)
        except Exception:
            pass

        # 文件来源
        chat_dir = self.data_dir / "chat_history"
        if chat_dir.exists():
            for f in chat_dir.rglob("*.json"):
                try:
                    rel = f.relative_to(chat_dir)
                except ValueError:
                    continue
                parts = rel.parts
                if len(parts) == 3:
                    compound = f"{parts[0]}_{parts[1]}_{f.stem}"
                    cid_map.setdefault(f.stem, set()).add(compound)
                elif len(parts) == 1:
                    cid_map.setdefault(f.stem, set()).add(f.stem)

        return cid_map

    def _collect_all_sessions(self) -> set:
        """从所有数据源收集全部已知会话 ID（保留原始 key 格式）。

        返回的集合同时包含纯 chat_id 和复合 chat_key 两种格式。
        上层调用者根据需要自行规范化。
        """
        sessions = set()
        # 注意力数据
        for key in self._safe_get_attention_map():
            sessions.add(key)
        # 主动对话状态
        for key in self._safe_get_proactive_states():
            sessions.add(key)
        # 主动对话处理中会话
        if hasattr(self.plugin, "proactive_processing_sessions"):
            for key in self.plugin.proactive_processing_sessions:
                sessions.add(key)
        # 处理中会话
        if hasattr(self.plugin, "processing_sessions"):
            for chat_id in self.plugin.processing_sessions.values():
                sessions.add(chat_id)
        # 情绪追踪
        if hasattr(self.plugin, "mood_tracker") and self.plugin.mood_tracker:
            if hasattr(self.plugin.mood_tracker, "moods"):
                for key in self.plugin.mood_tracker.moods:
                    sessions.add(key)
        # 待转存消息缓存
        if hasattr(self.plugin, "pending_messages_cache"):
            for key in self.plugin.pending_messages_cache:
                sessions.add(key)
        # 最近回复缓存
        if hasattr(self.plugin, "recent_replies_cache"):
            for key in self.plugin.recent_replies_cache:
                sessions.add(key)
        # 等待窗口
        if hasattr(self.plugin, "_group_wait_windows"):
            for key in self.plugin._group_wait_windows:
                if isinstance(key, tuple) and len(key) == 2:
                    chat_id, _user_id = key
                    sessions.add(str(chat_id))
        # 频率调整器
        if (
            hasattr(self.plugin, "frequency_adjuster")
            and self.plugin.frequency_adjuster
            and hasattr(self.plugin.frequency_adjuster, "check_states")
        ):
            for key in self.plugin.frequency_adjuster.check_states:
                sessions.add(key)
        # 其他注意力相关运行态
        try:
            from ..utils.attention_manager import AttentionManager

            for key in getattr(AttentionManager, "_conversation_activity_map", {}):
                sessions.add(key)
            for key in getattr(AttentionManager, "_fatigue_attention_block", {}):
                sessions.add(key)
        except Exception:
            pass
        # 冷却运行态
        try:
            from ..utils.cooldown_manager import CooldownManager

            for key in getattr(CooldownManager, "_cooldown_map", {}):
                sessions.add(key)
            for key in getattr(CooldownManager, "_pending_cooldown_map", {}):
                sessions.add(key)
        except Exception:
            pass

        # 防御性过滤：排除无效 key（空字符串、None、纯空白等），防止幽灵会话
        filtered = set()
        for s in sessions:
            if not s:
                continue
            s_str = str(s)
            if not s_str.strip():
                continue
            if not _SAFE_SESSION_RE.match(s_str):
                logger.debug(f"🌐 跳过不合规的会话 key: {s_str!r}")
                continue
            filtered.add(s_str)
        return filtered

    # ==================== 认证 Handler ====================


    def _load_schema(self) -> dict:
        """加载配置 schema"""
        schema_path = Path(__file__).parent.parent / "_conf_schema.json"
        if schema_path.exists():
            with open(schema_path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def _get_config_file_path(self) -> str:
        """获取插件真实配置文件路径（兼容不同 AstrBot 版本）"""
        # 优先从 AstrBotConfig 对象上拿 config_path 属性
        if hasattr(self.plugin.config, "config_path"):
            return self.plugin.config.config_path
        # 回退：手动拼接
        try:
            from astrbot.core.config import get_astrbot_config_path

            config_dir = get_astrbot_config_path()
            return os.path.join(
                config_dir,
                "astrbot_plugin_group_chat_plus_config.json",
            )
        except Exception as e:
            logger.debug(f"🌐 获取配置路径失败，使用默认回退: {e}")
        # 最终回退
        return os.path.join(
            "data",
            "config",
            "astrbot_plugin_group_chat_plus_config.json",
        )

    def _read_config_file(self) -> dict:
        """直接从配置文件读取当前配置"""
        config_path = self._get_config_file_path()
        try:
            with open(config_path, "r", encoding="utf-8-sig") as f:
                return json.load(f)
        except FileNotFoundError:
            logger.warning(f"🌐 配置文件不存在: {config_path}")
            return {}
        except Exception as e:
            logger.error(f"🌐 读取配置文件失败: {e}")
            return {}

    def _get_config_file_cached(self) -> dict:
        """缓存配置文件内容，避免单次请求链路反复读盘。"""
        if not hasattr(self, "_config_file_cache"):
            self._config_file_cache = self._read_config_file()
        return dict(self._config_file_cache)

    @staticmethod
    def _is_relative_to(base: Path, target: Path) -> bool:
        """判断目标路径是否位于给定目录内。"""
        try:
            target.relative_to(base)
            return True
        except ValueError:
            return False

    def _is_path_within_root(self, root: Path, target: Path) -> bool:
        """解析符号链接后判断路径是否仍位于指定根目录下。"""
        try:
            real_root = root.resolve()
            real_target = target.resolve()
        except Exception:
            return False
        return self._is_relative_to(real_root, real_target)

    def _get_path_under_root(self, root: Path, rel_path: str) -> Path | None:
        """将相对路径解析到安全根目录下，若越界则返回 None。"""
        if not rel_path or ".." in rel_path or "\\" in rel_path:
            return None
        if not self._SAFE_PATH_RE.match(rel_path):
            return None
        target = root / rel_path
        return target if self._is_path_within_root(root, target) else None

    def _get_known_sessions(self) -> set[str]:
        """收集当前系统已知的会话 ID（运行态 + 历史文件）。"""
        sessions = set()
        sessions.update(str(sid) for sid in self._collect_all_sessions() if sid)

        chat_dir = self.data_dir / "chat_history"
        if chat_dir.exists():
            try:
                for file_path in chat_dir.rglob("*.json"):
                    try:
                        rel = file_path.relative_to(chat_dir)
                    except ValueError:
                        continue
                    parts = rel.parts
                    if len(parts) == 3:
                        sessions.add(f"{parts[0]}_{parts[1]}_{file_path.stem}")
                        sessions.add(file_path.stem)  # 同时添加 chat_id 短键
                    elif len(parts) == 1:
                        sessions.add(file_path.stem)
            except Exception as e:
                logger.debug(f"🌐 收集已知会话失败: {e}")
        return sessions

    def _require_known_session(self, session: str) -> tuple[bool, str]:
        """校验会话名合法且已存在于系统已知会话集合中。"""
        if not session or not _SAFE_SESSION_RE.match(session):
            return False, "无效的会话名称"
        if session not in self._get_known_sessions():
            return False, f"会话不存在: {session}"
        return True, ""

    def _validate_chat_history_message(self, item: Any) -> bool:
        """校验单条聊天记录结构是否兼容 ContextManager 持久化格式。"""
        if not isinstance(item, dict):
            return False
        if set(item.keys()) - self._CHAT_HISTORY_ALLOWED_KEYS:
            return False

        for key, value in item.items():
            if key == "sender":
                if value is None:
                    continue
                if not isinstance(value, dict):
                    return False
                if set(value.keys()) - self._CHAT_HISTORY_ALLOWED_SENDER_KEYS:
                    return False
                if not all(
                    isinstance(v, self._CHAT_HISTORY_SCALAR_TYPES)
                    for v in value.values()
                ):
                    return False
                continue

            if key == "timestamp":
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    return False
                continue

            if key in {"group_id"}:
                if not isinstance(value, self._CHAT_HISTORY_SCALAR_TYPES):
                    return False
                continue

            if not isinstance(value, self._CHAT_HISTORY_SCALAR_TYPES):
                return False

        return True

    def _validate_chat_history_messages(self, messages: Any) -> tuple[bool, str]:
        """校验聊天记录数组结构。"""
        if not isinstance(messages, list):
            return False, "messages 必须是数组"
        for index, item in enumerate(messages):
            if not self._validate_chat_history_message(item):
                return False, f"第 {index + 1} 条消息结构不合法"
        return True, ""

    def _normalize_session_scope(self, session: str) -> tuple[str, str | None]:
        """将 session 标识解析为统一的 chat_key 与原始 chat_id。"""
        if not session or not _SAFE_SESSION_RE.match(session):
            return session, None
        parts = session.split("_", 2)
        if len(parts) >= 3:
            platform_name, chat_type, chat_id = parts[0], parts[1], parts[2]
            is_private = chat_type == "private"
            return ProbabilityManager.get_chat_key(
                platform_name, is_private, chat_id
            ), chat_id
        return session, None

    def _resolve_image_cache_file(self) -> Path | None:
        """定位图片描述缓存文件，兼容当前实现与旧版路径。"""
        candidates = [self.data_dir / "image_cache" / "descriptions.jsonl"]
        legacy_path = self.data_dir / "image_description_cache.json"
        if legacy_path not in candidates:
            candidates.append(legacy_path)
        for path in candidates:
            if path.exists():
                return path
        return candidates[0]

    def _clear_image_cache_storage(self) -> bool:
        """仅清理图片描述缓存文件，不影响其他功能文件。"""
        cache_file = self._resolve_image_cache_file()
        if cache_file is None or not cache_file.exists():
            return False
        cache_file.unlink()
        return True

    def _write_config_file(self, config_data: dict) -> bool:
        """直接写入配置文件"""
        config_path = self._get_config_file_path()
        try:
            os.makedirs(os.path.dirname(config_path), exist_ok=True)
            with open(config_path, "w", encoding="utf-8-sig") as f:
                json.dump(config_data, f, indent=2, ensure_ascii=False)
            self._config_file_cache = dict(config_data)
            logger.info(f"🌐 配置已写入: {config_path}")
            return True
        except Exception as e:
            logger.error(f"🌐 写入配置文件失败: {e}")
            return False

    def _build_protected_file_message(self, action: str) -> str:
        """返回受保护文件的统一拒绝提示。"""
        if action == "read":
            return "此文件属于核心安全文件，出于安全考虑不支持在线查看。"
        if action == "save":
            return "此文件属于核心安全文件，不允许通过 Web 端修改。"
        return "此文件属于核心安全文件，不允许通过 Web 端删除。"

    async def _handle_get_config(self):
        """获取完整配置（从文件读取真实值 + schema）"""
        schema = self._load_schema()
        file_config = self._read_config_file()

        # 用 schema 默认值填充缺失项
        current = {}
        for key in schema:
            if key in file_config:
                current[key] = file_config[key]
            else:
                current[key] = schema[key].get("default")

        return json_response(
            {
                "ok": True,
                "schema": schema,
                "config": current,
                "config_file_name": os.path.basename(self._get_config_file_path()),
                "is_desktop": getattr(self.plugin, "is_desktop_mode", False),
                "desktop_info": {
                    "is_desktop": getattr(self.plugin, "is_desktop_mode", False),
                    "mode_setting": getattr(
                        self.plugin, "desktop_mode_setting", "auto"
                    ),
                    "detected_env": current.get("desktop_detected_env", ""),
                },
            }
        )

    async def _handle_put_config(self):
        """批量更新配置值（直接写入配置文件）"""
        try:
            body = await request.json(default={})
        except Exception:
            return error_response("无效请求", status_code=400)

        updates = body.get("config", {})
        if not updates:
            return error_response("无更新内容", status_code=400)

        schema = self._load_schema()
        errors = []
        forbidden_keys = []

        # 校验类型
        validated = {}
        for key, value in updates.items():
            if key not in schema:
                errors.append(f"未知配置项: {key}")
                continue
            if key in self._WEB_CONFIG_READONLY_KEYS:
                forbidden_keys.append(key)
                continue
            expected_type = schema[key].get("type", "string")
            if not self._validate_value(value, expected_type):
                errors.append(f"{key}: 类型不匹配，期望 {expected_type}")
                continue
            validated[key] = value

        if forbidden_keys:
            return error_response("以下配置仅允许在 AstrBot 传统配置界面修改: "
                    + ", ".join(forbidden_keys), status_code=403)

        if errors:
            return error_response("; ".join(errors), status_code=400)

        # 读取当前文件 → 合并修改 → 写回文件
        file_config = self._read_config_file()
        file_config.update(validated)

        if not self._write_config_file(file_config):
            return error_response("写入配置文件失败", status_code=500)

        return json_response(
            {"ok": True, "msg": "配置已保存到文件（需重载插件生效）"}
        )

    def _validate_value(self, value, expected_type: str) -> bool:
        """校验配置值类型"""
        if expected_type == "bool":
            return isinstance(value, bool)
        if expected_type == "int":
            return isinstance(value, (int,)) and not isinstance(value, bool)
        if expected_type == "float":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if expected_type in ("string", "text"):
            return isinstance(value, str)
        if expected_type == "list":
            return isinstance(value, list)
        return True

    async def _handle_reload(self):
        """保存配置并重载插件（非重启 AstrBot）"""
        # 1. 先读取前端传来的最新配置并写入文件
        try:
            body = await request.json(default={})
            updates = body.get("config", {}) if body else {}
        except Exception:
            updates = {}

        if updates:
            schema = self._load_schema()
            file_config = self._read_config_file()
            forbidden_keys = []
            validation_errors = []
            validated_updates = {}
            for key, value in updates.items():
                if key not in schema:
                    validation_errors.append(f"未知配置项: {key}")
                    continue
                if key in self._WEB_CONFIG_READONLY_KEYS:
                    forbidden_keys.append(key)
                    continue
                expected_type = schema[key].get("type", "string")
                if not self._validate_value(value, expected_type):
                    validation_errors.append(f"{key}: 类型不匹配，期望 {expected_type}")
                    continue
                validated_updates[key] = value

            if forbidden_keys:
                return error_response("以下配置仅允许在 AstrBot 传统配置界面修改: "
                        + ", ".join(forbidden_keys), status_code=403)

            if validation_errors:
                return error_response("; ".join(validation_errors), status_code=400)

            file_config.update(validated_updates)
            if not self._write_config_file(file_config):
                return error_response("写入配置文件失败", status_code=500)

        # 2. 延迟重载：先返回响应，再触发插件重载
        # 不能在 handler 中直接 await reload()，因为 reload 会调用
        # terminate() 清理插件实例，导致当前请求上下文被中断，
        # 后续的 load() 可能永远不会执行（插件只关不开）
        try:
            self._create_deferred_reload_task()
            msg = "配置已保存，插件正在重载..." if updates else "插件正在重载..."
            logger.info("🌐 Web 面板触发插件重载（延迟执行）...")
            return json_response({"ok": True, "msg": msg})
        except Exception as e:
            logger.error(f"🌐 触发插件重载失败: {e}", exc_info=True)
            return error_response("配置已保存，但重载失败，请手动重启插件。", status_code=500)

    async def _handle_config_download(self):
        """下载当前配置文件 —— 只读、安全加固、无路径参数

        安全设计：
        - 不接受任何前端传入的路径参数，文件路径由服务端内部确定
        - 仅允许下载插件自身的配置文件，禁止下载其他任何文件
        - 只读操作，不提供写入/修改/删除能力
        - 不向前端暴露服务器绝对路径
        - 所有结果均写入 Web 端可查看的访问日志（含成功/失败说明）
        """
        config_path = self._get_config_file_path()
        filename = os.path.basename(config_path)

        # 防御性校验：确保文件名符合预期格式
        if not filename or not (
            filename.endswith("_config.json") or filename.endswith("_config.yaml")
        ):
            logger.warning(f"下载配置文件被拒绝：文件名格式不符合预期 — {filename}")
            return error_response(
                f"配置文件格式异常，无法下载: {filename}", status_code=403
            )

        if not os.path.exists(config_path):
            logger.warning(f"下载配置文件失败：文件不存在 — {config_path}")
            return error_response(
                "配置文件不存在，请检查插件是否正确加载", status_code=404
            )

        try:
            with open(config_path, "rb"):
                pass
        except PermissionError:
            logger.error(f"下载配置文件失败：权限不足 — {config_path}")
            return error_response("读取配置文件权限不足", status_code=403)
        except Exception as e:
            logger.error(
                f"下载配置文件失败：读取错误 — {config_path}: {e}", exc_info=True
            )
            return error_response(f"读取配置文件失败: {e}", status_code=500)

        logger.info(f"配置文件下载成功: {filename}")

        content_type = (
            "application/json" if filename.endswith(".json") else "application/x-yaml"
        )
        return file_response(config_path, filename=filename, content_type=content_type)

    # ==================== 重启/重载状态追踪 & JWT 签发 ====================

    def _reset_restart_status(self):
        self._restart_status = {
            "operation": None,
            "status": "idle",
            "error": None,
            "triggered_at": None,
            "completed_at": None,
        }

    def _set_restart_status(self, operation, status, error=None):
        now = __import__("time").time()
        self._restart_status["operation"] = operation
        self._restart_status["status"] = status
        if status == "pending":
            self._restart_status["triggered_at"] = now
        if status in ("success", "failed"):
            self._restart_status["completed_at"] = now
        if error is not None:
            self._restart_status["error"] = error

    def _generate_jwt_token_from_dbc(self, dbc: dict) -> str:
        """从 dbc 字典签发 JWT（不依赖 self.plugin，延迟任务中安全使用）。"""
        import jwt as _jwt
        import datetime as _dt

        jwt_secret = dbc.get("jwt_secret", "")
        if not jwt_secret:
            raise ValueError("jwt_secret 不在 dashboard 配置中")
        payload = {
            "username": dbc.get("username", "astrbot"),
            "exp": _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=7),
        }
        return _jwt.encode(payload, jwt_secret, algorithm="HS256")

    # ==================== 延迟重载/重启 ====================

    def _create_deferred_reload_task(self):
        """创建延迟插件重载任务（确保 HTTP 响应先发出）

        任务内部先 sleep 等待响应发送完毕，再通过 AstrBot 仪表盘
        REST API 触发插件重载。使用独立的 HTTP 会话，不受插件
        terminate() 的影响。
        """
        # 在插件被终止前，提前捕获仪表盘连接信息
        host = getattr(self.plugin, "host", "127.0.0.1")
        port = getattr(self.plugin, "port", 6185)
        dbc = dict(getattr(self.plugin, "dbc", {}) or {})

        self._reset_restart_status()
        self._set_restart_status("reload", "pending")

        task = asyncio.ensure_future(self._do_deferred_reload(host, port, dbc))
        # 保持强引用，避免 Python 3.12+ 的 Task GC 警告
        self._deferred_tasks = getattr(self, "_deferred_tasks", set())
        self._deferred_tasks.add(task)
        task.add_done_callback(self._deferred_tasks.discard)

    async def _do_deferred_reload(self, host, port, dbc):
        """延迟执行的插件重载

        优先通过 AstrBot 仪表盘 REST API（公开稳定接口）触发重载，
        降级使用 context._star_manager.reload()（私有属性，可能随版本变动）。
        """
        await asyncio.sleep(1.0)  # 等待 HTTP 响应完全发出

        self._set_restart_status("reload", "in_progress")

        # ---- 主路径：通过 AstrBot 仪表盘 REST API ----
        try:
            import httpx

            async with httpx.AsyncClient(timeout=20.0) as session:
                token = None
                try:
                    token = self._generate_jwt_token_from_dbc(dbc)
                except Exception as jwt_e:
                    logger.warning(
                        f"jwt_secret 生成 token 失败（重载）: {jwt_e}，尝试密码登录..."
                    )

                if not token:
                    # 降级：密码登录（旧版 AstrBot v3）
                    login_url = f"http://{host}:{port}/api/auth/login"
                    resp = await session.post(
                        login_url,
                        json={
                            "username": dbc.get("username", "astrbot"),
                            "password": dbc.get("password", ""),
                        },
                    )
                    data = resp.json()
                    token = (
                        data.get("data", {}).get("token")
                        if isinstance(data, dict)
                        else None
                    )
                    if not token:
                        raise RuntimeError(f"登录响应格式错误: {data}")

                # 调用仪表盘的插件重载 API
                reload_url = f"http://{host}:{port}/api/plugin/reload"
                resp = await session.post(
                    reload_url,
                    json={"name": PLUGIN_NAME},
                    headers={"Authorization": f"Bearer {token}"},
                )
                data = resp.json()
                if isinstance(data, dict) and data.get("status") == "ok":
                    logger.info("🌐 插件重载成功（通过仪表盘 API）")
                    self._set_restart_status("reload", "success")
                    return
                raise RuntimeError(
                    f"仪表盘返回: {data.get('message', data) if isinstance(data, dict) else data}"
                )
        except Exception as e:
            logger.warning(f"🌐 通过仪表盘 API 重载失败: {e}，尝试降级方案...")

        # ---- 降级路径：直接调用 star_manager（私有 API） ----
        try:
            star_manager = self.plugin.context._star_manager
            if star_manager is None:
                raise RuntimeError("无法获取插件管理器")
            success, err_msg = await star_manager.reload(PLUGIN_NAME)
            if success:
                logger.info("🌐 插件重载成功（通过 star_manager 降级）")
                self._set_restart_status("reload", "success")
            else:
                logger.error(f"🌐 插件重载失败: {err_msg}")
                self._set_restart_status("reload", "failed", err_msg)
        except Exception as e:
            logger.error(f"🌐 插件重载异常（所有方式均失败）: {e}", exc_info=True)
            self._set_restart_status("reload", "failed", str(e))

    def _create_deferred_restart_task(self):
        """创建延迟 AstrBot 重启任务（确保 HTTP 响应先发出）"""
        host = getattr(self.plugin, "host", "127.0.0.1")
        port = getattr(self.plugin, "port", 6185)
        dbc = dict(getattr(self.plugin, "dbc", {}) or {})

        self._reset_restart_status()
        self._set_restart_status("restart", "pending")

        task = asyncio.ensure_future(self._do_deferred_restart(host, port, dbc))
        self._deferred_tasks = getattr(self, "_deferred_tasks", set())
        self._deferred_tasks.add(task)
        task.add_done_callback(self._deferred_tasks.discard)

    async def _do_deferred_restart(self, host, port, dbc):
        """延迟执行的 AstrBot 重启

        桌面端兼容：桌面端进程由 Tauri 托管，HTTP API 触发的 os.execv() 重启
        在 Windows 上实际是新建进程+退出当前，可能导致 Tauri 丢失子进程跟踪。
        """
        await asyncio.sleep(1.0)  # 等待 HTTP 响应完全发出

        self._set_restart_status("restart", "in_progress")

        is_desktop = getattr(self.plugin, "is_desktop_mode", False)
        if is_desktop:
            logger.warning(
                "🖥️ [桌面端] 通过 Web 面板触发重启。桌面端进程由 Tauri 托管，"
                "如重启后无响应，请通过桌面端托盘菜单手动重启后端。"
            )
        try:
            import httpx

            async with httpx.AsyncClient(timeout=20.0) as session:
                token = None
                try:
                    token = self._generate_jwt_token_from_dbc(dbc)
                except Exception as jwt_e:
                    logger.warning(
                        f"jwt_secret 生成 token 失败（重启）: {jwt_e}，尝试密码登录..."
                    )

                if not token:
                    # 降级：密码登录（旧版 AstrBot v3）
                    login_url = f"http://{host}:{port}/api/auth/login"
                    resp = await session.post(
                        login_url,
                        json={
                            "username": dbc.get("username", "astrbot"),
                            "password": dbc.get("password", ""),
                        },
                    )
                    data = resp.json()
                    token = (
                        data.get("data", {}).get("token")
                        if isinstance(data, dict)
                        else None
                    )
                    if not token:
                        raise RuntimeError(f"登录响应格式错误: {data}")

                restart_url = f"http://{host}:{port}/api/stat/restart-core"
                resp = await session.post(
                    restart_url,
                    headers={"Authorization": f"Bearer {token}"},
                )
                if resp.status_code == 200:
                    logger.info("🌐 AstrBot 重启请求已发送")
                    self._set_restart_status("restart", "success")
                else:
                    raise RuntimeError(f"HTTP {resp.status_code}")
        except Exception as e:
            logger.warning(f"🌐 通过仪表盘 API 重启失败: {e}，尝试降级...")
            self._set_restart_status("restart", "failed", str(e))
            try:
                await self.plugin.restart_core()
            except Exception as e2:
                logger.error(f"🌐 AstrBot 重启异常: {e2}", exc_info=True)

    # ==================== 数据 Handler ====================

    def _safe_get_attention_map(self) -> dict:
        """安全获取注意力数据"""
        try:
            from ..utils.attention_manager import AttentionManager

            return dict(AttentionManager._attention_map)
        except Exception as e:
            logger.debug(f"🌐 获取注意力数据失败: {e}")
            return {}

    def _safe_get_proactive_states(self) -> dict:
        """安全获取主动对话状态"""
        try:
            from ..utils.proactive_chat_manager import ProactiveChatManager

            return dict(ProactiveChatManager._chat_states)
        except Exception as e:
            logger.debug(f"🌐 获取主动对话状态失败: {e}")
            return {}

    def _safe_get_proactive_boost(self) -> dict:
        """安全获取临时概率提升状态"""
        try:
            from ..utils.proactive_chat_manager import ProactiveChatManager

            return dict(ProactiveChatManager._temp_probability_boost)
        except Exception as e:
            logger.debug(f"🌐 获取临时概率提升失败: {e}")
            return {}


    async def _handle_data_sessions(self):
        """列出所有已知会话，返回 canonical compound key 列表"""
        cid_map = self._build_chat_id_to_compound_map()

        # 规范化为 compound key（复合 key 优先，因为含平台/类型信息且跨平台唯一）
        canonical = set()
        for compounds in cid_map.values():
            canonical.update(compounds)

        # 纯 chat_id 来源若不在 map 中则直接加入
        raw = self._collect_all_sessions()
        for key in raw:
            plat, ctype, cid = self._split_compound_key(key)
            if plat and ctype:
                canonical.add(key)
            elif cid not in cid_map:
                # 防御性检查：跳过无效的纯 chat_id
                if not cid or not _SAFE_SESSION_RE.match(cid):
                    logger.debug(f"🌐 跳过无效的孤立会话 key: {cid!r}")
                    continue
                canonical.add(cid)

        return json_response({"ok": True, "sessions": sorted(canonical)})

    def _find_in_dict_by_chat_id(self, d: dict, session: str):
        """在 dict 中查找 session，先精确匹配，再按 chat_id 规范化匹配。"""
        if session in d:
            return d[session]
        target_cid = self._extract_chat_id(session)
        for k, v in d.items():
            if self._extract_chat_id(k) == target_cid:
                return v
        return None

    async def _handle_data_attention(self, session: str):
        """获取会话注意力数据"""
        if not session or not _SAFE_SESSION_RE.match(session):
            return error_response("无效的会话名称", status_code=400)
        _sn = self._safe_num
        attention_map = self._safe_get_attention_map()
        users_data = self._find_in_dict_by_chat_id(attention_map, session) or {}

        result = []
        import time as _time

        now = _time.time()
        for uid, profile in users_data.items():
            if not isinstance(profile, dict):
                continue
            result.append(
                {
                    "user_id": str(uid),
                    "attention_score": _sn(
                        profile.get("attention_score", 0), ndigits=4
                    ),
                    "emotion": _sn(profile.get("emotion", 0), ndigits=4),
                    "interaction_count": profile.get("interaction_count", 0),
                    "last_interaction": profile.get("last_interaction", 0),
                    "idle_seconds": round(
                        now - _sn(profile.get("last_interaction", now))
                    ),
                    "preview": str(profile.get("last_message_preview", "")),
                }
            )

        result.sort(key=lambda x: x["attention_score"], reverse=True)
        return json_response({"ok": True, "users": result})

    async def _handle_data_mood(self, session: str):
        """获取会话情绪数据"""
        if not session or not _SAFE_SESSION_RE.match(session):
            return error_response("无效的会话名称", status_code=400)
        mood_data = {}
        if hasattr(self.plugin, "mood_tracker") and self.plugin.mood_tracker:
            tracker = self.plugin.mood_tracker
            if hasattr(tracker, "moods"):
                raw = self._find_in_dict_by_chat_id(tracker.moods, session)
            else:
                raw = None
            if raw:
                mood_data = {
                    "current_mood": raw.get("mood", "平静"),
                    "intensity": round(raw.get("intensity", 0), 4),
                    "last_update": raw.get("last_update", 0),
                }
        return json_response({"ok": True, "mood": mood_data})

    async def _handle_data_probability(self, session: str):
        """获取会话当前概率状态"""
        if not session or not _SAFE_SESSION_RE.match(session):
            return error_response("无效的会话名称", status_code=400)
        import time as _time

        prob_data = {
            "initial_probability": self.plugin.config.get("initial_probability", 0.3),
            "after_reply_probability": self.plugin.config.get(
                "after_reply_probability", 0.8
            ),
            "probability_duration": self.plugin.config.get("probability_duration", 120),
            "attention_mechanism_enabled": self.plugin.config.get(
                "enable_attention_mechanism", False
            ),
            "mode": "attention"
            if self.plugin.config.get("enable_attention_mechanism", False)
            else "traditional",
        }

        # 传统回复后提升状态（ProbabilityManager 内部按 chat_id 查找，兼容两种格式）
        try:
            probability_status = (
                await ProbabilityManager.get_probability_status_snapshot(session)
            )
            reply_boost_until = probability_status.get("reply_boost_until", 0)
            if reply_boost_until > _time.time():
                prob_data["reply_boost"] = {
                    "value": probability_status.get("reply_boost_probability", 0),
                    "remaining_seconds": round(reply_boost_until - _time.time()),
                    "source": probability_status.get(
                        "reply_boost_source", "after_reply_probability"
                    ),
                }
            base_until = probability_status.get("base_until", 0)
            if base_until > _time.time():
                prob_data["base_override"] = {
                    "value": probability_status.get("base_probability", 0),
                    "remaining_seconds": round(base_until - _time.time()),
                    "source": probability_status.get("base_source", "unknown"),
                }
        except Exception as e:
            logger.debug(f"🌐 概率状态快照读取异常 [{session}]: {e}")

        # 频率调整器状态（复合 key 存储，尝试两种格式匹配）
        if (
            hasattr(self.plugin, "frequency_adjuster")
            and self.plugin.frequency_adjuster
        ):
            fa = self.plugin.frequency_adjuster
            if hasattr(fa, "check_states"):
                state = self._find_in_dict_by_chat_id(fa.check_states, session)
                if state:
                    prob_data["frequency_adjusted_probability"] = state.get(
                        "adjusted_probability"
                    )
                    prob_data["frequency_last_check"] = state.get("last_check_time", 0)

        # 临时概率提升（复合 key 存储，尝试两种格式匹配）
        boost_map = self._safe_get_proactive_boost()
        b = self._find_in_dict_by_chat_id(boost_map, session)
        if b:
            remaining = b.get("boost_until", 0) - _time.time()
            if remaining > 0:
                prob_data["temp_boost"] = {
                    "value": b.get("boost_value", 0),
                    "remaining_seconds": round(remaining),
                }

        return json_response({"ok": True, "probability": prob_data})

    async def _handle_data_proactive(self):
        """获取主动对话统计"""
        states = self._safe_get_proactive_states()
        result = {}
        for chat_key, state in states.items():
            result[chat_key] = {
                "proactive_active": state.get("proactive_active", False),
                "last_proactive_time": state.get("last_proactive_time", 0),
                "consecutive_failures": state.get("consecutive_failures", 0),
                "cooldown_until": state.get("cooldown_until", 0),
                "total_successes": state.get("successful_interactions", 0),
                "total_failures": state.get("failed_interactions", 0),
                "interaction_score": state.get("interaction_score", 50),
            }
        return json_response({"ok": True, "proactive": result})

    async def _handle_data_overview(self):
        """总览仪表盘数据"""
        attention_map = self._safe_get_attention_map()
        proactive_states = self._safe_get_proactive_states()

        total_sessions = len(
            set(list(attention_map.keys()) + list(proactive_states.keys()))
        )
        total_tracked_users = sum(len(v) for v in attention_map.values())
        processing_count = len(getattr(self.plugin, "processing_sessions", {}))

        # 额外全局统计
        total_cached_messages = 0
        if hasattr(self.plugin, "pending_messages_cache"):
            for msgs in self.plugin.pending_messages_cache.values():
                total_cached_messages += len(msgs)

        active_wait_windows = 0
        if hasattr(self.plugin, "_group_wait_windows"):
            active_wait_windows = len(self.plugin._group_wait_windows)

        cooldown_users = 0
        pending_cooldown_users = 0
        try:
            from ..utils.cooldown_manager import CooldownManager

            if hasattr(CooldownManager, "_cooldown_map"):
                for users in CooldownManager._cooldown_map.values():
                    cooldown_users += len(users)
            if hasattr(CooldownManager, "_pending_cooldown_map"):
                for users in CooldownManager._pending_cooldown_map.values():
                    pending_cooldown_users += len(users)
        except Exception:
            pass

        seen_count = len(getattr(self.plugin, "_seen_message_ids", {}))
        duplicate_blocked = len(getattr(self.plugin, "_duplicate_blocked_messages", {}))

        proactive_processing = len(
            getattr(self.plugin, "proactive_processing_sessions", {})
        )

        return json_response(
            {
                "ok": True,
                "overview": {
                    "total_sessions": total_sessions,
                    "total_tracked_users": total_tracked_users,
                    "active_processing": processing_count,
                    "proactive_active_count": sum(
                        1
                        for s in proactive_states.values()
                        if s.get("proactive_active")
                    ),
                    "total_cached_messages": total_cached_messages,
                    "active_wait_windows": active_wait_windows,
                    "cooldown_users": cooldown_users,
                    "pending_cooldown_users": pending_cooldown_users,
                    "seen_messages": seen_count,
                    "duplicate_blocked": duplicate_blocked,
                    "proactive_processing": proactive_processing,
                },
            }
        )

    async def _handle_data_status(self):
        """各功能启用/禁用状态"""
        cfg = self.plugin.config
        return json_response(
            {
                "ok": True,
                "status": {
                    "group_chat": cfg.get("enable_group_chat", True),
                    "attention_mechanism": cfg.get("enable_attention_mechanism", False),
                    "mood_system": cfg.get("enable_mood_system", True),
                    "frequency_adjuster": cfg.get("enable_frequency_adjuster", True),
                    "proactive_chat": cfg.get("enable_proactive_chat", False),
                    "typing_simulator": cfg.get("enable_typing_simulator", True),
                    "typo_generator": cfg.get("enable_typo_generator", True),
                    "image_processing": cfg.get("enable_image_processing", False),
                    "memory_injection": cfg.get("enable_memory_injection", False),
                    "humanize_mode": cfg.get("enable_humanize_mode", False),
                    "private_chat": cfg.get("enable_private_chat", False),
                    "dynamic_reply_probability": cfg.get(
                        "enable_dynamic_reply_probability", False
                    ),
                    "dynamic_proactive_probability": cfg.get(
                        "enable_dynamic_proactive_probability", False
                    ),
                    "duplicate_filter": cfg.get("enable_duplicate_filter", True),
                    "conversation_fatigue": cfg.get(
                        "enable_conversation_fatigue", False
                    ),
                    "complaint_system": cfg.get("enable_complaint_system", True),
                    "adaptive_proactive": cfg.get("enable_adaptive_proactive", True),
                },
            }
        )

    @staticmethod
    def _safe_num(val, default=0, ndigits=None):
        """安全转换数值，处理 NaN/Infinity/None 等异常值"""
        import math

        if val is None:
            val = default
        try:
            val = float(val)
        except (TypeError, ValueError):
            return default
        if math.isnan(val) or math.isinf(val):
            return default
        if ndigits is not None:
            return round(val, ndigits)
        return val

    async def _handle_session_detail(self, session: str):
        """获取会话的完整运行时数据"""
        if not session or not _SAFE_SESSION_RE.match(session):
            return error_response("无效的会话名称", status_code=400)

        try:
            return await self._build_session_detail(session)
        except Exception as e:
            logger.error(f"🌐 获取会话详情 [{session}] 失败: {e}", exc_info=True)
            return error_response(f"获取会话数据时发生内部错误: {e}", status_code=500)

    async def _build_session_detail(self, session: str):
        """构建会话详情数据（内部实现）"""
        import time as _time

        now = _time.time()
        _sn = self._safe_num
        detail = {"session_id": session}
        # 纯 chat_id 用于匹配仅使用 chat_id 为 key 的数据源
        session_cid = self._extract_chat_id(session)

        # 注意力数据（兼容两种 key 格式）
        try:
            attention_map = self._safe_get_attention_map()
            users_data = self._find_in_dict_by_chat_id(attention_map, session) or {}
            users_list = []
            for uid, profile in users_data.items():
                if not isinstance(profile, dict):
                    continue
                users_list.append(
                    {
                        "user_id": str(uid),
                        "attention_score": _sn(
                            profile.get("attention_score", 0), ndigits=4
                        ),
                        "emotion": _sn(profile.get("emotion", 0), ndigits=4),
                        "interaction_count": profile.get("interaction_count", 0),
                        "last_interaction": profile.get("last_interaction", 0),
                        "idle_seconds": round(
                            now - _sn(profile.get("last_interaction", now))
                        ),
                        "preview": str(profile.get("last_message_preview", "")),
                    }
                )
            users_list.sort(key=lambda x: x["attention_score"], reverse=True)
            detail["attention"] = {
                "user_count": len(users_list),
                "users": users_list,
            }
        except Exception as e:
            logger.debug(f"🌐 会话详情-注意力数据异常 [{session}]: {e}")
            detail["attention"] = {"user_count": 0, "users": []}

        # 情绪数据
        try:
            mood_data = {}
            if hasattr(self.plugin, "mood_tracker") and self.plugin.mood_tracker:
                tracker = self.plugin.mood_tracker
                if hasattr(tracker, "moods"):
                    raw = self._find_in_dict_by_chat_id(tracker.moods, session)
                else:
                    raw = None
                if raw:
                    if isinstance(raw, dict):
                        mood_data = {
                            "current_mood": str(raw.get("mood", "平静")),
                            "intensity": _sn(raw.get("intensity", 0), ndigits=4),
                            "last_update": raw.get("last_update", 0),
                        }
            detail["mood"] = mood_data
        except Exception as e:
            logger.debug(f"🌐 会话详情-情绪数据异常 [{session}]: {e}")
            detail["mood"] = {}

        # 概率数据
        try:
            prob_data = {
                "initial_probability": _sn(
                    self.plugin.config.get("initial_probability", 0.3)
                ),
                "after_reply_probability": _sn(
                    self.plugin.config.get("after_reply_probability", 0.8)
                ),
                "probability_duration": self.plugin.config.get(
                    "probability_duration", 120
                ),
                "attention_mechanism_enabled": self.plugin.config.get(
                    "enable_attention_mechanism", False
                ),
                "mode": "attention"
                if self.plugin.config.get("enable_attention_mechanism", False)
                else "traditional",
            }
            try:
                probability_status = (
                    await ProbabilityManager.get_probability_status_snapshot(session)
                )
                reply_boost_until = _sn(probability_status.get("reply_boost_until", 0))
                if reply_boost_until > now:
                    prob_data["reply_boost"] = {
                        "value": _sn(
                            probability_status.get("reply_boost_probability", 0)
                        ),
                        "remaining_seconds": round(reply_boost_until - now),
                        "source": probability_status.get(
                            "reply_boost_source", "after_reply_probability"
                        ),
                    }
                base_until = _sn(probability_status.get("base_until", 0))
                if base_until > now:
                    prob_data["base_override"] = {
                        "value": _sn(probability_status.get("base_probability", 0)),
                        "remaining_seconds": round(base_until - now),
                        "source": probability_status.get("base_source", "unknown"),
                    }
            except Exception as e:
                logger.debug(f"🌐 会话详情-概率快照异常 [{session}]: {e}")
            if (
                hasattr(self.plugin, "frequency_adjuster")
                and self.plugin.frequency_adjuster
            ):
                fa = self.plugin.frequency_adjuster
                if hasattr(fa, "check_states"):
                    state = self._find_in_dict_by_chat_id(fa.check_states, session)
                    if isinstance(state, dict):
                        prob_data["frequency_adjusted_probability"] = _sn(
                            state.get("adjusted_probability")
                        )
                        prob_data["frequency_last_check"] = state.get(
                            "last_check_time", 0
                        )
            boost_map = self._safe_get_proactive_boost()
            b = self._find_in_dict_by_chat_id(boost_map, session)
            if b and isinstance(b, dict):
                remaining = _sn(b.get("boost_until", 0)) - now
                if remaining > 0:
                    prob_data["temp_boost"] = {
                        "value": _sn(b.get("boost_value", 0)),
                        "remaining_seconds": round(remaining),
                    }
            detail["probability"] = prob_data
        except Exception as e:
            logger.debug(f"🌐 会话详情-概率数据异常 [{session}]: {e}")
            detail["probability"] = {}

        # 主动对话状态（兼容两种 key 格式；仅暴露前端需要的字段，避免泄露内部数据）
        try:
            proactive_states = self._safe_get_proactive_states()
            raw = self._find_in_dict_by_chat_id(proactive_states, session) or {}
            if raw and isinstance(raw, dict):
                cooldown_until = _sn(raw.get("cooldown_until", 0))
                cooldown_remaining = (
                    round(cooldown_until - now) if cooldown_until > now else 0
                )
                proactive_data = {
                    "proactive_active": raw.get("proactive_active", False),
                    "cooldown_remaining": cooldown_remaining,
                    "successful_interactions": raw.get("successful_interactions", 0),
                    "failed_interactions": raw.get("failed_interactions", 0),
                    "interaction_score": _sn(
                        raw.get("interaction_score", 50), ndigits=1
                    ),
                }
            else:
                proactive_data = {}
            detail["proactive"] = proactive_data
        except Exception as e:
            logger.debug(f"🌐 会话详情-主动对话异常 [{session}]: {e}")
            detail["proactive"] = {}

        # 消息缓存详情
        cache_count = 0
        cache_messages = []
        try:
            if hasattr(self.plugin, "pending_messages_cache"):
                cached = self.plugin.pending_messages_cache.get(session_cid, [])
                cache_count = len(cached)
                for m in cached:
                    cache_messages.append(
                        {
                            "role": m.get("role", "unknown"),
                            "content": str(m.get("content", ""))[:100],
                            "timestamp": m.get("timestamp", 0),
                            "sender_name": m.get("sender_name", ""),
                        }
                    )
        except Exception:
            pass
        detail["message_cache_count"] = cache_count
        detail["message_cache"] = cache_messages

        # 处理中状态
        is_processing = False
        try:
            if hasattr(self.plugin, "processing_sessions"):
                is_processing = session_cid in self.plugin.processing_sessions.values()
        except Exception:
            pass
        detail["is_processing"] = is_processing

        # 主动对话处理中
        try:
            detail["proactive_processing"] = session in getattr(
                self.plugin, "proactive_processing_sessions", {}
            ) or session_cid in getattr(
                self.plugin, "proactive_processing_sessions", {}
            )
        except Exception:
            detail["proactive_processing"] = False

        # 等待窗口
        wait_windows = []
        try:
            if hasattr(self.plugin, "_group_wait_windows"):
                for key, winfo in list(self.plugin._group_wait_windows.items()):
                    if not isinstance(key, tuple) or len(key) != 2:
                        continue
                    cid, uid = key
                    if str(cid) in (session, session_cid):
                        wait_windows.append(
                            {
                                "user_id": str(uid),
                                "extra_count": winfo.get("extra_count", 0),
                                "deadline": winfo.get("deadline", 0),
                                "remaining": max(
                                    0,
                                    round(winfo.get("deadline", 0) - now),
                                ),
                            }
                        )
        except Exception:
            pass
        detail["wait_windows"] = wait_windows

        # 冷却状态
        cooldown_users = []
        pending_cooldown_users = []
        try:
            from ..utils.cooldown_manager import CooldownManager
            import time as _time

            if hasattr(CooldownManager, "_cooldown_map"):
                session_cooldowns = CooldownManager._cooldown_map.get(session, {})
                for uid, cinfo in session_cooldowns.items():
                    info = await CooldownManager.get_cooldown_info(session, uid)
                    if info:
                        cooldown_users.append(
                            {
                                "user_id": str(uid),
                                "user_name": cinfo.get("user_name", ""),
                                "remaining": round(_sn(info.get("remaining_time", 0))),
                                "reason": cinfo.get("reason", ""),
                                "phase": "active",
                            }
                        )
            if hasattr(CooldownManager, "_pending_cooldown_map"):
                session_pending = CooldownManager._pending_cooldown_map.get(session, {})
                for uid, pinfo in session_pending.items():
                    pending_cooldown_users.append(
                        {
                            "user_id": str(uid),
                            "user_name": pinfo.get("user_name", ""),
                            "remaining": round(
                                max(
                                    0,
                                    _sn(
                                        CooldownManager.PENDING_COOLDOWN_MAX_WAIT_SECONDS
                                        - (
                                            _time.time() - pinfo.get("pending_start", 0)
                                        ),
                                        0,
                                    ),
                                )
                            ),
                            "reason": pinfo.get("reason", ""),
                            "grace_message_budget": pinfo.get(
                                "grace_message_budget", 0
                            ),
                            "consumed_user_messages": pinfo.get(
                                "consumed_user_messages", 0
                            ),
                            "phase": "pending",
                        }
                    )
        except Exception:
            pass
        detail["cooldowns"] = cooldown_users
        detail["pending_cooldowns"] = pending_cooldown_users

        # 回复密度
        density_data = {}
        try:
            from ..utils.reply_density_manager import ReplyDensityManager

            density_data = await ReplyDensityManager.get_density_info(session)
        except Exception:
            pass
        detail["reply_density"] = density_data if isinstance(density_data, dict) else {}

        # 会话活跃度
        activity_data = {}
        try:
            from ..utils.attention_manager import AttentionManager

            act_map = getattr(AttentionManager, "_conversation_activity_map", {})
            if session in act_map:
                raw = act_map[session]
                if isinstance(raw, dict):
                    activity_data = {
                        "activity_score": _sn(raw.get("activity_score", 0), ndigits=4),
                        "last_bot_reply": raw.get("last_bot_reply", 0),
                        "peak_user_id": str(raw.get("peak_user_id", "")),
                        "peak_user_name": str(raw.get("peak_user_name", "")),
                        "peak_attention": _sn(raw.get("peak_attention", 0), ndigits=4),
                    }
        except Exception:
            pass
        detail["conversation_activity"] = activity_data

        # 疲劳锁定
        fatigue_list = []
        try:
            from ..utils.attention_manager import AttentionManager

            fatigue_map = getattr(AttentionManager, "_fatigue_attention_block", {})
            if session in fatigue_map:
                for uid, finfo in fatigue_map[session].items():
                    fatigue_list.append(
                        {
                            "user_id": str(uid),
                            "fatigue_level": finfo.get("fatigue_level", ""),
                            "blocked_at": finfo.get("blocked_at", 0),
                        }
                    )
        except Exception:
            pass
        detail["fatigue_blocks"] = fatigue_list

        # 最近回复缓存
        recent_replies_count = 0
        try:
            if hasattr(self.plugin, "recent_replies_cache"):
                replies = self.plugin.recent_replies_cache.get(session_cid, [])
                recent_replies_count = len(replies)
        except Exception:
            pass
        detail["recent_replies_count"] = recent_replies_count

        # 聊天记录文件信息
        try:
            path = self._get_chat_history_path(session)
            if path and path.exists():
                try:
                    stat = path.stat()
                    detail["chat_history_file"] = {
                        "exists": True,
                        "file_size": stat.st_size,
                        "last_modified": stat.st_mtime,
                    }
                except OSError as e:
                    logger.debug(f"🌐 获取聊天记录文件信息失败 [{path}]: {e}")
                    detail["chat_history_file"] = {"exists": True}
            else:
                detail["chat_history_file"] = {"exists": False}
        except Exception:
            detail["chat_history_file"] = {"exists": False}

        return json_response({"ok": True, "detail": detail})

    # ==================== 会话管理 Handler ====================

    def _clear_session_data(self, session: str) -> list:
        """清理指定会话的运行态数据，返回已清理的模块列表"""
        cleared = []
        session_key, chat_id = self._normalize_session_scope(session)

        # 清除注意力数据
        try:
            from ..utils.attention_manager import AttentionManager

            if session in AttentionManager._attention_map:
                del AttentionManager._attention_map[session]
                cleared.append("attention")
            elif session_key in AttentionManager._attention_map:
                del AttentionManager._attention_map[session_key]
                cleared.append("attention")
        except Exception as e:
            logger.warning(f"🌐 清除注意力数据失败: {e}")

        # 清除主动对话状态
        try:
            from ..utils.proactive_chat_manager import ProactiveChatManager

            if session in ProactiveChatManager._chat_states:
                del ProactiveChatManager._chat_states[session]
                cleared.append("proactive_state")
            elif session_key in ProactiveChatManager._chat_states:
                del ProactiveChatManager._chat_states[session_key]
                cleared.append("proactive_state")
            if session in ProactiveChatManager._temp_probability_boost:
                del ProactiveChatManager._temp_probability_boost[session]
                cleared.append("temp_boost")
            elif session_key in ProactiveChatManager._temp_probability_boost:
                del ProactiveChatManager._temp_probability_boost[session_key]
                cleared.append("temp_boost")
        except Exception as e:
            logger.warning(f"🌐 清除主动对话状态失败: {e}")

        # 清除注意力冷却数据
        try:
            from ..utils.cooldown_manager import CooldownManager

            cooldown_hit = False
            for key in {session, session_key}:
                if (
                    key in CooldownManager._cooldown_map
                    or key in CooldownManager._pending_cooldown_map
                ):
                    CooldownManager._cooldown_map.pop(key, None)
                    CooldownManager._pending_cooldown_map.pop(key, None)
                    cooldown_hit = True
            if cooldown_hit:
                cleared.append("cooldown")
        except Exception as e:
            logger.warning(f"🌐 清除冷却数据失败: {e}")

        # 清除情绪数据（支持三种 key 格式）
        if hasattr(self.plugin, "mood_tracker") and self.plugin.mood_tracker:
            if hasattr(self.plugin.mood_tracker, "moods"):
                for key in {session, session_key, chat_id}:
                    if key and key in self.plugin.mood_tracker.moods:
                        del self.plugin.mood_tracker.moods[key]
                        cleared.append("mood")
                        break

        # 清除频率调整器状态（支持三种 key 格式）
        if (
            hasattr(self.plugin, "frequency_adjuster")
            and self.plugin.frequency_adjuster
        ):
            if hasattr(self.plugin.frequency_adjuster, "check_states"):
                for key in {session, session_key, chat_id}:
                    if key and key in self.plugin.frequency_adjuster.check_states:
                        del self.plugin.frequency_adjuster.check_states[key]
                        cleared.append("frequency")
                        break

        # 清除处理中标记
        if hasattr(self.plugin, "processing_sessions"):
            target_ids = {session, session_key}
            if chat_id:
                target_ids.add(chat_id)
            stale_message_ids = [
                message_id
                for message_id, current_chat_id in self.plugin.processing_sessions.items()
                if current_chat_id in target_ids
            ]
            for message_id in stale_message_ids:
                self.plugin.processing_sessions.pop(message_id, None)

        # 清除主动对话处理中标记
        if hasattr(self.plugin, "proactive_processing_sessions"):
            self.plugin.proactive_processing_sessions.pop(session, None)
            self.plugin.proactive_processing_sessions.pop(session_key, None)

        # 清除待转存消息缓存（尝试多种 key 格式）
        try:
            if hasattr(self.plugin, "pending_messages_cache"):
                for key in {session, session_key, chat_id}:
                    if key and key in self.plugin.pending_messages_cache:
                        del self.plugin.pending_messages_cache[key]
                        cleared.append("pending_messages_cache")
        except Exception as e:
            logger.warning(f"🌐 清除待转存消息缓存失败: {e}")

        # 清除最近回复缓存（尝试多种 key 格式）
        try:
            if hasattr(self.plugin, "recent_replies_cache"):
                for key in {session, session_key, chat_id}:
                    if key and key in self.plugin.recent_replies_cache:
                        del self.plugin.recent_replies_cache[key]
                        cleared.append("recent_replies")
        except Exception as e:
            logger.warning(f"🌐 清除最近回复缓存失败: {e}")

        # 清除等待窗口（key 为 (chat_id, user_id) 元组）
        try:
            if hasattr(self.plugin, "_group_wait_windows"):
                target_ids = {session, session_key}
                if chat_id:
                    target_ids.add(chat_id)
                stale_windows = [
                    wk
                    for wk, _ in self.plugin._group_wait_windows.items()
                    if isinstance(wk, tuple)
                    and len(wk) >= 1
                    and str(wk[0]) in target_ids
                ]
                for wk in stale_windows:
                    self.plugin._group_wait_windows.pop(wk, None)
                if stale_windows:
                    cleared.append("group_wait_windows")
        except Exception as e:
            logger.warning(f"🌐 清除等待窗口失败: {e}")

        # 清除对话活跃度映射
        try:
            from ..utils.attention_manager import AttentionManager

            for key in {session, session_key}:
                if key and key in AttentionManager._conversation_activity_map:
                    del AttentionManager._conversation_activity_map[key]
                    cleared.append("conversation_activity")
                    break
            for key in {session, session_key}:
                if key and key in AttentionManager._fatigue_attention_block:
                    del AttentionManager._fatigue_attention_block[key]
                    cleared.append("fatigue_attention_block")
                    break
        except Exception:
            pass

        return cleared

    async def _handle_session_list(self):
        """列出会话及元数据——使用 compound chat_key 作为 canonical ID。

        不同平台的同一 chat_id 会产生不同的 canonical key，不会错误合并。
        例如 aiocqhttp_group_123456789 和 gewechat_group_123456789 是两个独立的条目。
        """
        sessions: dict[str, dict] = {}

        # 1. 构建 chat_id → compound_keys 映射
        cid_map = self._build_chat_id_to_compound_map()

        # 2. 收集纯 chat_id 运行时数据标记
        raw_runtime = self._collect_all_sessions()
        plain_runtime_cids: set[str] = set()  # 有运行时数据的 chat_id
        for key in raw_runtime:
            plat, ctype, cid = self._split_compound_key(key)
            if plat and ctype:
                pass  # 复合 key，在第 3 步处理
            else:
                plain_runtime_cids.add(cid)

        # 3. 为每个 compound key 创建 canonical 条目
        all_compound_keys: set[str] = set()
        for compounds in cid_map.values():
            all_compound_keys.update(compounds)
        # 也加入运行时中的复合 key（可能未出现在 cid_map 中，例如无文件的新会话）
        for key in raw_runtime:
            plat, ctype, cid = self._split_compound_key(key)
            if plat and ctype:
                all_compound_keys.add(key)

        for ckey in all_compound_keys:
            _, _, cid = self._split_compound_key(ckey)
            has_rt = ckey in raw_runtime or cid in plain_runtime_cids
            if not has_rt and cid in cid_map:
                has_rt = any(ck in raw_runtime for ck in cid_map[cid])
            sessions[ckey] = {
                "message_count": 0,
                "file_size": 0,
                "last_modified": 0,
                "has_file": False,
                "has_runtime_data": has_rt,
            }

        # 4. 纯 chat_id 且无对应 compound key 的孤立运行时条目
        for cid in plain_runtime_cids:
            if cid not in cid_map and cid not in sessions:
                # 防御性检查：跳过无效的 chat_id
                if not cid or not cid.strip() or not _SAFE_SESSION_RE.match(cid):
                    logger.debug(f"🌐 跳过无效的孤立运行时会话 key: {cid!r}")
                    continue
                sessions[cid] = {
                    "message_count": 0,
                    "file_size": 0,
                    "last_modified": 0,
                    "has_file": False,
                    "has_runtime_data": True,
                }

        # 5. 扫描文件，按 chat_id 匹配到 canonical 条目
        chat_dir = self.data_dir / "chat_history"
        if chat_dir.exists():
            for f in chat_dir.rglob("*.json"):
                try:
                    rel = f.relative_to(chat_dir)
                except ValueError:
                    continue
                parts = rel.parts
                if len(parts) == 3:
                    compound_key = f"{parts[0]}_{parts[1]}_{f.stem}"
                    file_cid = f.stem
                elif len(parts) == 1:
                    compound_key = f.stem
                    file_cid = f.stem
                else:
                    continue

                # 找到匹配的 canonical 条目（优先精确 compound key 匹配，其次 chat_id 匹配）
                if compound_key in sessions:
                    target_key = compound_key
                elif file_cid in sessions:
                    target_key = file_cid
                elif file_cid in cid_map:
                    # 文件存在但未直接匹配到 canonical 条目；检查其 chat_id
                    # 是否通过复合 key 关联到了运行时数据
                    target_key = compound_key
                    has_rt = file_cid in plain_runtime_cids
                    if not has_rt:
                        for ck in cid_map.get(file_cid, []):
                            if ck in raw_runtime:
                                has_rt = True
                                break
                    sessions.setdefault(
                        target_key,
                        {
                            "message_count": 0,
                            "file_size": 0,
                            "last_modified": 0,
                            "has_file": True,
                            "has_runtime_data": has_rt,
                        },
                    )
                else:
                    target_key = compound_key
                    sessions.setdefault(
                        target_key,
                        {
                            "message_count": 0,
                            "file_size": 0,
                            "last_modified": 0,
                            "has_file": True,
                            "has_runtime_data": False,
                        },
                    )

                try:
                    stat = f.stat()
                    with open(f, "r", encoding="utf-8") as fh:
                        data = json.load(fh)
                    msg_count = len(data) if isinstance(data, list) else 0
                    sessions[target_key].update(
                        {
                            "message_count": msg_count,
                            "file_size": stat.st_size,
                            "last_modified": stat.st_mtime,
                            "has_file": True,
                        }
                    )
                except Exception as e:
                    logger.debug(f"🌐 读取聊天记录文件 {f.name} 失败: {e}")
                    sessions[target_key].update(
                        {
                            "has_file": True,
                            "error": True,
                        }
                    )

        return json_response({"ok": True, "sessions": sessions})

    async def _handle_clean_ghost_sessions(self):
        """清理孤立会话文件（没有对应运行时状态的聊天记录文件）"""
        raw_runtime = self._collect_all_sessions()
        # 规范化为 chat_id 集合以便精确匹配
        in_memory_cids = {self._extract_chat_id(k) for k in raw_runtime}
        chat_dir = self.data_dir / "chat_history"
        if not chat_dir.exists():
            return json_response({"ok": True, "msg": "无聊天记录目录"})

        deleted = 0
        skipped_no_runtime = 0
        skipped_path_fail = 0
        total_files = 0
        for f in chat_dir.rglob("*.json"):
            total_files += 1
            try:
                rel = f.relative_to(chat_dir)
            except ValueError:
                continue
            parts = rel.parts
            if len(parts) == 3:
                chat_id = f.stem
            elif len(parts) == 1:
                chat_id = f.stem
            else:
                continue
            # 只删除确实没有运行时状态的孤立文件
            if chat_id not in in_memory_cids:
                # 安全检查：确保文件在 chat_history 目录内
                rel_str = rel.as_posix()
                resolved = self._get_path_under_root(chat_dir, rel_str)
                if resolved is None:
                    logger.warning(f"🌐 清理孤立文件路径校验失败: rel={rel_str!r}")
                    skipped_path_fail += 1
                    continue
                if not resolved.exists():
                    logger.warning(f"🌐 清理孤立文件不存在(解析后): {resolved}")
                    skipped_path_fail += 1
                    continue
                try:
                    resolved.unlink()
                    deleted += 1
                    logger.info(f"🌐 已清理孤立会话文件: {rel_str}")
                except Exception as e:
                    logger.warning(f"🌐 清理孤立会话文件失败 {f}: {e}")
            else:
                skipped_no_runtime += 1

        logger.info(
            f"🌐 清理孤立会话完成: total={total_files} deleted={deleted} "
            f"skipped_no_runtime={skipped_no_runtime} skipped_path_fail={skipped_path_fail} "
            f"in_memory_cids={sorted(in_memory_cids)[:20]}"
        )
        return json_response(
            {"ok": True, "msg": f"已清理 {deleted} 个孤立会话文件"}
        )

    async def _handle_session_reset(self, session: str):
        """重置会话数据（仅清除运行时状态，不删除文件，不设历史截止点）。"""
        session_ok, session_msg = self._require_known_session(session)
        if not session_ok:
            status = 400 if "无效" in session_msg else 404
            return error_response(session_msg, status_code=status)
        cleared = self._clear_session_data(session)

        # 触发插件重载（使内存状态清理完全生效）
        self._create_deferred_reload_task()

        return json_response(
            {
                "ok": True,
                "msg": f"已清除会话 {session} 的数据，插件重载中...",
                "cleared": cleared,
            }
        )

    async def _handle_clear_image_cache(self):
        """清除图片描述缓存"""
        cleared = self._clear_image_cache_storage()
        return json_response(
            {"ok": True, "msg": "图片缓存已清除" if cleared else "无缓存文件"}
        )

    def _get_chat_history_path(self, session: str) -> Path | None:
        """获取聊天记录文件路径（含路径遍历防护）

        支持两种存储结构：
        1. 嵌套目录: chat_history/{platform}/{chat_type}/{chat_id}.json
        2. 平面文件: chat_history/{session}.json（旧兼容）

        当 session 为纯 chat_id 时，会搜索子目录定位文件。
        """
        if not session or not _SAFE_SESSION_RE.match(session):
            return None
        safe_dir = (self.data_dir / "chat_history").resolve()

        # 尝试解析 session 名为嵌套路径:
        # aiocqhttp_group_123456789 → aiocqhttp/group/123456789.json
        parts = session.split("_", 2)
        nested_path = None
        if len(parts) >= 3:
            platform, chat_type, chat_id = parts[0], parts[1], parts[2]
            nested_rel = f"{platform}/{chat_type}/{chat_id}.json"
            nested_path = self._get_path_under_root(safe_dir, nested_rel)
            if nested_path is not None and nested_path.exists():
                return nested_path

        # 兼容：检查平面路径
        flat_path = self._get_path_under_root(safe_dir, f"{session}.json")
        if flat_path is not None and flat_path.exists():
            return flat_path

        # 纯 chat_id 格式：搜索子目录定位文件
        # chat_id 已通过 _SAFE_SESSION_RE 校验，不含 .. 或 /
        if len(parts) < 3 and safe_dir.exists():
            for child in safe_dir.iterdir():
                if not child.is_dir():
                    continue
                for sub in ("group", "private"):
                    candidate = child / sub / f"{session}.json"
                    try:
                        candidate = candidate.resolve()
                        if (
                            self._is_path_within_root(safe_dir, candidate)
                            and candidate.exists()
                        ):
                            return candidate
                    except Exception:
                        continue
            # 纯 chat_id 无已有文件：不返回路径，防止在错误位置创建 flat 文件
            # ContextManager 会在消息首次保存时创建正确的嵌套路径
            return None

        # 文件不存在时，优先返回嵌套路径（与 ContextManager 一致）
        if nested_path is not None:
            return nested_path
        return flat_path

    async def _handle_get_chat_history(self, session: str):
        """查看自定义存储聊天记录"""
        session_ok, session_msg = self._require_known_session(session)
        if not session_ok:
            status = 400 if "无效" in session_msg else 404
            return error_response(session_msg, status_code=status)

        path = self._get_chat_history_path(session)
        if path is None:
            return error_response("无效的会话名称", status_code=400)

        if not path.exists():
            return json_response({"ok": True, "messages": []})

        try:
            with open(path, "r", encoding="utf-8") as f:
                messages = json.load(f)
            valid, error_msg = self._validate_chat_history_messages(messages)
            if not valid:
                logger.warning(f"🌐 聊天记录结构异常 [{session}]: {error_msg}")
                return error_response("聊天记录文件结构异常，请先在服务器本地检查后再处理", status_code=500)
            return json_response({"ok": True, "messages": messages})
        except Exception as e:
            logger.error(f"🌐 读取聊天记录 [{session}] 失败: {e}", exc_info=True)
            return error_response("读取失败", status_code=500)

    async def _handle_put_chat_history(self, session: str):
        """编辑聊天记录"""
        session_ok, session_msg = self._require_known_session(session)
        if not session_ok:
            status = 400 if "无效" in session_msg else 404
            return error_response(session_msg, status_code=status)
        try:
            body = await request.json(default={})
        except Exception:
            return error_response("无效请求", status_code=400)

        messages = body.get("messages")
        valid, error_msg = self._validate_chat_history_messages(messages)
        if not valid:
            return error_response(error_msg, status_code=400)

        path = self._get_chat_history_path(session)
        if path is None:
            return error_response("无效的会话名称", status_code=400)
        path.parent.mkdir(parents=True, exist_ok=True)

        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(messages, f, ensure_ascii=False, indent=2)
            return json_response(
                {
                    "ok": True,
                    "msg": f"已保存 {len(messages)} 条消息",
                }
            )
        except Exception as e:
            logger.error(f"🌐 保存聊天记录 [{session}] 失败: {e}", exc_info=True)
            return error_response("保存失败", status_code=500)

    async def _handle_get_image_cache(self):
        """查看图片描述缓存"""
        cache_file = self._resolve_image_cache_file()
        if cache_file is None or not cache_file.exists():
            return json_response({"ok": True, "cache": {}, "count": 0})

        try:
            if cache_file.suffix.lower() == ".jsonl":
                cache = {}
                count = 0
                with open(cache_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        url = entry.get("u")
                        desc = entry.get("d")
                        if not url or not desc:
                            continue
                        cache[url] = desc
                        count += 1
                return json_response(
                    {
                        "ok": True,
                        "cache": cache,
                        "count": count,
                    }
                )

            with open(cache_file, "r", encoding="utf-8") as f:
                cache = json.load(f)
            return json_response(
                {
                    "ok": True,
                    "cache": cache,
                    "count": len(cache) if isinstance(cache, dict) else 0,
                }
            )
        except Exception as e:
            logger.error(f"🌐 读取图片缓存失败: {e}", exc_info=True)
            return error_response("读取缓存失败，请查看日志", status_code=500)

    # ==================== 指令执行 Handler ====================


    async def _handle_cmd_reset(self):
        """从 Web 端执行 gcp_reset（全局重置）"""
        try:
            body = await request.json(default={})
        except Exception:
            body = {}
        restart_mode = body.get("restart_mode", "reload")

        try:
            if hasattr(self.plugin, "_reset_plugin_data_and_reload"):
                await self.plugin._reset_plugin_data_and_reload()

            if restart_mode == "restart":
                self._create_deferred_restart_task()
                is_desktop = getattr(self.plugin, "is_desktop_mode", False)
                msg = "插件已重置，AstrBot 重启中..."
                if is_desktop:
                    msg += "（桌面端：如重启后无响应，请通过托盘菜单手动重启）"
                return json_response(
                    {
                        "ok": True,
                        "msg": msg,
                        "is_desktop": is_desktop,
                    }
                )
            else:
                self._create_deferred_reload_task()
                return json_response(
                    {
                        "ok": True,
                        "msg": "插件已重置，正在重载...",
                    }
                )
        except Exception as e:
            logger.error(f"🌐 执行 gcp_reset 失败: {e}", exc_info=True)
            return error_response("执行重置操作失败，请查看日志", status_code=500)

    async def _handle_cmd_reset_here(self):
        """从 Web 端执行 gcp_reset_here（指定会话重置）"""
        try:
            body = await request.json(default={})
        except Exception:
            return error_response("无效请求", status_code=400)

        session_id = body.get("session_id", "")
        restart_mode = body.get("restart_mode", "reload")
        session_ok, session_msg = self._require_known_session(session_id)
        if not session_ok:
            status = 400 if "无效" in session_msg else 404
            return error_response(session_msg, status_code=status)

        try:
            cleared = self._clear_session_data(session_id)

            # 提取 chat_id 并设置历史截止时间戳
            try:
                from ..utils.context_manager import ContextManager
                import re

                parts = re.split(r"[:_]", session_id)
                chat_id = parts[-1] if len(parts) >= 3 else session_id
                if chat_id:
                    ContextManager.set_history_cutoff(chat_id)
                    cleared.append("history_cutoff")
                    logger.info(
                        "🌐 [Web指令-会话重置] 已设置历史截止时间戳 chat_id=%s", chat_id
                    )
            except Exception as e:
                logger.warning(f"🌐 设置历史截止点失败: {e}")

            # 删除会话的聊天历史文件
            history_path = self._get_chat_history_path(session_id)
            if history_path is not None and history_path.exists():
                history_path.unlink()
                cleared.append("chat_history_file")

            if restart_mode == "restart":
                self._create_deferred_restart_task()
                return json_response(
                    {
                        "ok": True,
                        "msg": f"会话 {session_id} 已重置，AstrBot 重启中...",
                        "cleared": cleared,
                    }
                )
            else:
                self._create_deferred_reload_task()
                return json_response(
                    {
                        "ok": True,
                        "msg": f"会话 {session_id} 已重置，插件重载中...",
                        "cleared": cleared,
                    }
                )
        except Exception as e:
            logger.error(f"🌐 执行 gcp_reset_here 失败: {e}", exc_info=True)
            return error_response("执行会话重置失败，请查看日志", status_code=500)

    async def _handle_cmd_clear_image_cache(self):
        """从 Web 端执行 gcp_clear_image_cache"""
        try:
            body = await request.json(default={})
        except Exception:
            body = {}
        restart_mode = body.get("restart_mode", "reload")

        try:
            count = 0
            if (
                hasattr(self.plugin, "image_description_cache")
                and self.plugin.image_description_cache
            ):
                stats = self.plugin.image_description_cache.get_stats()
                count = stats.get("entry_count", 0)
                self.plugin.image_description_cache.clear()

            cleared_file = self._clear_image_cache_storage()
            if not count and cleared_file:
                count = -1

            if restart_mode == "restart":
                self._create_deferred_restart_task()
                msg = (
                    f"已清除 {count} 条缓存，AstrBot 重启中..."
                    if count >= 0
                    else "图片缓存已清除，AstrBot 重启中..."
                )
                return json_response(
                    {
                        "ok": True,
                        "msg": msg,
                    }
                )
            else:
                self._create_deferred_reload_task()
                msg = (
                    f"已清除 {count} 条缓存，插件重载中..."
                    if count >= 0
                    else "图片缓存已清除，插件重载中..."
                )
                return json_response(
                    {
                        "ok": True,
                        "msg": msg,
                    }
                )
        except Exception as e:
            logger.error(f"🌐 执行 clear_image_cache 失败: {e}", exc_info=True)
            return error_response("清除缓存失败，请查看日志", status_code=500)

    async def _handle_restart_status(self):
        """返回当前重启/重载操作状态（供前端轮询）。"""
        return json_response({"ok": True, **dict(self._restart_status)})

    # ==================== 文件管理 Handler ====================

    # 安全路径校验正则：仅允许安全字符
    _SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9_\-!./]+$")

    # 敏感文件：禁止在 Web 端读取、编辑、删除（防止会话/运行状态数据被篡改）
    _SENSITIVE_FILES = {"sessions.json"}
    _TEXT_FILE_SUFFIXES = {
        ".json",
        ".jsonl",
        ".txt",
        ".log",
        ".md",
        ".yaml",
        ".yml",
        ".ini",
        ".cfg",
        ".conf",
        ".toml",
        ".csv",
        ".env",
    }


    def _validate_file_path(self, rel_path: str) -> Path | None:
        """校验文件路径安全性，返回绝对路径或 None

        - 禁止 .. 路径遍历
        - 必须在数据目录内
        - 仅允许安全字符
        """
        return self._get_path_under_root(self.data_dir, rel_path)

    def _is_protected_file(self, filename: str) -> bool:
        """检查文件名是否属于受保护的敏感文件（大小写不敏感，防止 NTFS 大小写绕过）"""
        return filename.lower() in self._SENSITIVE_FILES

    def _is_probably_text_file(self, target: Path) -> bool:
        """判断文件是否适合在线文本查看/编辑"""
        if target.suffix.lower() in self._TEXT_FILE_SUFFIXES:
            return True
        try:
            with open(target, "rb") as f:
                chunk = f.read(4096)
        except Exception:
            return False
        if not chunk:
            return True
        if b"\x00" in chunk:
            return False
        try:
            chunk.decode("utf-8")
            return True
        except UnicodeDecodeError:
            return False

    def _build_file_capabilities(self, target: Path) -> dict:
        """根据文件类型和保护规则生成前端可用能力（含符号链接绕过防护）"""
        is_protected = self._is_protected_file(target.name)
        if not is_protected:
            # 防止符号链接绕过：检查 resolve 后的真实文件名
            try:
                resolved = target.resolve()
                if resolved.name != target.name:
                    is_protected = self._is_protected_file(resolved.name)
            except Exception:
                pass
        is_json = target.suffix.lower() == ".json"
        is_text = False if is_protected else self._is_probably_text_file(target)
        can_read = not is_protected and is_text
        can_edit = can_read
        can_delete = not is_protected
        status = (
            "protected" if is_protected else ("editable" if can_edit else "delete_only")
        )
        return {
            "protected": is_protected,
            "is_json": is_json,
            "is_text": is_text,
            "can_read": can_read,
            "can_edit": can_edit,
            "can_delete": can_delete,
            "status": status,
        }

    async def _handle_file_list(self):
        """列出数据目录下所有文件"""
        data_dir = self.data_dir
        files = []

        if not data_dir.exists():
            return json_response({"ok": True, "files": []})

        try:
            for item in sorted(data_dir.rglob("*")):
                if not item.is_file():
                    continue
                # 跳过 __pycache__ 等
                rel = item.relative_to(data_dir)
                if any(p.startswith("__") for p in rel.parts):
                    continue
                try:
                    stat = item.stat()
                    capabilities = self._build_file_capabilities(item)
                    files.append(
                        {
                            "path": str(rel).replace("\\", "/"),
                            "name": item.name,
                            "directory": str(rel.parent).replace("\\", "/")
                            if str(rel.parent) != "."
                            else "",
                            "size": stat.st_size,
                            "modified": stat.st_mtime,
                            **capabilities,
                        }
                    )
                except OSError as e:
                    logger.debug(f"🌐 获取文件 {item} 信息失败: {e}")
                    continue
        except Exception as e:
            logger.warning(f"🌐 扫描数据目录失败: {e}")

        return json_response({"ok": True, "files": files})

    async def _handle_file_read(self):
        """读取指定文件内容"""
        rel_path = request.query.get("path", "")
        target = self._validate_file_path(rel_path)
        if target is None:
            return error_response("无效的文件路径", status_code=400)
        if not target.exists():
            return error_response("文件不存在", status_code=404)

        capabilities = self._build_file_capabilities(target)
        if not capabilities["can_read"]:
            if capabilities["protected"]:
                logger.warning(f"🌐 已拒绝读取受保护文件: {rel_path}")
                return error_response(self._build_protected_file_message("read"), status_code=403)
            return error_response("此文件不是可在线查看的文本文件", status_code=415)

        try:
            size = target.stat().st_size
            if size > 5 * 1024 * 1024:
                return error_response("文件过大（超过 5MB），无法在线查看", status_code=413)
        except OSError as e:
            logger.debug(f"🌐 获取文件大小失败 [{target}]: {e}")

        try:
            with open(target, "r", encoding="utf-8") as f:
                content = f.read()
            parsed = None
            if capabilities["is_json"]:
                try:
                    parsed = json.loads(content)
                except json.JSONDecodeError:
                    pass
            return json_response(
                {
                    "ok": True,
                    "path": rel_path,
                    "content": content,
                    "parsed": parsed,
                    **capabilities,
                }
            )
        except UnicodeDecodeError:
            return error_response("文件非文本格式，无法读取", status_code=415)
        except Exception as e:
            logger.error(f"🌐 读取文件失败 [{rel_path}]: {e}", exc_info=True)
            return error_response("读取文件失败", status_code=500)

    async def _handle_file_save(self):
        """保存文件内容"""
        try:
            body = await request.json(default={})
        except Exception:
            return error_response("无效请求", status_code=400)

        rel_path = body.get("path", "")
        content = body.get("content", "")
        target = self._validate_file_path(rel_path)

        if target is None:
            return error_response("无效的文件路径", status_code=400)
        if target.exists() and not target.is_file():
            return error_response("目标路径不是普通文件", status_code=400)

        capabilities = self._build_file_capabilities(target)
        if not capabilities["can_edit"]:
            if capabilities["protected"]:
                logger.warning(f"🌐 已拒绝修改受保护文件: {rel_path}")
                return error_response(self._build_protected_file_message("save"), status_code=403)
            return error_response("此文件不是可在线编辑的文本文件", status_code=403)

        parsed = None
        if capabilities["is_json"]:
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError as e:
                return error_response(f"JSON 格式错误: {e}", status_code=400)

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                if capabilities["is_json"]:
                    json.dump(parsed, f, ensure_ascii=False, indent=2)
                else:
                    f.write(content)
            logger.info(f"🌐 文件已保存: {rel_path}")
            return json_response(
                {
                    "ok": True,
                    "msg": f"文件已保存: {rel_path}",
                    **capabilities,
                }
            )
        except Exception as e:
            logger.error(f"🌐 保存文件失败 [{rel_path}]: {e}", exc_info=True)
            return error_response("保存文件失败", status_code=500)

    async def _handle_file_delete(self):
        """删除文件"""
        try:
            body = await request.json(default={})
        except Exception:
            return error_response("无效请求", status_code=400)

        rel_path = body.get("path", "")
        target = self._validate_file_path(rel_path)

        if target is None:
            return error_response("无效的文件路径", status_code=400)
        if not target.exists():
            return error_response("文件不存在", status_code=404)
        # 禁止删除认证和封禁文件（含符号链接绕过防护）
        protected = self._is_protected_file(target.name)
        if not protected:
            try:
                resolved = target.resolve()
                if resolved.name != target.name:
                    protected = self._is_protected_file(resolved.name)
            except Exception:
                pass
        if protected:
            logger.warning(f"🌐 已拒绝删除受保护文件: {rel_path}")
            return error_response(self._build_protected_file_message("delete"), status_code=403)

        try:
            target.unlink()
            logger.info(f"🌐 文件已删除: {rel_path}")
            return json_response(
                {
                    "ok": True,
                    "msg": f"文件已删除: {rel_path}",
                }
            )
        except Exception as e:
            logger.error(f"🌐 删除文件失败 [{rel_path}]: {e}", exc_info=True)
            return error_response("删除文件失败", status_code=500)
