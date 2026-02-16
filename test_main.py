import os
import json
import time
import asyncio
from pathlib import Path
from collections import OrderedDict
from unittest.mock import patch, MagicMock, AsyncMock

import pytest
import aiohttp

# 需要 mock AstrBot 依赖才能导入 main
import sys
# Mock AstrBot 模块
sys.modules["astrbot"] = MagicMock()
sys.modules["astrbot.api"] = MagicMock()
sys.modules["astrbot.api.event"] = MagicMock()
sys.modules["astrbot.api.star"] = MagicMock()

# Mock register 装饰器为透传
from unittest.mock import MagicMock as _MagicMock
mock_star_module = sys.modules["astrbot.api.star"]
mock_star_module.register = lambda *a, **k: lambda cls: cls
mock_star_module.Star = type("Star", (), {"__init__": lambda self, *a, **k: None})
mock_star_module.Context = _MagicMock

# Mock filter
mock_event_module = sys.modules["astrbot.api.event"]
mock_filter = _MagicMock()
mock_filter.event_message_type = lambda *a, **k: lambda fn: fn
mock_filter.EventMessageType = _MagicMock()
mock_filter.EventMessageType.ALL = "ALL"
mock_event_module.filter = mock_filter
mock_event_module.AstrMessageEvent = _MagicMock
mock_event_module.MessageEventResult = _MagicMock

# Mock logger
mock_api = sys.modules["astrbot.api"]
mock_api.logger = MagicMock()

from main import (
    emoji_to_codepoint,
    codepoint_to_url_segment,
    make_cache_key,
    EMOJI_PATTERN,
    HARDCODED_DATES,
    RateLimitError,
    EmojiKitchenPlugin,
)


class AsyncContextManager:
    """辅助类：mock async context manager"""
    def __init__(self, return_value):
        self.return_value = return_value
    async def __aenter__(self):
        return self.return_value
    async def __aexit__(self, *args):
        pass


class TestToolFunctions:
    """测试模块级工具函数"""

    def test_emoji_to_codepoint_single(self):
        """单码点 emoji：😀 → '1f600'"""
        assert emoji_to_codepoint("😀") == "1f600"

    def test_emoji_to_codepoint_multi(self):
        """多码点 emoji：❤️ (U+2764 + U+FE0F) → '2764-fe0f'"""
        assert emoji_to_codepoint("❤️") == "2764-fe0f"

    def test_emoji_to_codepoint_zwj(self):
        """ZWJ 序列：👨‍👩‍👧 → 包含 200d 的 codepoint"""
        result = emoji_to_codepoint("👨‍👩‍👧")
        assert "200d" in result

    def test_codepoint_to_url_segment_single(self):
        assert codepoint_to_url_segment("1f600") == "u1f600"

    def test_codepoint_to_url_segment_multi(self):
        assert codepoint_to_url_segment("2764-fe0f") == "u2764-ufe0f"

    def test_make_cache_key_sorted(self):
        """验证排序：无论输入顺序，结果相同"""
        assert make_cache_key("1f600", "1f60d") == "1f600_1f60d"
        assert make_cache_key("1f60d", "1f600") == "1f600_1f60d"

    def test_make_cache_key_same(self):
        """相同 emoji"""
        assert make_cache_key("1f600", "1f600") == "1f600_1f600"


class TestEmojiPattern:
    """测试 EMOJI_PATTERN 正则匹配"""

    def test_two_simple_emojis(self):
        """两个简单 emoji"""
        result = EMOJI_PATTERN.findall("😀😍")
        assert len(result) == 2
        assert result[0] == "😀"
        assert result[1] == "😍"

    def test_two_emojis_with_space(self):
        """两个 emoji 中间有空格 → findall 仍返回 2 个，但 join 校验会失败"""
        msg = "😀 😍"
        emojis = EMOJI_PATTERN.findall(msg)
        assert len(emojis) == 2
        assert "".join(emojis) != msg  # 有空格，不等于原消息

    def test_single_emoji(self):
        """单个 emoji → 不触发"""
        result = EMOJI_PATTERN.findall("😀")
        assert len(result) == 1

    def test_three_emojis(self):
        """三个 emoji → 不触发"""
        result = EMOJI_PATTERN.findall("😀😍🎉")
        assert len(result) == 3

    def test_emoji_with_text(self):
        """emoji + 文字 → join 校验失败"""
        msg = "hello😀😍"
        emojis = EMOJI_PATTERN.findall(msg)
        assert "".join(emojis) != msg

    def test_emoji_with_variation_selector(self):
        """带变体选择符的 emoji"""
        result = EMOJI_PATTERN.findall("❤️😀")
        assert len(result) == 2

    def test_emoji_with_skin_tone(self):
        """带肤色修饰符的 emoji"""
        result = EMOJI_PATTERN.findall("👍🏻😀")
        assert len(result) == 2
        assert result[0] == "👍🏻"

    def test_zwj_sequence(self):
        """ZWJ 组合 emoji 算一个 grapheme"""
        result = EMOJI_PATTERN.findall("👨‍👩‍👧😀")
        assert len(result) == 2

    def test_empty_string(self):
        result = EMOJI_PATTERN.findall("")
        assert len(result) == 0

    def test_pure_text(self):
        result = EMOJI_PATTERN.findall("hello world")
        assert len(result) == 0


