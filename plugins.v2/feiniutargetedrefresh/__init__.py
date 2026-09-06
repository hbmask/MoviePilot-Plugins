# plugins.v2/feiniutargetedrefresh/__init__.py
"""飞牛定向刷新插件。

入库后按「MoviePilot 二级分类/二级目录 → 飞牛站点」的手动映射，
复用系统「媒体服务器」中已登录的飞牛影视连接，调用 POST /mdb/scan/{guid}
定向刷新对应的飞牛媒体库。无第三方依赖，不自行登录飞牛。
"""
import threading
import time
from typing import Any, Dict, List, Tuple

from app.core.event import eventmanager, Event
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType


class FeiniuTargetedRefresh(_PluginBase):
    # 插件展示名称
    plugin_name = "飞牛定向刷新"
    # 插件描述
    plugin_desc = "入库后自动刷新飞牛影视对应的媒体库（手动映射二级分类→站点）。"
    # 插件图标
    plugin_icon = "refresh2.png"
    # 插件版本（必须与 package.v2.json 一致）
    plugin_version = "1.0.0"
    # 插件作者
    plugin_author = "hbmask"
    # 作者主页
    author_url = "https://github.com/hbmask"
    # 配置项前缀
    plugin_config_prefix = "feiniutargetedrefresh_"
    # 加载顺序
    plugin_order = 14
    # 用户可见级别
    auth_level = 1

    # 运行时状态
    _enabled = False
    _mediaserver = ""
    _delay = 0
    _mappings = []     # [{'site': '站点名或guid', 'cats': ['分类1', ...]}]
    _fallback = "skip"

    _pending = {}      # 待扫描集合：site -> True
    _draining = False
    _lock = threading.Lock()

    def init_plugin(self, config: dict = None):
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._mediaserver = config.get("mediaserver") or ""
        try:
            self._delay = int(config.get("delay") or 0)
        except Exception:
            self._delay = 0
        self._mappings = self._parse_mappings(config.get("mappings") or "")
        self._fallback = config.get("fallback") or "skip"

    @staticmethod
    def _parse_mappings(text: str) -> List[Dict[str, Any]]:
        """
        解析映射文本。每行一条：
            站点名或guid = 分类1,分类2,分类3
        '#' 开头为注释，空行忽略。站点的分类列表逗号分隔。
        """
        rows = []
        for line in (text or "").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line and "：" not in line:
                continue
            sep = "=" if "=" in line else "："
            left, right = line.split(sep, 1)
            site = left.strip()
            cats = [c.strip() for c in right.replace("，", ",").split(",") if c.strip()]
            if site and cats:
                rows.append({"site": site, "cats": cats})
        return rows

    @staticmethod
    def _is_guid(s: str) -> bool:
        s = (s or "").lower()
        return len(s) == 32 and all(c in "0123456789abcdef" for c in s)

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/libraries",
                "endpoint": self.api_libraries,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取飞牛媒体库站点列表",
                "description": "返回站点名与 guid，用于填写映射",
            },
            {
                "path": "/scan_all",
                "endpoint": self.api_scan_all,
                "methods": ["POST"],
                "auth": "apikey",
                "summary": "手动全量扫描飞牛所有媒体库",
                "description": "配置完成后人工验证用",
            },
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        # 媒体服务器下拉：复用系统里已配置的媒体服务器
        try:
            server_items = [
                {"title": config.name, "value": config.name}
                for config in MediaServerHelper().get_configs().values()
            ]
        except Exception:
            server_items = []

        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enabled", "label": "启用插件"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "mediaserver",
                                            "label": "飞牛媒体服务器",
                                            "items": server_items,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "delay",
                                            "label": "延迟合并（秒）",
                                            "placeholder": "0",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "fallback",
                                            "label": "未命中兜底",
                                            "items": [
                                                {"title": "跳过不刷", "value": "skip"},
                                                {"title": "全量扫描", "value": "scanall"},
                                            ],
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "mappings",
                                            "label": "站点映射（每行一条）",
                                            "rows": 8,
                                            "placeholder": "电影=华语电影,外语电影,动画电影\n动漫=国漫,日番\n国产=国产\n岛国=岛国",
                                            "hint": "格式：飞牛站点名或guid = MoviePilot二级分类1,分类2,...（逗号分隔，可用全角逗号）。# 开头为注释。站点名与 guid 见插件详情页。",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "mediaserver": "",
            "delay": 0,
            "mappings": "",
            "fallback": "skip",
        }

    def get_page(self) -> List[dict]:
        lines = self._library_lines()
        return [
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "text": "入库后按映射调用飞牛 /mdb/scan/{guid} 定向刷新对应媒体库；未命中按兜底策略（跳过/全量）。"
                           "映射里左边可填站点名或 guid，右边填 MoviePilot 二级分类（逗号分隔）。",
                },
            },
            {
                "component": "VList",
                "props": {},
                "content": [
                    {
                        "component": "VListItem",
                        "props": {"title": line, "dense": True},
                    }
                    for line in lines
                ],
            },
        ]

    # ------------------------------------------------------------------
    # 复用系统媒体服务器连接
    # ------------------------------------------------------------------
    def _get_instance(self):
        if not self._mediaserver:
            logger.warning("[飞牛定向刷新] 未选择飞牛媒体服务器")
            return None
        try:
            services = MediaServerHelper().get_services(name_filters=[self._mediaserver])
        except Exception as e:
            logger.error(f"[飞牛定向刷新] 获取媒体服务器实例失败：{e}")
            return None
        if not services:
            logger.warning(f"[飞牛定向刷新] 未找到媒体服务器 {self._mediaserver}")
            return None
        info = list(services.values())[0]
        inst = getattr(info, "instance", None)
        if not inst:
            return None
        try:
            if inst.is_inactive():
                inst.reconnect()
        except Exception:
            pass
        return inst

    def _library_lines(self) -> List[str]:
        inst = self._get_instance()
        if not inst:
            return ["（未选择飞牛媒体服务器，或未连接）"]
        try:
            libs = inst.api.mediadb_list() if getattr(inst, "api", None) else None
        except Exception:
            libs = None
        if not libs:
            return ["（无法拉取飞牛媒体库列表）"]
        return [f"{lib.name}  →  {lib.guid}" for lib in libs]

    def _resolve_mediadb(self, site: str):
        inst = self._get_instance()
        if not inst:
            return None
        try:
            libs = inst.api.mediadb_list()
        except Exception as e:
            logger.error(f"[飞牛定向刷新] 拉取媒体库列表失败：{e}")
            return None
        if self._is_guid(site):
            for lib in libs:
                if lib.guid == site:
                    return lib
        for lib in libs:
            if lib.name == site:
                return lib
        return None

    # ------------------------------------------------------------------
    # 事件处理
    # ------------------------------------------------------------------
    @eventmanager.register(EventType.TransferComplete)
    def refresh(self, event: Event):
        if not self._enabled:
            return
        try:
            event_data = event.event_data or {}
            mediainfo = event_data.get("mediainfo")
            if not mediainfo:
                return
            category = getattr(mediainfo, "category", None) or ""

            # 二级目录名：取 target_diritem 的 name / basename / 路径末段
            dirname = ""
            transferinfo = event_data.get("transferinfo")
            if transferinfo:
                d = getattr(transferinfo, "target_diritem", None)
                if d:
                    dirname = getattr(d, "name", "") or getattr(d, "basename", "") or ""
                    if not dirname:
                        p = getattr(d, "path", None)
                        if p:
                            dirname = str(p).rstrip("/").split("/")[-1]

            site = None
            for m in self._mappings:
                if category in m["cats"] or (dirname and dirname in m["cats"]):
                    site = m["site"]
                    break

            if not site:
                if self._fallback == "scanall":
                    self._schedule("__all__", "全量")
                else:
                    logger.info(
                        f"[飞牛定向刷新] 未命中映射 category={category} dirname={dirname}，按配置跳过")
                return

            self._schedule(site, site)
        except Exception as e:
            logger.error(f"[飞牛定向刷新] 处理异常：{e}")

    # ------------------------------------------------------------------
    # debounce + 扫描
    # ------------------------------------------------------------------
    def _schedule(self, key: str, label: str):
        with self._lock:
            self._pending[key] = label
            if self._draining:
                return
            self._draining = True
        delay = max(0, self._delay)
        threading.Thread(target=self._drain, args=(delay,), daemon=True).start()

    def _drain(self, delay: int):
        if delay:
            time.sleep(delay)
        with self._lock:
            pending = dict(self._pending)
            self._pending.clear()
            self._draining = False
        for key, label in pending.items():
            self._do_scan(key, label)

    def _do_scan(self, key: str, label: str):
        inst = self._get_instance()
        if not inst:
            return
        try:
            if key == "__all__":
                ok = inst.refresh_root_library()
                logger.info(f"[飞牛定向刷新] 全量扫描结果：{ok}")
                return
            lib = self._resolve_mediadb(key)
            if not lib:
                logger.warning(f"[飞牛定向刷新] 站点 '{key}' 未找到对应媒体库")
                return
            # 必须先 task_running，避免飞牛误报 -14 Task duplicate
            getattr(inst.api, "task_running", lambda: None)()
            ok = inst.api.mdb_scan(lib)
            logger.info(f"[飞牛定向刷新] 扫描媒体库 '{lib.name}'({lib.guid}) 结果：{ok}")
        except Exception as e:
            logger.error(f"[飞牛定向刷新] 扫描失败 '{label}'：{e}")

    # ------------------------------------------------------------------
    # 插件 API
    # ------------------------------------------------------------------
    def api_libraries(self):
        inst = self._get_instance()
        if not inst:
            return {"code": -1, "msg": "未选择飞牛媒体服务器或未连接"}
        try:
            libs = inst.api.mediadb_list()
        except Exception as e:
            return {"code": -1, "msg": f"拉取失败：{e}"}
        return {"code": 0, "data": [{"name": lib.name, "guid": lib.guid} for lib in libs]}

    def api_scan_all(self):
        self._schedule("__all__", "全量")
        return {"code": 0, "msg": "已提交全量扫描"}

    def stop_service(self):
        pass