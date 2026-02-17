import os
import json
import asyncio
import hashlib
import time
from pathlib import Path
from collections import OrderedDict

import regex
import aiohttp

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api import logger

# ===== 常量 =====
HARDCODED_DATES = [
    "20251029", "20250501", "20250430", "20250204", "20250130",
    "20241023", "20241021", "20240610", "20240530", "20240214",
    "20240206", "20231128", "20231113", "20230821", "20230818",
    "20230803", "20230426", "20230418", "20230301", "20230216",
    "20230127", "20230126", "20221107", "20221101", "20220815",
    "20220506", "20220406", "20220203", "20220110", "20211115",
    "20210831", "20210521", "20210218", "20201001",
]

EMOJI_PATTERN = regex.compile(
    r'\p{Extended_Pictographic}'
    r'(?:\ufe0f|\ufe0e)?'
    r'(?:[\U0001F3FB-\U0001F3FF])?'
    r'(?:\u200d\p{Extended_Pictographic}'
    r'(?:\ufe0f|\ufe0e)?'
    r'(?:[\U0001F3FB-\U0001F3FF])?)*',
    regex.UNICODE
)

# ===== 异常 =====
class RateLimitError(Exception):
    """CDN 限流异常，需要立即停止探测"""
    pass

# ===== 工具函数 =====
def emoji_to_codepoint(emoji_str: str) -> str:
    """将 emoji 字符串转为 codepoint 格式。
    😀 → '1f600', ❤️ → '2764-fe0f'
    """
    return "-".join(f"{ord(c):x}" for c in emoji_str)

def codepoint_to_url_segment(cp: str) -> str:
    """将 codepoint 转为 URL 路径段。
    '1f600' → 'u1f600', '2764-fe0f' → 'u2764-ufe0f'
    """
    return "-".join(f"u{part}" for part in cp.split("-"))

def make_cache_key(cp1: str, cp2: str) -> str:
    """生成排序后的缓存 key。保证 A+B 与 B+A 命中同一缓存。"""
    return "_".join(sorted([cp1, cp2]))

def _parse_combinations(data: dict) -> dict[str, str]:
    """从元数据 JSON 中解析组合索引。返回 {partner_cp: date}"""
    index_entry = {}
    combinations = data.get("combinations", {}) if isinstance(data, dict) else {}
    for partner_cp, combo_list in combinations.items():
        if not isinstance(combo_list, list) or not combo_list:
            continue
        chosen = None
        for item in combo_list:
            if not isinstance(item, dict):
                continue
            if item.get("isLatest"):
                chosen = item
                break
        if chosen is None:
            chosen = combo_list[0] if isinstance(combo_list[0], dict) else None
        if chosen is None:
            continue
        date = chosen.get("date", "")
        if date:
            index_entry[partner_cp] = date
    return index_entry