class TestCacheManagement:
    """测试缓存管理方法"""

    @pytest.fixture
    def plugin(self, tmp_path):
        """创建带临时目录的插件实例"""
        with patch("main.Star.__init__", return_value=None), \
             patch("main.register", lambda *a, **k: lambda cls: cls):
            # 直接构造，绕过 AstrBot 框架
            from main import EmojiKitchenPlugin
            ctx = MagicMock()
            plugin = EmojiKitchenPlugin.__new__(EmojiKitchenPlugin)
            plugin.context = ctx
            plugin.config = {"notfound_expire_days": 7}
            plugin.data_dir = tmp_path
            plugin.cache_dir = tmp_path / "cache"
            plugin.notfound_dir = tmp_path / "notfound"
            plugin.dates_cache_path = tmp_path / "dates_cache.json"
            plugin.date_list = list(HARDCODED_DATES)
            plugin.metadata_dir = tmp_path / "metadata"
            plugin.metadata_index = {}
            plugin._locks = OrderedDict()
            plugin._global_lock = asyncio.Lock()
            plugin._session = None
            plugin._session_lock = asyncio.Lock()
            plugin._semaphore = asyncio.Semaphore(4)
            plugin._update_task = None
            plugin.cache_dir.mkdir(parents=True, exist_ok=True)
            plugin.notfound_dir.mkdir(parents=True, exist_ok=True)
            plugin.metadata_dir.mkdir(parents=True, exist_ok=True)
            return plugin

    def test_get_cached_image_exists(self, plugin):
        """缓存存在时返回路径"""
        (plugin.cache_dir / "test_key.png").write_bytes(b"\x89PNG fake")
        result = plugin._get_cached_image("test_key")
        assert result is not None
        assert result.endswith("test_key.png")

    def test_get_cached_image_not_exists(self, plugin):
        """缓存不存在时返回 None"""
        assert plugin._get_cached_image("nonexistent") is None

    def test_is_notfound_not_exists(self, plugin):
        """标记文件不存在 → False"""
        assert plugin._is_notfound("test_key") is False

    def test_is_notfound_valid(self, plugin):
        """有效的 notfound 标记 → True"""
        data = {
            "timestamp": int(time.time()),
            "dates_tried": 34,
            "date_list_hash": plugin._get_date_list_hash(),
        }
        (plugin.notfound_dir / "test_key.json").write_text(json.dumps(data))
        assert plugin._is_notfound("test_key") is True

    def test_is_notfound_expired(self, plugin):
        """过期的 notfound 标记 → False"""
        data = {
            "timestamp": int(time.time()) - 8 * 86400,  # 8 天前
            "dates_tried": 34,
            "date_list_hash": plugin._get_date_list_hash(),
        }
        (plugin.notfound_dir / "test_key.json").write_text(json.dumps(data))
        assert plugin._is_notfound("test_key") is False

    def test_is_notfound_hash_mismatch(self, plugin):
        """日期列表 hash 不匹配 → False"""
        data = {
            "timestamp": int(time.time()),
            "dates_tried": 34,
            "date_list_hash": "wrong_hash",
        }
        (plugin.notfound_dir / "test_key.json").write_text(json.dumps(data))
        assert plugin._is_notfound("test_key") is False

    def test_is_notfound_corrupted_json(self, plugin):
        """损坏的 JSON → False"""
        (plugin.notfound_dir / "test_key.json").write_text("not json")
        assert plugin._is_notfound("test_key") is False

    def test_write_notfound(self, plugin):
        """写入 notfound 标记"""
        plugin._write_notfound("test_key", 34)
        path = plugin.notfound_dir / "test_key.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert "timestamp" in data
        assert data["dates_tried"] == 34
        assert "date_list_hash" in data

    def test_save_image_atomic(self, plugin):
        """原子写入图片"""
        png_data = b"\x89PNG fake image data"
        result = plugin._save_image_atomic("test_key", png_data)
        assert result.endswith("test_key.png")
        assert Path(result).read_bytes() == png_data
        # 临时文件应该不存在
        assert not (plugin.cache_dir / "test_key.tmp").exists()


