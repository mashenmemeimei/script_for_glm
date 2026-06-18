# 智谱 GLM Coding Plan 抢购脚本

> 基于 Playwright + httpx 的混合抢购方案，专门针对每天 10:00 放量的 [GLM Coding Plan](https://open.bigmodel.cn/glm-coding) 限购活动。

---

## 📑 目录

- [项目简介](#项目简介)
- [功能特性](#功能特性)
- [安装](#安装)
- [快速开始](#快速开始)
- [⭐ 手动抓包模式 (build-config)](#-手动抓包模式-build-config)  **← 推荐优先看**
- [其他运行模式](#其他运行模式)
- [套餐说明](#套餐说明)
- [WAF 令牌与时效](#waf-令牌与时效)
- [常见问题](#常见问题)
- [已知限制与免责声明](#已知限制与免责声明)

---

## 项目简介

智谱 GLM Coding Plan 每天上午 **10:00** 放出限量名额（49 元 lite / 149 元 pro / 469 元 max），需要在几秒钟内点击「订阅」才能抢到。本脚本提供：

1. **Playwright 捕获阶段**：打开真实浏览器，拦截 API 请求，自动提取 Cookie、Authorization、WAF 令牌
2. **httpx 抢购阶段**：用异步高并发引擎在 10:00 准时发出请求，10 路并发 × 5 秒爆发 + 5 路普通重试
3. **手动抓包模式**：当 Playwright 启动的浏览器被 WAF 识别（antidom.js 反爬）时，可以**完全绕开 Playwright**，用真实浏览器手动 F12 抓包后构造配置

---

## 功能特性

| 功能 | 说明 |
|------|------|
| 🎯 多种运行模式 | capture / rush / full / build-config 四种模式，按需选择 |
| 💳 多套餐支持 | 连续包月 lite/pro/max（推荐），兼容 lite/pro/max 旧版 |
| 🛡️ WAF 防护 | 自动 405 退避（2~5s）、请求指纹动态化、降低并发避免触发限流 |
| 🔍 智能匹配 | CJK+English 混合关键词归一化，精确识别"连续包月 Lite" 等变体 |
| ⏰ 精准对时 | HTTP HEAD 请求同步服务器时间，忙等精确到 0.5ms |
| 🔌 代理支持 | `--proxy` 参数支持 HTTP 代理，避开 IP 拉黑 |
| 🔧 强制触发 | `force_subscribe_attempt` 绕过 disabled 按钮，捕获真实 API |
| 📋 cURL 自动解析 | 粘 DevTools 复制的 cURL 命令即可自动提取 URL/header/productId |

---

## 安装

### 1. 克隆/下载项目

```bash
git clone <repo-url> script_for_glm
cd script_for_glm
```

### 2. 安装 Python 依赖

```bash
pip install httpx playwright
```

### 3. 安装 Playwright Chromium 浏览器

```bash
playwright install chromium
```

> ⚠️  Playwright 仅在 `capture` / `full` 模式下需要。如果只用 `rush` 和 `build-config` 模式，可以跳过第 3 步。

---

## 快速开始

### 新手（IP 未被封、首次使用）

```bash
# 1. 启动 Playwright 浏览器，自动捕获 Cookie 和端点
python glm_coding_rush.py --mode capture --plan monthly_lite

# 2. 在打开的浏览器里登录智谱账号，点击 Lite 套餐的"订阅"按钮
# 3. 回终端按 Enter，配置保存到 ~/.glm_rush/config.json

# 4. 10:00 准时抢购（脚本会自动等到 09:59:58）
python glm_coding_rush.py --mode rush --plan monthly_lite
```

### 一步到位（捕获 + 等待 + 抢购）

```bash
python glm_coding_rush.py --mode full --plan monthly_lite
```

### IP 已被 WAF 标记（推荐 ⭐ 手动抓包模式）

```bash
python glm_coding_rush.py --mode build-config --print-template
# 编辑生成的模板，填入抓包数据
python glm_coding_rush.py --mode build-config --config-input ~/.glm_rush/manual_capture.json
```

---

## ⭐ 手动抓包模式 (build-config)

> **本节是 README 的重点**。当以下情况发生时，Playwright 启动的浏览器也会被 WAF 拦截，此时必须使用 build-config 模式：
> - IP 已被阿里云 WAF 拉黑（曾经高频请求过智谱 API）
> - 页面显示"当前访问人数过多"，且短时间内不会解除
> - 浏览器被 antidom.js / interfaceacting.js 识别为自动化工具

### 设计理念

**build-config 模式完全绕开 Playwright**，转而让你在真实浏览器（Edge / Chrome / Firefox）里：
1. 正常访问智谱页面（真实浏览器指纹通过 WAF JS 挑战）
2. 用 F12 手动抓包
3. 把抓到的 cURL 命令粘到脚本

脚本会解析 cURL 中的 URL、Cookie、Authorization、productId，构造出与 `capture` 模式完全等价的 `config.json`，然后用 `rush` 模式发起抢购。

### 完整操作步骤

#### Step 1: 切换到干净的网络

如果你的当前 IP 已被标记：

```
断开电脑 WiFi
打开手机 4G 热点（注意是 4G，不是 5G）
电脑连接热点
```

> 💡  4G 移动网络通常能分配到新 IP，能绕过之前电脑 IP 的拉黑。

#### Step 2: 启动 build-config 模板

```bash
python glm_coding_rush.py --mode build-config --print-template
```

会输出一个 JSON 模板，包含三个 cURL 占位符（preview / check / pay）。

或者直接生成模板文件：

```bash
python glm_coding_rush.py --mode build-config
# 会在 ~/.glm_rush/manual_capture.json 写入模板
```

#### Step 3: 真实浏览器登录智谱

1. 打开 Edge 或 Chrome（**普通模式**，不要无痕）
2. 访问 https://open.bigmodel.cn/glm-coding
3. 按 **F12** 打开 DevTools
4. 切换到 **Network** 标签
5. 勾上 **Preserve log**（保留历史请求）

> ⚠️  **关键**：勾上 Preserve log，否则刷新页面会清空请求记录。

#### Step 4: 真实交互通过 WAF JS 挑战

在页面上：
- **真实移动鼠标** 5~10 次（让 antidom.js 检测到人类行为）
- **真实滚动** 页面几次
- 等待 5~10 秒（让 JS 挑战跑完）

如果页面显示"当前访问人数过多"：
- **持续刷新页面**（F5），等待"人数过多"状态解除
- 通常在 09:55（抢购前 5 分钟）会有窗口期
- 也可换不同 IP 试

#### Step 5: 点击 Lite 套餐的「订阅」按钮

套餐按钮变成可点击状态时：
- **立刻点击**（窗口期可能只有 10~30 秒）
- DevTools Network 会立即出现多个 `/api/...` 请求

#### Step 6: 抓取三个关键请求的 cURL

在 Network 面板里找到这三个请求，**右键 → Copy → Copy as cURL (bash)**：

| 请求路径 | 用途 | 是否必须 |
|----------|------|----------|
| `/api/biz/pay/batch-preview` | preview（下单预览） | ✅ 必须 |
| `/api/coding-plan/subscribe/check` | check（订单校验） | ⚠️ 推荐 |
| `/api/coding-plan/subscribe/pay` | pay（获取支付链接） | ⚠️ 推荐 |

> 💡  如果 check / pay 端点抓不到，脚本会用推断 URL（成功率较低，建议尽量抓全）。

#### Step 7: 把 cURL 粘到模板

打开 `~/.glm_rush/manual_capture.json`（或 `--print-template` 的输出），把 cURL 粘到对应字段：

```json
{
  "curl_commands": {
    "preview": "curl 'https://open.bigmodel.cn/api/biz/pay/batch-preview?decode__1570=xxxxx' \\\n  -H 'authorization: Bearer eyJ...' \\\n  -H 'cookie: acw_tc=...' \\\n  -H 'content-type: application/json' \\\n  --data-raw '{\"productId\":\"prod_lite_49\"}'",
    "check": "curl 'https://open.bigmodel.cn/api/coding-plan/subscribe/check?decode__1570=yyyyy' ...",
    "pay": "curl 'https://open.bigmodel.cn/api/coding-plan/subscribe/pay?decode__1570=zzzzz' ..."
  }
}
```

> ⚠️  **必须保留 `decode__1570` 参数**！这是阿里云 WAF 的令牌，缺失会导致 405。

#### Step 8: 构造配置

```bash
python glm_coding_rush.py --mode build-config --config-input ~/.glm_rush/manual_capture.json
```

输出类似：

```
✅ 配置已构建并保存到 C:\Users\xxx\.glm_rush\config.json

📊 配置摘要:
  - 目标套餐: 个人连续包月 Lite (¥49/月)
  - Cookie 长度: 3532 字符
  - Authorization: ✅ 已设置 (341 字符)
  - productId: prod_lite_49
  - preview 端点: ✅ ...preview?decode__1570=xxxxx
  - check 端点: ✅ ...check?decode__1570=yyyyy
  - pay 端点: ✅ ...pay?decode__1570=zzzzz

🚀 下一步:
  # 1. 先 dry-run 验证接口
  python glm_coding_rush.py --mode rush --plan monthly_lite --dry-run
  # 2. 抢购
  python glm_coding_rush.py --mode rush --plan monthly_lite
```

### 4 种输入方式（任选其一）

build-config 模式支持 4 种输入方式，按推荐度排序：

#### 方式 A: cURL 命令（强烈推荐，最方便）

```bash
# preview 的 cURL 已包含 cookie + authorization + productId
python glm_coding_rush.py --mode build-config \
  --preview-curl "curl 'https://...' -H 'authorization: Bearer ...' ..." \
  --check-url "https://...?decode__1570=yyy" \
  --pay-url "https://...?decode__1570=zzz"
```

#### 方式 B: JSON 文件（适合反复修改）

```bash
# 1. 打印模板
python glm_coding_rush.py --mode build-config --print-template

# 2. 编辑 ~/.glm_rush/manual_capture.json

# 3. 构造配置
python glm_coding_rush.py --mode build-config --config-input ~/.glm_rush/manual_capture.json
```

#### 方式 C: 单独 CLI 参数

```bash
python glm_coding_rush.py --mode build-config \
  --cookie "acw_tc=..." \
  --authorization "Bearer eyJ..." \
  --product-id "prod_lite_49" \
  --preview-url "https://...?decode__1570=xxx" \
  --check-url "https://...?decode__1570=yyy" \
  --pay-url "https://...?decode__1570=zzz"
```

#### 方式 D: 混合（cURL 提取通用字段 + URL 单独指定）

```bash
python glm_coding_rush.py --mode build-config \
  --preview-curl "curl ..." \
  --check-url "..." \
  --pay-url "..."
```

### ⚠️ build-config 模式的限制

- **WAF 令牌时效**：`decode__1570` 有效期约 5~10 分钟。建议 **09:59:30 重新抓一次**再抢购。
- **无法捕获不存在的请求**：如果页面一直"人数过多"灰色按钮，没有 API 请求被发出，cURL 也无从获取。
- **无法绕过 IP 拉黑**：build-config 仍需要你的 IP 至少能正常通过 WAF JS 挑战，否则连手动抓包都做不到。

---

## 其他运行模式

### `capture` 模式 —— Playwright 自动捕获

```bash
python glm_coding_rush.py --mode capture --plan monthly_lite
```

- 启动 Playwright 浏览器，访问智谱页面
- 自动拦截 API 请求，提取 Cookie / Authorization / 端点 URL
- 等用户登录、点击订阅按钮（按 Enter 继续）
- 保存到 `~/.glm_rush/config.json`

**适合**：IP 没被封、首次使用。

### `rush` 模式 —— 仅抢购

```bash
python glm_coding_rush.py --mode rush --plan monthly_lite
# 加 --dry-run 仅做连通性测试，不实际抢购
python glm_coding_rush.py --mode rush --plan monthly_lite --dry-run
```

- 加载 `~/.glm_rush/config.json`（由 capture / build-config 阶段生成）
- 时间同步后等到 09:59:58，10:00 准时开火
- 10 路并发爆发 5 秒，5 路普通重试 10 分钟

**参数调优**：
```bash
# 调高并发（不推荐，WAF 会拦截）
python glm_coding_rush.py --mode rush --plan monthly_lite --burst 5 --concurrency 3

# 调整开火提前量（应对网络延迟）
python glm_coding_rush.py --mode rush --plan monthly_lite --fire-ahead 100

# 使用代理
python glm_coding_rush.py --mode rush --plan monthly_lite --proxy http://127.0.0.1:7890
```

### `full` 模式 —— 捕获 + 抢购一气呵成

```bash
python glm_coding_rush.py --mode full --plan monthly_lite
```

- 复用 1 小时内的 config，或重新 capture
- 然后自动进入 rush 模式
- 适合**首次使用 + 抢购当天**的场景

---

## 套餐说明

### ⭐ 默认目标（连续包月 / 个人订阅，**推荐**）

| 套餐键 | 名称 | 价格 | 描述 |
|--------|------|------|------|
| `monthly_lite` | 个人连续包月 Lite | ¥49/月 | 基础套餐（默认） |
| `monthly_pro` | 个人连续包月 Pro | ¥149/月 | 5 倍额度，GLM-5 优先 |
| `monthly_max` | 个人连续包月 Max | ¥469/月 | 20 倍额度，最高并发 |

### 兼容旧版（不指定周期，**慎用**）

| 套餐键 | 名称 | 价格 | 风险 |
|--------|------|------|------|
| `lite` | Lite | ¥49/月 | 可能误匹配"年付 Lite" / "一次性 Lite" |
| `pro` | Pro | ¥149/月 | 同上 |
| `max` | Max | ¥469/月 | 同上 |

**如何选择**：

```bash
# 默认（推荐）
python glm_coding_rush.py --plan monthly_lite

# 想要更多额度
python glm_coding_rush.py --plan monthly_pro
```

> 💡  套餐检测的关键词（如"连续包月 Lite"）会归一化处理，能同时匹配"连续包月 Lite" / "连续包月Lite" / "连续包月_Lite" 等多种写法。

---

## WAF 令牌与时效

### decode__1570 是什么

智谱 API 部署在阿里云 WAF 后面，每个 URL 后面都有一个 `decode__1570=xxx` 参数：
- 这是 WAF 颁发的**会话令牌**
- 类似于 CSRF token，用于验证请求来自合法浏览器会话
- **有效期 5~10 分钟**，过期需要重新获取

### 何时需要重新抓包

| 场景 | 是否需要重新抓 |
|------|---------------|
| 抢购前 dry-run 测试 | ✅ 需要（令牌可能已过期几分钟） |
| 抢购前 5 分钟 | ✅ 强烈建议 |
| 抢购失败后立即重试 | ⚠️ 看错误码（405 = 令牌过期） |
| 切换 IP 后 | ✅ 必须要 |
| 重新登录后 | ✅ 必须要 |

### 重新抓包的快捷命令

```bash
# 第一次：生成模板
python glm_coding_rush.py --mode build-config

# 09:55 / 09:59 / 失败后：重新抓 → 重新构造
# 在真实浏览器里抓 3 个 cURL
python glm_coding_rush.py --mode build-config --config-input ~/.glm_rush/manual_capture.json
```

---

## 常见问题

### Q1: dry-run 一直返回 405 怎么办？

405 = WAF 拦截。常见原因：
1. **IP 被拉黑**：换手机 4G 热点
2. **WAF 令牌过期**：重新跑 build-config 抓 cURL
3. **Cookie 过期**：重新登录智谱账号
4. **缺少 Authorization**：检查 build-config 输出，确保 Authorization 已设置

### Q2: 页面显示"当前访问人数过多"怎么办？

这是**服务端限流**，不是按钮问题。**点击按钮不会触发任何 API 请求**。

解决方案：
1. **持续刷新**（F5），等待限流解除
2. **错峰**：在 09:55:00（抢购前 5 分钟）抓包，此时流量小
3. **换 IP**：4G 热点或代理
4. 实在不行就**等明天**

### Q3: preview 200 但 check_ok=0 怎么办？

历史问题。原因：
- 之前抓的 check 端点是 `/api/biz/label/whitelist/check`（白名单查询），不是订单 check
- 已在 [build-config 模式] 中修复

解决方法：
- **重新跑 build-config**，确保 check 的 cURL 是 `/api/coding-plan/subscribe/check`
- 检查 cURL 里的 URL，**不能是 whitelist**

### Q4: 抢购时大量 405 怎么办？

参考 [Q1](#q1-dry-run-一直返回-405-怎么办)。同时：
- 调低并发：`--burst 2 --concurrency 1`
- 用代理：`--proxy http://...`
- 脚本已经自动 2~5 秒退避，不要关闭

### Q5: 配置保存到哪个文件？

`~/.glm_rush/config.json`（Windows 上是 `C:\Users\用户名\.glm_rush\config.json`）

可以删除这个文件强制重新生成：
```bash
# Windows
del %USERPROFILE%\.glm_rush\config.json

# macOS / Linux
rm ~/.glm_rush/config.json
```

---

## 已知限制与免责声明

### 已知限制

1. **WAF 反爬**：智谱部署了 antidom.js / interfaceacting.js 等反爬脚本，Playwright 启动的浏览器可能被识别。build-config 模式部分缓解此问题。
2. **WAF 令牌时效**：`decode__1570` 有效期仅 5~10 分钟，需要在抢购前重新抓取。
3. **IP 限流**：高频请求会触发阿里云 WAF IP 拉黑，需换网络（4G 热点）或使用代理。
4. **抢购窗口**：每日 10:00 放量，几秒钟内售罄，自动化抢购的成功率受网络延迟、IP 限流、WAF 拦截影响。
5. **套餐缺货**：即使所有技术问题解决，套餐本身可能已售罄（"人数过多"也可能是这个原因）。

### 免责声明

⚠️  **本脚本仅供学习和技术研究使用。**

- 请遵守智谱开放平台的服务条款
- 高频请求可能违反 API 使用政策
- 抢购结果不保证成功，技术方案不构成商业承诺
- 因使用本脚本导致的任何账号问题，开发者不承担责任

---

## 命令速查

| 用途 | 命令 |
|------|------|
| 首次使用（捕获+抢购） | `python glm_coding_rush.py --mode full --plan monthly_lite` |
| 仅捕获 | `python glm_coding_rush.py --mode capture --plan monthly_lite` |
| 仅抢购 | `python glm_coding_rush.py --mode rush --plan monthly_lite` |
| 抢购前验证 | `python glm_coding_rush.py --mode rush --plan monthly_lite --dry-run` |
| IP 被封/手动抓包 | `python glm_coding_rush.py --mode build-config --print-template` |
| 用 cURL 构造配置 | `python glm_coding_rush.py --mode build-config --preview-curl "curl ..."` |
| 查看帮助 | `python glm_coding_rush.py --help` |

---

## 项目结构

```
script_for_glm/
├── glm_coding_rush.py   # 主脚本（包含所有模式）
├── README.md            # 本文件
└── ~/.glm_rush/         # 运行时配置目录
    ├── config.json           # 主配置（capture / build-config 生成）
    └── manual_capture.json   # build-config 模板（可选）
```

---

**祝抢购顺利！🍀**
