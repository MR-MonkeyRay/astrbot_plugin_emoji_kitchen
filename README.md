# Emoji Kitchen

一个 AstrBot 插件，自动合成 Google Emoji Kitchen 图片。当你在聊天中发送两个 emoji 时，插件会自动返回它们的合成图片。

## ✨ 功能特性

- 🎨 **自动识别**：无需命令，直接发送两个 emoji 即可触发
- 🚀 **智能缓存**：本地文件缓存，避免重复下载
- ⚡ **高效探测**：从 GitHub 样本数据提取并更新日期候选列表，按日期探测可用图片
- 🛡️ **防重机制**：当探测覆盖完整日期列表且全部 404 时，标记 notfound 避免重复请求
- 🔧 **灵活配置**：支持自定义 CDN、代理、超时等参数
- 🌐 **并发控制**：异步并发限流，保护 CDN 资源

## 📦 安装

### 通过插件市场安装（推荐）

1. 在 AstrBot 控制台进入插件市场
2. 搜索 "Emoji Kitchen"
3. 点击安装

### 手动安装

```bash
cd astrbot/plugins
git clone https://github.com/MR-MonkeyRay/astrbot_plugin_emoji_kitchen.git
cd astrbot_plugin_emoji_kitchen
pip install -r requirements.txt
```

## 🎯 使用方式

在聊天中直接发送两个 emoji，插件会自动识别并返回合成图片：

```
😀😎  → 返回合成图片
🐱🐶  → 返回合成图片
❤️🔥  → 返回合成图片
```

**注意**：
- 消息中必须恰好包含 2 个 emoji，且两个 emoji 必须紧挨着（如 `😀😎`；`😀 😎` 中间有空格不会触发）
- 不能包含其他文字或字符
- 不是所有 emoji 组合都有合成图片，未命中时插件不会回复消息

## ⚙️ 配置说明

在 AstrBot 插件配置页面可调整以下参数（所有配置留空时，默认使用 `www.gstatic.cn` 作为 CDN、`ghfast.top` 作为 GitHub 代理）：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `cdn_source` | string (下拉) | （空） | Emoji 图片 CDN 来源：`www.gstatic.cn（国内推荐）`、`www.gstatic.com（国际）`、`自定义` |
| `cdn_url` | string | （空） | 自定义 CDN 地址（仅在 `cdn_source` 选择「自定义」时生效） |
| `github_proxy_source` | string (下拉) | （空） | GitHub 代理来源：`ghfast.top`、`gh-proxy.com`、`自定义`、`不使用代理` |
| `github_proxy` | string | （空） | 自定义 GitHub 代理地址（仅在 `github_proxy_source` 选择「自定义」时生效） |
| `extra_dates` | text | （空） | 用户手动追加的日期，每行一个（格式如 `20251029`） |
| `notfound_expire_days` | int | `7` | 不存在标记过期天数 |
| `request_timeout` | int | `10` | 单次 HTTP 请求超时（秒） |
| `max_probe_dates` | int | `10` | 每次请求最多探测的日期数 |

## 🔍 工作原理

1. **消息监听**：监听所有消息，使用正则表达式提取 emoji
2. **触发检测**：检测到恰好 2 个 emoji 且消息无多余字符时触发
3. **Codepoint 转换**：将 emoji 转为 Unicode codepoint
4. **URL 构造**：根据 codepoint 构造 Google Emoji Kitchen CDN URL
5. **日期探测**：按日期列表（硬编码 + 远程更新）探测可用的合成图片
6. **缓存机制**：
   - 本地缓存命中直接返回
   - 未命中则从 CDN 下载并缓存
   - 当 `max_probe_dates` 覆盖完整日期列表且全部返回 404 时，写入 notfound 标记以减少重复请求
7. **原子写入**：使用临时文件 + 原子重命名保证缓存文件完整性

## 🙏 致谢

- [Google Emoji Kitchen](https://emojikitchen.dev/) - 提供 emoji 合成服务
- [xsalazar/emoji-kitchen-backend](https://github.com/xsalazar/emoji-kitchen-backend) - 日期列表数据源

## 📄 许可

本项目遵循 MIT 许可证。

## 🔗 相关链接

- [AstrBot 官方文档](https://docs.astrbot.app/)
- [插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)
- [项目仓库](https://github.com/MR-MonkeyRay/astrbot_plugin_emoji_kitchen)