class TestDateList:
    """测试日期列表管理"""

    @pytest.fixture
    def plugin(self, tmp_path):
        """同 TestCacheManagement 的 fixture"""
        with patch("main.Star.__init__", return_value=None):
            from main import EmojiKitchenPlugin
            plugin = EmojiKitchenPlugin.__new__(EmojiKitchenPlugin)
            plugin.context = MagicMock()
            plugin.config = {}
            plugin.data_dir = tmp_path
            plugin.cache_dir = tmp_path / "cache"
            plugin.notfound_dir = tmp_path / "notfound"
            plugin.dates_cache_path = tmp_path / "dates_cache.json"
            plugin.date_list = []
            plugin.metadata_dir = tmp_path / "metadata"
            plugin.metadata_index = {}
            plugin._locks = OrderedDict()
            plugin._global_lock = asyncio.Lock()
            plugin._session = None
            plugin._session_lock = asyncio.Lock()
            plugin._semaphore = asyncio.Semaphore(4)
            plugin._update_task = None
            plugin.cache_dir.mkdir(parents=True, exist_ok=True)
            plugin.notfound_dir.mkdir(parents=True, exist_ok=True)
            plugin.metadata_dir.mkdir(parents=True, exist_ok=True)
            return plugin

    def test_load_date_list_hardcoded_only(self, plugin):
        """仅硬编码日期"""
        plugin._load_date_list()
        assert len(plugin.date_list) == len(HARDCODED_DATES)
        # 验证倒序
        assert plugin.date_list == sorted(set(HARDCODED_DATES), reverse=True)

    def test_load_date_list_with_cache(self, plugin):
        """硬编码 + 本地缓存"""
        plugin.dates_cache_path.write_text(json.dumps(["20261001", "20260501"]))
        plugin._load_date_list()
        assert "20261001" in plugin.date_list
        assert "20260501" in plugin.date_list
        assert len(plugin.date_list) == len(HARDCODED_DATES) + 2

    def test_load_date_list_with_extra_dates(self, plugin):
        """硬编码 + extra_dates 配置"""
        plugin.config = {"extra_dates": "20261201\n20261101\n"}
        plugin._load_date_list()
        assert "20261201" in plugin.date_list
        assert "20261101" in plugin.date_list

    def test_load_date_list_extra_dates_invalid(self, plugin):
        """无效的 extra_dates 被忽略"""
        plugin.config = {"extra_dates": "invalid\n2026\n20261201\n"}
        plugin._load_date_list()
        assert "20261201" in plugin.date_list
        assert "invalid" not in plugin.date_list
        assert "2026" not in plugin.date_list

    def test_load_date_list_dedup(self, plugin):
        """去重：缓存中有重复日期"""
        plugin.dates_cache_path.write_text(json.dumps(["20251029"]))  # 已在硬编码中
        plugin._load_date_list()
        assert plugin.date_list.count("20251029") == 1

    def test_date_list_hash_deterministic(self, plugin):
        """hash 确定性"""
        plugin.date_list = ["20251029", "20250501"]
        h1 = plugin._get_date_list_hash()
        h2 = plugin._get_date_list_hash()
        assert h1 == h2
        assert len(h1) == 8

    def test_date_list_hash_changes(self, plugin):
        """日期列表变化时 hash 变化"""
        plugin.date_list = ["20251029"]
        h1 = plugin._get_date_list_hash()
        plugin.date_list = ["20251029", "20250501"]
        h2 = plugin._get_date_list_hash()
        assert h1 != h2


class TestBuildUrls:
    """测试 URL 构造"""

    @pytest.fixture
    def plugin(self, tmp_path):
        with patch("main.Star.__init__", return_value=None):
            from main import EmojiKitchenPlugin
            plugin = EmojiKitchenPlugin.__new__(EmojiKitchenPlugin)
            plugin.config = {"cdn_url": "https://www.gstatic.cn"}
            return plugin

    def test_build_urls_returns_two(self, plugin):
        """返回两个 URL（双向）"""
        urls = plugin._build_urls("1f600", "1f60d", "20251029")
        assert len(urls) == 2

    def test_build_urls_format(self, plugin):
        """URL 格式正确"""
        urls = plugin._build_urls("1f600", "1f60d", "20251029")
        assert urls[0] == "https://www.gstatic.cn/android/keyboard/emojikitchen/20251029/u1f600/u1f600_u1f60d.png"
        assert urls[1] == "https://www.gstatic.cn/android/keyboard/emojikitchen/20251029/u1f60d/u1f60d_u1f600.png"

    def test_build_urls_multi_codepoint(self, plugin):
        """多码点 emoji 的 URL"""
        urls = plugin._build_urls("2764-fe0f", "1f600", "20251029")
        assert "u2764-ufe0f" in urls[0]
        assert "u1f600" in urls[0]