# ===== 插件主类 =====
@register("astrbot_plugin_emoji_kitchen", "monkeyray", "发送两个 emoji 自动合成 Google Emoji Kitchen 图片", "1.1.0")
class EmojiKitchenPlugin(Star):
    _MAX_LOCKS = 1024
    _CONCURRENT_LIMIT = 4

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.data_dir: Path = Path("")
        self.cache_dir: Path = Path("")
        self.notfound_dir: Path = Path("")
        self.dates_cache_path: Path = Path("")
        self.date_list: list[str] = []
        self._locks: OrderedDict[str, asyncio.Lock] = OrderedDict()
        self._global_lock = asyncio.Lock()
        self._session_lock = asyncio.Lock()
        self._session: aiohttp.ClientSession | None = None
        self._semaphore: asyncio.Semaphore | None = None
        self._update_task: asyncio.Task | None = None
        self.metadata_dir: Path = Path("")
        self.metadata_index: dict[str, dict[str, str]] = {}  # {emoji_cp: {partner_cp: date}}

    async def initialize(self):
        """插件初始化：创建目录、加载配置、启动日期更新"""
        self.data_dir = StarTools.get_data_dir()
        self.cache_dir = self.data_dir / "cache"
        self.notfound_dir = self.data_dir / "notfound"
        self.dates_cache_path = self.data_dir / "dates_cache.json"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.notfound_dir.mkdir(parents=True, exist_ok=True)
        self.metadata_dir = self.data_dir / "metadata"
        self.metadata_dir.mkdir(parents=True, exist_ok=True)
        self._load_metadata_index()
        # 初始化 semaphore
        self._semaphore = asyncio.Semaphore(self._CONCURRENT_LIMIT)
        # 预创建 session
        await self._ensure_session()
        # 加载日期列表
        self._load_date_list()
        # 异步更新远程日期（不阻塞初始化）
        self._update_task = asyncio.create_task(self._update_dates_from_remote())
        logger.info(f"Emoji Kitchen 插件初始化完成，日期列表: {len(self.date_list)} 个")

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """确保 session 单例存在（双重检查锁）"""
        if self._session and not self._session.closed:
            return self._session
        async with self._session_lock:
            if self._session and not self._session.closed:
                return self._session
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=20, ttl_dns_cache=300)
            )
            return self._session

    def _get_config(self, key: str, default=None):
        """从插件配置中获取值"""
        if self.config and key in self.config:
            return self.config[key]
        return default

    def _get_config_int(self, key: str, default: int) -> int:
        """从插件配置中获取整数值，类型异常时返回默认值"""
        val = self._get_config(key, default)
        try:
            return int(val)
        except (TypeError, ValueError):
            return default

    def _resolve_cdn_url(self) -> str:
        """解析实际的 CDN 地址：优先 cdn_source 预设，自定义时用 cdn_url"""
        source = str(self._get_config("cdn_source", "") or "")
        if source.startswith("www.gstatic.cn"):
            return "https://www.gstatic.cn"
        elif source.startswith("www.gstatic.com"):
            return "https://www.gstatic.com"
        elif source == "自定义":
            custom = str(self._get_config("cdn_url", "") or "").strip().rstrip("/")
            if custom:
                return custom
        elif not source:
            # 兼容旧配置：cdn_source 为空时检查旧的 cdn_url 字段
            legacy = str(self._get_config("cdn_url", "") or "").strip().rstrip("/")
            if legacy:
                return legacy
        # 默认（空或未识别）：返回 gstatic.cn
        return "https://www.gstatic.cn"

    def _resolve_github_proxy(self) -> str:
        """解析实际的 GitHub 代理地址：返回代理 URL 或空字符串（直连）"""
        source = str(self._get_config("github_proxy_source", "") or "")
        if source.startswith("ghfast.top"):
            return "https://ghfast.top"
        elif source.startswith("gh-proxy.com"):
            return "https://gh-proxy.com"
        elif source == "自定义":
            custom = str(self._get_config("github_proxy", "") or "").strip().rstrip("/")
            if custom:
                return custom
        elif source == "不使用代理":
            return ""
        elif not source:
            # 兼容旧配置：github_proxy_source 为空时检查旧的 github_proxy 字段
            legacy = str(self._get_config("github_proxy", "") or "").strip().rstrip("/")
            if legacy:
                return legacy
        # 默认（空或未识别）：返回 ghfast.top
        return "https://ghfast.top"

    def _get_cached_image(self, cache_key: str) -> str | None:
        """检查缓存图片是否存在，返回路径或 None"""
        path = self.cache_dir / f"{cache_key}.png"
        if path.exists():
            return str(path)
        return None

    def _is_notfound(self, cache_key: str) -> bool:
        """检查 notfound 标记是否存在且未过期"""
        path = self.notfound_dir / f"{cache_key}.json"
        if not path.exists():
            return False
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            # 检查过期
            expire_days = self._get_config_int("notfound_expire_days", 7)
            if time.time() - data.get("timestamp", 0) > expire_days * 86400:
                path.unlink(missing_ok=True)
                return False
            # 检查日期列表是否变化
            current_hash = self._get_date_list_hash()
            if data.get("date_list_hash") != current_hash:
                path.unlink(missing_ok=True)
                return False
            return True
        except (json.JSONDecodeError, KeyError, OSError, TypeError, ValueError):
            path.unlink(missing_ok=True)
            return False

    def _write_notfound(self, cache_key: str, dates_tried: int):
        """写入 notfound 标记（JSON 格式）"""
        path = self.notfound_dir / f"{cache_key}.json"
        data = {
            "timestamp": int(time.time()),
            "dates_tried": dates_tried,
            "date_list_hash": self._get_date_list_hash(),
        }
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except OSError as e:
            logger.warning(f"写入 notfound 标记失败: {e}")

    def _save_image_atomic(self, cache_key: str, data: bytes) -> str:
        """原子写入缓存图片：先写临时文件再 replace"""
        target = self.cache_dir / f"{cache_key}.png"
        tmp_path = self.cache_dir / f"{cache_key}.tmp"
        try:
            with open(tmp_path, "wb") as f:
                f.write(data)
            os.replace(tmp_path, target)
            return str(target)
        except OSError as e:
            logger.warning(f"缓存写入失败: {e}")
            # 清理临时文件
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _get_date_list_hash(self) -> str:
        """获取当前日期列表的 hash"""
        return hashlib.md5(",".join(self.date_list).encode()).hexdigest()[:8]

    def _load_date_list(self):
        """加载日期列表：合并远程缓存 + 硬编码 + extra_dates，去重后按日期倒序"""
        dates = set(HARDCODED_DATES)

        # 从本地缓存加载
        if self.dates_cache_path.exists():
            try:
                with open(self.dates_cache_path, encoding="utf-8") as f:
                    cached = json.load(f)
                if isinstance(cached, list):
                    dates.update(cached)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"日期缓存加载失败: {e}")

        # 合并 extra_dates 配置
        extra = self._get_config("extra_dates", "")
        if extra:
            for line in extra.strip().splitlines():
                line = line.strip()
                if line and line.isdigit() and len(line) == 8:
                    dates.add(line)

        self.date_list = sorted(dates, reverse=True)

    def _load_metadata_index(self):
        """从本地缓存的元数据 JSON 文件加载索引到内存"""
        self.metadata_index.clear()
        if not self.metadata_dir.exists():
            return
        for json_file in self.metadata_dir.glob("*.json"):
            try:
                cp = json_file.stem  # 文件名就是 emoji codepoint，如 "1f437"
                with open(json_file, encoding="utf-8") as f:
                    data = json.load(f)
                index_entry = _parse_combinations(data)
                if index_entry:
                    self.metadata_index[cp] = index_entry
            except (json.JSONDecodeError, OSError, TypeError, KeyError, AttributeError) as e:
                logger.warning(f"加载元数据索引失败 {json_file.name}: {e}")

    def _lookup_date(self, cp1: str, cp2: str) -> str | None:
        """从元数据索引中查找组合对应的日期，双向查找"""
        # 方向1: cp1 的元数据中查找 cp2
        entry = self.metadata_index.get(cp1, {})
        date = entry.get(cp2)
        if date:
            return date
        # 方向2: cp2 的元数据中查找 cp1
        entry = self.metadata_index.get(cp2, {})
        date = entry.get(cp1)
        if date:
            return date
        return None

    async def _fetch_and_cache_metadata(self, cp: str):
        """从 GitHub 拉取单个 emoji 的元数据 JSON 并缓存，更新内存索引"""
        github_proxy = self._resolve_github_proxy()
        timeout_sec = self._get_config_int("request_timeout", 10)
        raw_url = f"https://raw.githubusercontent.com/xsalazar/emoji-kitchen-backend/main/emoji/data/{cp}.json"
        if github_proxy:
            url = f"{github_proxy}/{raw_url}"
        else:
            url = raw_url

        try:
            session = await self._ensure_session()
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout_sec)) as resp:
                if resp.status != 200:
                    logger.debug(f"元数据拉取失败 {cp}: HTTP {resp.status}")
                    return
                data = await resp.json(content_type=None)

            # 缓存到本地文件
            cache_file = self.metadata_dir / f"{cp}.json"
            try:
                with open(cache_file, "w", encoding="utf-8") as f:
                    json.dump(data, f)
            except OSError as e:
                logger.warning(f"元数据缓存写入失败 {cp}: {e}")

            # 更新内存索引
            index_entry = _parse_combinations(data)
            if index_entry:
                self.metadata_index[cp] = index_entry

            # 从元数据中提取日期合并到日期列表
            new_dates = set()
            combinations = data.get("combinations", {}) if isinstance(data, dict) else {}
            for partner_cp, combo_list in combinations.items():
                if not isinstance(combo_list, list):
                    continue
                for item in combo_list:
                    if not isinstance(item, dict):
                        continue
                    d = item.get("date", "")
                    if d and d not in self.date_list:
                        new_dates.add(d)
            if new_dates:
                date_set = set(self.date_list) | new_dates
                self.date_list = sorted(date_set, reverse=True)

        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError, OSError, KeyError, TypeError, AttributeError) as e:
            logger.debug(f"元数据拉取异常 {cp}: {e}")

    async def _update_dates_from_remote(self):
        """从 GitHub 拉取样本数据提取日期列表并缓存"""
        github_proxy = self._resolve_github_proxy()
        timeout_sec = self._get_config_int("request_timeout", 10)
        raw_url = "https://raw.githubusercontent.com/xsalazar/emoji-kitchen-backend/main/emoji/data/1f600.json"
        if github_proxy:
            url = f"{github_proxy}/{raw_url}"
        else:
            url = raw_url

        try:
            session = await self._ensure_session()

            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout_sec)) as resp:
                if resp.status != 200:
                    logger.warning(f"远程日期拉取失败: HTTP {resp.status}")
                    return
                data = await resp.json(content_type=None)

            # 提取所有 date 字段
            remote_dates = set()
            if isinstance(data, dict):
                combinations = data.get("combinations", {})
                if isinstance(combinations, dict):
                    for partner_cp, combo_list in combinations.items():
                        if isinstance(combo_list, list):
                            for item in combo_list:
                                if not isinstance(item, dict):
                                    continue
                                d = item.get("date", "")
                                if d:
                                    remote_dates.add(d)

            if remote_dates:
                # 合并到已知列表并写入缓存
                all_dates = set(HARDCODED_DATES) | remote_dates
                if self.dates_cache_path.exists():
                    try:
                        with open(self.dates_cache_path, encoding="utf-8") as f:
                            existing = json.load(f)
                        if isinstance(existing, list):
                            all_dates.update(existing)
                    except (json.JSONDecodeError, OSError):
                        pass

                sorted_dates = sorted(all_dates, reverse=True)
                with open(self.dates_cache_path, "w", encoding="utf-8") as f:
                    json.dump(sorted_dates, f)

                # 重新加载
                self._load_date_list()
                logger.info(f"日期列表更新成功，共 {len(self.date_list)} 个日期")
        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError, OSError, KeyError, TypeError, AttributeError) as e:
            logger.warning(f"远程日期更新失败: {e}")

    async def _get_lock(self, cache_key: str) -> asyncio.Lock:
        """获取指定 cache_key 的锁，用于并发请求去重（LRU 淘汰）"""
        async with self._global_lock:
            if cache_key in self._locks:
                self._locks.move_to_end(cache_key)
                return self._locks[cache_key]
            # 先淘汰再插入，确保当前 key 不会被淘汰
            while len(self._locks) >= self._MAX_LOCKS:
                evicted = False
                for key in list(self._locks.keys()):
                    if not self._locks[key].locked():
                        del self._locks[key]
                        evicted = True
                        break
                if not evicted:
                    break  # 所有锁都在使用中，允许临时超出上限
            lock = asyncio.Lock()
            self._locks[cache_key] = lock
            return lock

    def _build_urls(self, cp1: str, cp2: str, date: str) -> list[str]:
        """为给定日期构造两个方向的 URL（A→B 和 B→A）"""
        cdn_url = self._resolve_cdn_url()
        seg1 = codepoint_to_url_segment(cp1)
        seg2 = codepoint_to_url_segment(cp2)
        return [
            f"{cdn_url}/android/keyboard/emojikitchen/{date}/{seg1}/{seg1}_{seg2}.png",
            f"{cdn_url}/android/keyboard/emojikitchen/{date}/{seg2}/{seg2}_{seg1}.png",
        ]

    async def _try_fetch_url(self, url: str) -> bytes | None:
        """尝试请求单个 URL。返回 None 仅表示确认 404，其他失败均 raise。"""
        timeout_sec = self._get_config_int("request_timeout", 10)
        try:
            session = await self._ensure_session()

            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=timeout_sec)
            ) as resp:
                if resp.status == 404:
                    return None
                if resp.status == 429:
                    logger.warning(f"Emoji Kitchen CDN 限流: {url}")
                    raise RateLimitError()
                if resp.status >= 500:
                    logger.warning(f"Emoji Kitchen CDN 服务端错误 {resp.status}: {url}")
                    raise aiohttp.ClientError(f"server error {resp.status}")
                if resp.status != 200:
                    logger.warning(f"Emoji Kitchen 未预期状态码 {resp.status}: {url}")
                    raise aiohttp.ClientError(f"unexpected status {resp.status}")

                data = await resp.read()
                # PNG 校验：检查 magic bytes
                if data[:4] != b'\x89PNG':
                    logger.warning(f"Emoji Kitchen 响应非 PNG 格式: {url}")
                    raise aiohttp.ClientError("not PNG")
                return data
        except asyncio.CancelledError:
            raise
        except (RateLimitError, aiohttp.ClientError):
            raise
        except Exception as e:
            logger.warning(f"Emoji Kitchen 请求异常: {url} - {e}")
            raise aiohttp.ClientError(str(e))

    async def _try_fetch_with_semaphore(self, url: str) -> bytes | None:
        """带 semaphore 限流的 URL 请求"""
        async with self._semaphore:
            return await self._try_fetch_url(url)

    async def _try_exact_date(self, cp1: str, cp2: str, date: str, cache_key: str) -> str | None:
        """尝试精确日期的两个方向 URL，命中即返回"""
        urls = self._build_urls(cp1, cp2, date)
        tasks = [asyncio.ensure_future(self._try_fetch_with_semaphore(url)) for url in urls]
        try:
            for coro in asyncio.as_completed(tasks):
                try:
                    result = await coro
                except RateLimitError:
                    raise  # 限流异常向上传播，停止后续探测
                except Exception:
                    continue
                if result is not None:
                    try:
                        return self._save_image_atomic(cache_key, result)
                    except OSError:
                        return None
            return None
        finally:
            # 取消剩余未完成的任务
            for t in tasks:
                if not t.done():
                    t.cancel()
            # 等待取消完成，避免警告
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _fetch_emoji_image(self, cp1: str, cp2: str) -> str | None:
        """尝试从 CDN 获取合成图片，返回缓存路径或 None

        策略（三阶段）：
        1. 精确查找：从元数据索引获取确切日期，只请求该日期（2 个方向）
        2. 按需拉取：索引未命中时，从 GitHub 拉取元数据并缓存，再次查找
        3. 回退探测：仍未命中则走原有的日期探测逻辑
        """
        cache_key = make_cache_key(cp1, cp2)

        # === 阶段1：精确查找 ===
        date = self._lookup_date(cp1, cp2)
        if date:
            result = await self._try_exact_date(cp1, cp2, date, cache_key)
            if result:
                return result

        # === 阶段2：按需拉取元数据 ===
        # 检查是否需要拉取（本地没有缓存文件或文件过期）
        fetched_new = False
        for cp in (cp1, cp2):
            meta_file = self.metadata_dir / f"{cp}.json"
            need_fetch = False
            if not meta_file.exists():
                need_fetch = True
            else:
                try:
                    age = time.time() - meta_file.stat().st_mtime
                    if age > 7 * 86400:  # 7 天过期
                        need_fetch = True
                except OSError:
                    need_fetch = True
            if need_fetch:
                await self._fetch_and_cache_metadata(cp)
                fetched_new = True

        if fetched_new:
            date = self._lookup_date(cp1, cp2)
            if date:
                result = await self._try_exact_date(cp1, cp2, date, cache_key)
                if result:
                    return result

        # === 阶段3：回退到日期探测 ===
        return await self._probe_dates(cp1, cp2, cache_key)

    async def _probe_dates(self, cp1: str, cp2: str, cache_key: str) -> str | None:
        """回退：按日期列表逐日期探测（原有逻辑）"""
        max_probe = self._get_config_int("max_probe_dates", 10)
        probe_dates = self.date_list[:max_probe]
        if not probe_dates:
            logger.warning("日期列表为空，无法探测")
            return None

        all_404 = True
        has_error = False

        for date in probe_dates:
            urls = self._build_urls(cp1, cp2, date)
            tasks = [self._try_fetch_with_semaphore(url) for url in urls]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            found_data = None
            should_stop = False
            for r in results:
                if isinstance(r, RateLimitError):
                    all_404 = False
                    has_error = True
                    should_stop = True
                elif isinstance(r, Exception):
                    all_404 = False
                    has_error = True
                elif r is not None:
                    found_data = r

            if found_data:
                try:
                    path = self._save_image_atomic(cache_key, found_data)
                    return path
                except OSError:
                    return None

            if should_stop:
                break

        # notfound 判定逻辑
        if all_404 and not has_error and max_probe >= len(self.date_list):
            self._write_notfound(cache_key, len(probe_dates))
            logger.info(f"Emoji Kitchen 组合不存在: {cache_key}")
        elif has_error:
            logger.info(f"Emoji Kitchen 探测存在网络错误，不写 notfound: {cache_key}")
        else:
            logger.info(f"Emoji Kitchen 未命中（探测了 {len(probe_dates)}/{len(self.date_list)} 个日期）: {cache_key}")
        return None

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """监听所有消息，检测双 emoji 并合成"""
        # 1. 获取纯文本消息
        msg = event.message_str.strip() if event.message_str else ""
        if not msg:
            return

        # 2. 提取 emoji
        emojis = EMOJI_PATTERN.findall(msg)
        if len(emojis) != 2:
            return

        # 3. 验证消息仅包含这两个 emoji（无多余字符）
        if "".join(emojis) != msg:
            return

        # 4. 转换 codepoint 并生成 cache_key
        cp1 = emoji_to_codepoint(emojis[0])
        cp2 = emoji_to_codepoint(emojis[1])
        cache_key = make_cache_key(cp1, cp2)

        # 5. 检查缓存
        cached = self._get_cached_image(cache_key)
        if cached:
            yield event.image_result(cached)
            event.stop_event()
            return

        # 6. 检查 notfound 标记
        if self._is_notfound(cache_key):
            return

        # 7. 使用并发锁防止重复请求
        lock = await self._get_lock(cache_key)
        async with lock:
            # 双重检查：获取锁后再次检查缓存和 notfound
            cached = self._get_cached_image(cache_key)
            if cached:
                yield event.image_result(cached)
                event.stop_event()
                return
            if self._is_notfound(cache_key):
                return

            # 8. 从 CDN 获取（异常兜底）
            try:
                path = await self._fetch_emoji_image(cp1, cp2)
                if path:
                    yield event.image_result(path)
                    event.stop_event()
                    return
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError, RateLimitError) as e:
                logger.error(f"Emoji Kitchen 获取异常: {e}")
            except Exception as e:
                logger.exception(f"Emoji Kitchen 未预期异常: {e}")

        # 未命中：不 yield，不 stop_event，事件继续传播

    async def terminate(self):
        """插件销毁：取消后台任务、关闭 HTTP session"""
        if self._update_task and not self._update_task.done():
            self._update_task.cancel()
            try:
                await self._update_task
            except (asyncio.CancelledError, Exception):
                pass
            self._update_task = None
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