class TestMetadataIndex:
    """测试元数据索引功能"""

    @pytest.fixture
    def plugin(self, tmp_path):
        from main import EmojiKitchenPlugin
        plugin = EmojiKitchenPlugin.__new__(EmojiKitchenPlugin)
        plugin.config = {}
        plugin.data_dir = tmp_path
        plugin.cache_dir = tmp_path / "cache"
        plugin.notfound_dir = tmp_path / "notfound"
        plugin.metadata_dir = tmp_path / "metadata"
        plugin.dates_cache_path = tmp_path / "dates_cache.json"
        from main import HARDCODED_DATES
        plugin.date_list = list(HARDCODED_DATES)
        plugin.metadata_index = {}
        plugin._locks = OrderedDict()
        plugin._global_lock = asyncio.Lock()
        plugin._session = None
        plugin._session_lock = asyncio.Lock()
        plugin._semaphore = asyncio.Semaphore(4)
        plugin._update_task = None
        plugin.cache_dir.mkdir(parents=True, exist_ok=True)
        plugin.notfound_dir.mkdir(parents=True, exist_ok=True)
        plugin.metadata_dir.mkdir(parents=True, exist_ok=True)
        return plugin

    def test_lookup_date_hit(self, plugin):
        """索引命中：双向查找"""
        plugin.metadata_index = {
            "1f437": {"1f437": "20230216", "1f600": "20201001"}
        }
        # 正向命中
        assert plugin._lookup_date("1f437", "1f600") == "20201001"
        # 反向命中
        assert plugin._lookup_date("1f600", "1f437") == "20201001"

    def test_lookup_date_miss(self, plugin):
        """索引未命中"""
        plugin.metadata_index = {
            "1f437": {"1f437": "20230216"}
        }
        assert plugin._lookup_date("1f437", "1f600") is None
        assert plugin._lookup_date("1f600", "1f60d") is None

    def test_lookup_date_empty_index(self, plugin):
        """空索引"""
        assert plugin._lookup_date("1f437", "1f600") is None

    def test_load_metadata_index(self, plugin):
        """从本地文件加载索引"""
        # 写入一个元数据文件
        metadata = {
            "combinations": {
                "1f600": [
                    {"gStaticUrl": "...", "date": "20201001", "isLatest": True}
                ],
                "1f60d": [
                    {"gStaticUrl": "...", "date": "20230216", "isLatest": False},
                    {"gStaticUrl": "...", "date": "20201001", "isLatest": True}
                ]
            }
        }
        (plugin.metadata_dir / "1f437.json").write_text(json.dumps(metadata))
        plugin._load_metadata_index()

        assert "1f437" in plugin.metadata_index
        assert plugin.metadata_index["1f437"]["1f600"] == "20201001"
        # isLatest=True 的应该被选中
        assert plugin.metadata_index["1f437"]["1f60d"] == "20201001"

    def test_load_metadata_index_no_is_latest(self, plugin):
        """没有 isLatest 字段时取第一条"""
        metadata = {
            "combinations": {
                "1f600": [
                    {"gStaticUrl": "...", "date": "20230216"},
                    {"gStaticUrl": "...", "date": "20201001"}
                ]
            }
        }
        (plugin.metadata_dir / "1f437.json").write_text(json.dumps(metadata))
        plugin._load_metadata_index()
        assert plugin.metadata_index["1f437"]["1f600"] == "20230216"

    def test_load_metadata_index_corrupted_file(self, plugin):
        """损坏的 JSON 文件被跳过"""
        (plugin.metadata_dir / "bad.json").write_text("not json")
        (plugin.metadata_dir / "1f437.json").write_text(json.dumps({
            "combinations": {"1f600": [{"date": "20201001", "isLatest": True}]}
        }))
        plugin._load_metadata_index()
        # bad.json 被跳过，1f437 正常加载
        assert "1f437" in plugin.metadata_index
        assert "bad" not in plugin.metadata_index

    def test_load_metadata_index_empty_dir(self, plugin):
        """空目录"""
        plugin._load_metadata_index()
        assert plugin.metadata_index == {}

    @pytest.mark.asyncio
    async def test_fetch_and_cache_metadata(self, plugin):
        """远程拉取并缓存元数据"""
        remote_data = {
            "combinations": {
                "1f600": [
                    {"gStaticUrl": "...", "date": "20201001", "isLatest": True}
                ]
            }
        }
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=remote_data)

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.get = MagicMock(return_value=AsyncContextManager(mock_resp))
        plugin._session = mock_session

        await plugin._fetch_and_cache_metadata("1f437")

        # 验证文件缓存
        assert (plugin.metadata_dir / "1f437.json").exists()
        # 验证内存索引更新
        assert "1f437" in plugin.metadata_index
        assert plugin.metadata_index["1f437"]["1f600"] == "20201001"

    @pytest.mark.asyncio
    async def test_fetch_and_cache_metadata_failure(self, plugin):
        """远程拉取失败不影响已有索引"""
        plugin.metadata_index = {"existing": {"key": "value"}}

        mock_resp = AsyncMock()
        mock_resp.status = 404

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.get = MagicMock(return_value=AsyncContextManager(mock_resp))
        plugin._session = mock_session

        await plugin._fetch_and_cache_metadata("nonexistent")

        # 已有索引不受影响
        assert plugin.metadata_index == {"existing": {"key": "value"}}

    @pytest.mark.asyncio
    async def test_fetch_and_cache_metadata_merges_dates(self, plugin):
        """拉取元数据时新日期被合并到 date_list"""
        plugin.date_list = ["20251029"]
        remote_data = {
            "combinations": {
                "1f600": [
                    {"gStaticUrl": "...", "date": "20190101", "isLatest": True}
                ]
            }
        }
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=remote_data)

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.get = MagicMock(return_value=AsyncContextManager(mock_resp))
        plugin._session = mock_session

        await plugin._fetch_and_cache_metadata("1f437")

        assert "20190101" in plugin.date_list
        assert "20251029" in plugin.date_list


class TestTryFetchUrl:
    """测试 _try_fetch_url"""

    @pytest.fixture
    def plugin(self, tmp_path):
        with patch("main.Star.__init__", return_value=None):
            from main import EmojiKitchenPlugin
            plugin = EmojiKitchenPlugin.__new__(EmojiKitchenPlugin)
            plugin.config = {"request_timeout": 10}
            plugin._session = None
            plugin._session_lock = asyncio.Lock()
            return plugin

    @pytest.mark.asyncio
    async def test_fetch_200_png(self, plugin):
        """200 + PNG → 返回 bytes"""
        png_data = b"\x89PNG fake data"
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.read = AsyncMock(return_value=png_data)

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.get = MagicMock(return_value=AsyncContextManager(mock_resp))
        plugin._session = mock_session

        result = await plugin._try_fetch_url("http://example.com/test.png")
        assert result == png_data

    @pytest.mark.asyncio
    async def test_fetch_404(self, plugin):
        """404 → 返回 None"""
        mock_resp = AsyncMock()
        mock_resp.status = 404

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.get = MagicMock(return_value=AsyncContextManager(mock_resp))
        plugin._session = mock_session

        result = await plugin._try_fetch_url("http://example.com/test.png")
        assert result is None

    @pytest.mark.asyncio
    async def test_fetch_429(self, plugin):
        """429 → raise RateLimitError"""
        mock_resp = AsyncMock()
        mock_resp.status = 429

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.get = MagicMock(return_value=AsyncContextManager(mock_resp))
        plugin._session = mock_session

        with pytest.raises(RateLimitError):
            await plugin._try_fetch_url("http://example.com/test.png")

    @pytest.mark.asyncio
    async def test_fetch_500(self, plugin):
        """5xx → raise ClientError"""
        mock_resp = AsyncMock()
        mock_resp.status = 500

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.get = MagicMock(return_value=AsyncContextManager(mock_resp))
        plugin._session = mock_session

        with pytest.raises(aiohttp.ClientError):
            await plugin._try_fetch_url("http://example.com/test.png")

    @pytest.mark.asyncio
    async def test_fetch_200_not_png(self, plugin):
        """200 + 非 PNG → raise ClientError"""
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.read = AsyncMock(return_value=b"not png data")

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.get = MagicMock(return_value=AsyncContextManager(mock_resp))
        plugin._session = mock_session

        with pytest.raises(aiohttp.ClientError):
            await plugin._try_fetch_url("http://example.com/test.png")


class TestFetchEmojiImage:
    """测试 _fetch_emoji_image 核心探测逻辑"""

    @pytest.fixture
    def plugin(self, tmp_path):
        from main import EmojiKitchenPlugin
        plugin = EmojiKitchenPlugin.__new__(EmojiKitchenPlugin)
        plugin.config = {"max_probe_dates": 10, "cdn_url": "https://www.gstatic.cn", "request_timeout": 10}
        plugin.data_dir = tmp_path
        plugin.cache_dir = tmp_path / "cache"
        plugin.notfound_dir = tmp_path / "notfound"
        plugin.dates_cache_path = tmp_path / "dates_cache.json"
        plugin.date_list = ["20251029", "20250501"]
        plugin.metadata_dir = tmp_path / "metadata"
        plugin.metadata_index = {}
        plugin._locks = OrderedDict()
        plugin._global_lock = asyncio.Lock()
        plugin._session = None
        plugin._session_lock = asyncio.Lock()
        plugin._semaphore = asyncio.Semaphore(4)
        plugin._update_task = None
        plugin.cache_dir.mkdir(parents=True, exist_ok=True)
        plugin.notfound_dir.mkdir(parents=True, exist_ok=True)
        plugin.metadata_dir.mkdir(parents=True, exist_ok=True)
        return plugin

    @pytest.mark.asyncio
    async def test_fetch_hit(self, plugin):
        """首个请求命中 → 返回缓存路径"""
        png_data = b"\x89PNG fake image"
        with patch.object(plugin, "_try_fetch_url", new_callable=AsyncMock) as mock_fetch, \
             patch.object(plugin, "_fetch_and_cache_metadata", new_callable=AsyncMock):
            mock_fetch.return_value = png_data
            result = await plugin._fetch_emoji_image("1f600", "1f60d")
            assert result is not None
            assert result.endswith(".png")
            assert Path(result).exists()

    @pytest.mark.asyncio
    async def test_fetch_all_404_full_probe(self, plugin):
        """全部 404 且探测全部日期 → 写入 notfound"""
        plugin.config["max_probe_dates"] = 10  # >= len(date_list)=2
        with patch.object(plugin, "_try_fetch_url", new_callable=AsyncMock) as mock_fetch, \
             patch.object(plugin, "_fetch_and_cache_metadata", new_callable=AsyncMock):
            mock_fetch.return_value = None
            result = await plugin._fetch_emoji_image("1f600", "1f60d")
            assert result is None
            from main import make_cache_key
            ck = make_cache_key("1f600", "1f60d")
            assert (plugin.notfound_dir / f"{ck}.json").exists()

    @pytest.mark.asyncio
    async def test_fetch_all_404_partial_probe(self, plugin):
        """全部 404 但 max_probe < 总日期数 → 不写 notfound"""
        plugin.config["max_probe_dates"] = 1  # < len(date_list)=2
        with patch.object(plugin, "_try_fetch_url", new_callable=AsyncMock) as mock_fetch, \
             patch.object(plugin, "_fetch_and_cache_metadata", new_callable=AsyncMock):
            mock_fetch.return_value = None
            result = await plugin._fetch_emoji_image("1f600", "1f60d")
            assert result is None
            from main import make_cache_key
            ck = make_cache_key("1f600", "1f60d")
            assert not (plugin.notfound_dir / f"{ck}.json").exists()

    @pytest.mark.asyncio
    async def test_fetch_429_stops_and_no_notfound(self, plugin):
        """429 → 立即停止，不写 notfound"""
        with patch.object(plugin, "_try_fetch_url", new_callable=AsyncMock) as mock_fetch, \
             patch.object(plugin, "_fetch_and_cache_metadata", new_callable=AsyncMock):
            mock_fetch.side_effect = RateLimitError()
            result = await plugin._fetch_emoji_image("1f600", "1f60d")
            assert result is None
            from main import make_cache_key
            ck = make_cache_key("1f600", "1f60d")
            assert not (plugin.notfound_dir / f"{ck}.json").exists()

    @pytest.mark.asyncio
    async def test_fetch_network_error_no_notfound(self, plugin):
        """网络错误 → 不写 notfound"""
        with patch.object(plugin, "_try_fetch_url", new_callable=AsyncMock) as mock_fetch, \
             patch.object(plugin, "_fetch_and_cache_metadata", new_callable=AsyncMock):
            mock_fetch.side_effect = aiohttp.ClientError("timeout")
            result = await plugin._fetch_emoji_image("1f600", "1f60d")
            assert result is None
            from main import make_cache_key
            ck = make_cache_key("1f600", "1f60d")
            assert not (plugin.notfound_dir / f"{ck}.json").exists()

    @pytest.mark.asyncio
    async def test_fetch_with_metadata_hit(self, plugin):
        """元数据索引命中 → 精确日期直接返回，不走探测"""
        png_data = b"\x89PNG fake image"
        plugin.metadata_index = {
            "1f600": {"1f60d": "20201001"}
        }
        with patch.object(plugin, "_try_fetch_url", new_callable=AsyncMock) as mock_fetch, \
             patch.object(plugin, "_fetch_and_cache_metadata", new_callable=AsyncMock) as mock_cache:
            mock_fetch.return_value = png_data
            result = await plugin._fetch_emoji_image("1f600", "1f60d")
            assert result is not None
            assert result.endswith(".png")
            # _fetch_and_cache_metadata 不应被调用（索引已命中）
            mock_cache.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetch_metadata_miss_fallback(self, plugin):
        """元数据未命中 → 拉取后仍未命中 → 回退到探测"""
        with patch.object(plugin, "_try_fetch_url", new_callable=AsyncMock) as mock_fetch, \
             patch.object(plugin, "_fetch_and_cache_metadata", new_callable=AsyncMock):
            mock_fetch.return_value = None  # 全部 404
            result = await plugin._fetch_emoji_image("1f600", "1f60d")
            assert result is None


class TestOnMessage:
    """测试 on_message 事件流"""

    @pytest.fixture
    def plugin(self, tmp_path):
        from main import EmojiKitchenPlugin
        plugin = EmojiKitchenPlugin.__new__(EmojiKitchenPlugin)
        plugin.config = {"max_probe_dates": 10, "cdn_url": "https://www.gstatic.cn",
                         "request_timeout": 10, "notfound_expire_days": 7}
        plugin.data_dir = tmp_path
        plugin.cache_dir = tmp_path / "cache"
        plugin.notfound_dir = tmp_path / "notfound"
        plugin.dates_cache_path = tmp_path / "dates_cache.json"
        plugin.date_list = list(HARDCODED_DATES)
        plugin.metadata_dir = tmp_path / "metadata"
        plugin.metadata_index = {}
        plugin._locks = OrderedDict()
        plugin._global_lock = asyncio.Lock()
        plugin._session = None
        plugin._session_lock = asyncio.Lock()
        plugin._semaphore = asyncio.Semaphore(4)
        plugin._update_task = None
        plugin.cache_dir.mkdir(parents=True, exist_ok=True)
        plugin.notfound_dir.mkdir(parents=True, exist_ok=True)
        plugin.metadata_dir.mkdir(parents=True, exist_ok=True)
        return plugin

    def _make_event(self, message_str):
        """创建 mock event"""
        event = MagicMock()
        event.message_str = message_str
        event.image_result = MagicMock(return_value="image_result")
        event.stop_event = MagicMock()
        return event

    @pytest.mark.asyncio
    async def test_two_emojis_cached(self, plugin):
        """两个 emoji + 缓存命中 → yield image_result + stop_event"""
        from main import emoji_to_codepoint, make_cache_key
        cp1 = emoji_to_codepoint("😀")
        cp2 = emoji_to_codepoint("😍")
        ck = make_cache_key(cp1, cp2)
        (plugin.cache_dir / f"{ck}.png").write_bytes(b"\x89PNG fake")

        event = self._make_event("😀😍")
        results = []
        async for r in plugin.on_message(event):
            results.append(r)

        assert len(results) == 1
        event.stop_event.assert_called_once()

    @pytest.mark.asyncio
    async def test_single_emoji_no_trigger(self, plugin):
        """单个 emoji → 不触发"""
        event = self._make_event("😀")
        results = []
        async for r in plugin.on_message(event):
            results.append(r)
        assert len(results) == 0
        event.stop_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_text_message_no_trigger(self, plugin):
        """纯文本 → 不触发"""
        event = self._make_event("hello world")
        results = []
        async for r in plugin.on_message(event):
            results.append(r)
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_emoji_with_text_no_trigger(self, plugin):
        """emoji + 文字 → 不触发"""
        event = self._make_event("hi😀😍")
        results = []
        async for r in plugin.on_message(event):
            results.append(r)
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_three_emojis_no_trigger(self, plugin):
        """三个 emoji → 不触发"""
        event = self._make_event("😀😍🎉")
        results = []
        async for r in plugin.on_message(event):
            results.append(r)
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_notfound_marker_skip(self, plugin):
        """有 notfound 标记 → 不触发"""
        from main import emoji_to_codepoint, make_cache_key
        cp1 = emoji_to_codepoint("😀")
        cp2 = emoji_to_codepoint("😍")
        ck = make_cache_key(cp1, cp2)
        data = {
            "timestamp": int(time.time()),
            "dates_tried": 34,
            "date_list_hash": plugin._get_date_list_hash(),
        }
        (plugin.notfound_dir / f"{ck}.json").write_text(json.dumps(data))

        event = self._make_event("😀😍")
        results = []
        async for r in plugin.on_message(event):
            results.append(r)
        assert len(results) == 0
        event.stop_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetch_success(self, plugin):
        """两个 emoji + fetch 成功 → yield image_result + stop_event"""
        event = self._make_event("😀😍")
        fake_path = str(plugin.cache_dir / "fake.png")
        with patch.object(plugin, "_fetch_emoji_image", new_callable=AsyncMock, return_value=fake_path):
            results = []
            async for r in plugin.on_message(event):
                results.append(r)
            assert len(results) == 1
            event.stop_event.assert_called_once()

    @pytest.mark.asyncio
    async def test_fetch_failure_no_trigger(self, plugin):
        """两个 emoji + fetch 失败 → 不触发"""
        event = self._make_event("😀😍")
        with patch.object(plugin, "_fetch_emoji_image", new_callable=AsyncMock, return_value=None):
            results = []
            async for r in plugin.on_message(event):
                results.append(r)
            assert len(results) == 0
            event.stop_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_message(self, plugin):
        """空消息 → 不触发"""
        event = self._make_event("")
        results = []
        async for r in plugin.on_message(event):
            results.append(r)
        assert len(results) == 0


# ========== 新增测试：对应 main.py 优化改动 ==========

class TestEnsureSession:
    """测试 _ensure_session 单例保证"""

    @pytest.fixture
    def plugin(self, tmp_path):
        from main import EmojiKitchenPlugin
        plugin = EmojiKitchenPlugin.__new__(EmojiKitchenPlugin)
        plugin.config = {"request_timeout": 10}
        plugin._session = None
        plugin._session_lock = asyncio.Lock()
        return plugin

    @pytest.mark.asyncio
    async def test_ensure_session_singleton(self, plugin):
        """并发调用 _ensure_session 只创建一个 session"""
        call_count = 0
        original_init = aiohttp.ClientSession.__init__

        def counting_init(self_session, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            original_init(self_session, *args, **kwargs)

        with patch.object(aiohttp.ClientSession, "__init__", counting_init):
            # 并发调用 10 次
            sessions = await asyncio.gather(
                *[plugin._ensure_session() for _ in range(10)]
            )

        # 所有返回同一个 session
        assert all(s is sessions[0] for s in sessions)
        # 只创建了一次
        assert call_count == 1

        # 清理
        if plugin._session and not plugin._session.closed:
            await plugin._session.close()


class TestLocksLRU:
    """测试 _locks LRU 淘汰"""

    @pytest.fixture
    def plugin(self):
        from main import EmojiKitchenPlugin
        plugin = EmojiKitchenPlugin.__new__(EmojiKitchenPlugin)
        plugin._locks = OrderedDict()
        plugin._global_lock = asyncio.Lock()
        return plugin

    @pytest.mark.asyncio
    async def test_locks_lru_eviction(self, plugin):
        """超过 _MAX_LOCKS 上限时淘汰最早的 key"""
        # 临时将上限设小以方便测试
        original_max = EmojiKitchenPlugin._MAX_LOCKS
        EmojiKitchenPlugin._MAX_LOCKS = 5
        try:
            # 插入 5 个 key
            for i in range(5):
                await plugin._get_lock(f"key_{i}")
            assert len(plugin._locks) == 5
            assert "key_0" in plugin._locks

            # 插入第 6 个，应淘汰 key_0
            await plugin._get_lock("key_5")
            assert len(plugin._locks) == 5
            assert "key_0" not in plugin._locks
            assert "key_5" in plugin._locks

            # 访问 key_1（使其 move_to_end），然后插入 key_6，应淘汰 key_2
            await plugin._get_lock("key_1")
            await plugin._get_lock("key_6")
            assert "key_2" not in plugin._locks
            assert "key_1" in plugin._locks
            assert "key_6" in plugin._locks
        finally:
            EmojiKitchenPlugin._MAX_LOCKS = original_max


class TestOnMessageExceptionSafe:
    """测试 on_message 异常兜底"""

    @pytest.fixture
    def plugin(self, tmp_path):
        from main import EmojiKitchenPlugin
        plugin = EmojiKitchenPlugin.__new__(EmojiKitchenPlugin)
        plugin.config = {"max_probe_dates": 10, "cdn_url": "https://www.gstatic.cn",
                         "request_timeout": 10, "notfound_expire_days": 7}
        plugin.data_dir = tmp_path
        plugin.cache_dir = tmp_path / "cache"
        plugin.notfound_dir = tmp_path / "notfound"
        plugin.dates_cache_path = tmp_path / "dates_cache.json"
        plugin.date_list = list(HARDCODED_DATES)
        plugin.metadata_dir = tmp_path / "metadata"
        plugin.metadata_index = {}
        plugin._locks = OrderedDict()
        plugin._global_lock = asyncio.Lock()
        plugin._session = None
        plugin._session_lock = asyncio.Lock()
        plugin._semaphore = asyncio.Semaphore(4)
        plugin._update_task = None
        plugin.cache_dir.mkdir(parents=True, exist_ok=True)
        plugin.notfound_dir.mkdir(parents=True, exist_ok=True)
        plugin.metadata_dir.mkdir(parents=True, exist_ok=True)
        return plugin

    @pytest.mark.asyncio
    async def test_on_message_exception_safe(self, plugin):
        """_fetch_emoji_image 抛异常时 on_message 不冒泡"""
        event = MagicMock()
        event.message_str = "😀😍"
        event.image_result = MagicMock(return_value="image_result")
        event.stop_event = MagicMock()

        with patch.object(plugin, "_fetch_emoji_image", new_callable=AsyncMock,
                          side_effect=RuntimeError("unexpected crash")):
            results = []
            # 不应该抛异常
            async for r in plugin.on_message(event):
                results.append(r)
            assert len(results) == 0
            event.stop_event.assert_not_called()


class TestNotfoundCleanup:
    """测试 notfound 过期/hash 不匹配时自动清理文件"""

    @pytest.fixture
    def plugin(self, tmp_path):
        from main import EmojiKitchenPlugin
        plugin = EmojiKitchenPlugin.__new__(EmojiKitchenPlugin)
        plugin.context = MagicMock()
        plugin.config = {"notfound_expire_days": 7}
        plugin.data_dir = tmp_path
        plugin.cache_dir = tmp_path / "cache"
        plugin.notfound_dir = tmp_path / "notfound"
        plugin.dates_cache_path = tmp_path / "dates_cache.json"
        plugin.date_list = list(HARDCODED_DATES)
        plugin.metadata_dir = tmp_path / "metadata"
        plugin.metadata_index = {}
        plugin._locks = OrderedDict()
        plugin._global_lock = asyncio.Lock()
        plugin._session = None
        plugin._session_lock = asyncio.Lock()
        plugin._semaphore = asyncio.Semaphore(4)
        plugin._update_task = None
        plugin.cache_dir.mkdir(parents=True, exist_ok=True)
        plugin.notfound_dir.mkdir(parents=True, exist_ok=True)
        plugin.metadata_dir.mkdir(parents=True, exist_ok=True)
        return plugin

    def test_is_notfound_expired_cleanup(self, plugin):
        """过期的 notfound 文件被自动删除"""
        data = {
            "timestamp": int(time.time()) - 8 * 86400,  # 8 天前，已过期
            "dates_tried": 34,
            "date_list_hash": plugin._get_date_list_hash(),
        }
        path = plugin.notfound_dir / "test_key.json"
        path.write_text(json.dumps(data))
        assert path.exists()

        result = plugin._is_notfound("test_key")
        assert result is False
        # 文件应该被清理
        assert not path.exists()

    def test_is_notfound_hash_mismatch_cleanup(self, plugin):
        """hash 不匹配的 notfound 文件被自动删除"""
        data = {
            "timestamp": int(time.time()),  # 未过期
            "dates_tried": 34,
            "date_list_hash": "wrong_hash",  # hash 不匹配
        }
        path = plugin.notfound_dir / "test_key.json"
        path.write_text(json.dumps(data))
        assert path.exists()

        result = plugin._is_notfound("test_key")
        assert result is False
        # 文件应该被清理
        assert not path.exists()
