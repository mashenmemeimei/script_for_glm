# 智谱 GLM Coding Plan 抢购脚本

基于 **Playwright + httpx** 的 Python 抢购方案，用于自动化抢购智谱开放平台的 GLM Coding Plan 订阅。

## 原理

脚本分两个阶段工作：

### Phase 1: 捕获 (Capture)
- 使用 Playwright 打开 Chromium 浏览器
- 拦截所有 XHR/Fetch 请求，自动提取 API 端点和参数
- 获取登录后的 Cookie
- 保存到 `~/.glm_rush/config.json`

### Phase 2: 抢购 (Rush)
- 通过 HTTP HEAD 请求同步服务器时钟
- 高精度定时，在放量时刻精准开火
- 并发引擎：前 5 秒 10 路零延迟爆发 → 之后 5 路自适应间隔
- 自动重试直到成功或达到上限

## 安装

```bash
pip install -r requirements_glm_rush.txt
playwright install chromium
```

## 套餐选择

智谱 GLM Coding Plan 有三个套餐等级，脚本通过 `--plan` 参数指定目标：

| 套餐 | 价格 | 适用场景 |
|------|------|----------|
| **lite** (默认) | ¥49/月 | 个人开发者 / 小型项目 / 轻量迭代 |
| **pro** | ¥149/月 | 5倍额度 / GLM-5 优先 / 中大型项目 |
| **max** | ¥469/月 | 20倍额度 / 最高并发优先级 |

> **重要**：脚本捕获的是你**在浏览器中实际点击的那个套餐按钮**的 API 参数。请确保在捕获阶段点击正确的套餐！

## 使用方法

### 1. 首次使用：捕获模式（以 Lite 为例）

先打开浏览器，手动登录并进入 GLM Coding 页面，让脚本捕获 API 和 Cookie：

```bash
# 抢购 Lite 套餐 (¥49/月)
python glm_coding_rush.py --mode capture --plan lite

# 抢购 Pro 套餐 (¥149/月)
python glm_coding_rush.py --mode capture --plan pro
```

操作步骤：
1. 脚本自动打开浏览器并导航到 GLM Coding 页面
2. 如果未登录，请在浏览器中登录你的智谱账号
3. 找到你要抢购的套餐，**点击对应套餐的「立即订阅」按钮**（如 Lite ¥49/月）
4. 脚本会自动检测你点击的是哪个套餐
5. 回到终端按 Enter 确认

配置会自动保存到 `~/.glm_rush/config.json`。

### 2. 干跑测试

验证 Cookie 和接口是否正常（不实际抢购）：

```bash
python glm_coding_rush.py --mode rush --plan lite --dry-run
```

### 3. 定时抢购

```bash
# 使用已保存的配置自动抢购 Lite 套餐
python glm_coding_rush.py --mode rush --plan lite

# 或手动提供 Cookie
python glm_coding_rush.py --mode rush --plan lite --cookie "SESSION=xxx; token=yyy"
```

默认在每天 **10:00:00 北京时间** 自动开火，脚本会显示倒计时。

### 4. 完整模式（推荐）

一条命令完成全部流程：捕获 → 自动进入等待 → 准时抢购

```bash
# 抢购 Lite 套餐
python glm_coding_rush.py --mode full --plan lite
```

## 高级参数

```bash
python glm_coding_rush.py --mode rush --plan lite \
  --burst 15 \             # 极速阶段并发数 (默认 10)
  --concurrency 8 \        # 普通阶段并发数 (默认 5)
  --burst-duration 6 \     # 极速阶段持续秒数 (默认 5)
  --max-retries 3000 \     # 最大重试次数 (默认 2000)
  --fire-ahead 80 \        # 提前开火 ms (默认 50)
  --proxy http://127.0.0.1:7890  # HTTP 代理
```

### 参数调优建议

| 场景 | burst | concurrency | burst_duration | fire_ahead |
|------|-------|-------------|----------------|------------|
| 网络延迟低 (<20ms) | 10 | 5 | 5 | 30 |
| 网络延迟中 (20-50ms) | 15 | 8 | 6 | 50-80 |
| 网络延迟高 (>50ms) | 20 | 10 | 8 | 100-150 |

## 抢购策略

基于社区经验总结：

1. **放量时间**：每天上午 10:00 北京时间
2. **黄金窗口**：10:00:00 - 10:05:00（前5分钟成功率最高）
3. **并发策略**：
   - 前 20 次：零延迟爆发
   - 前 5 秒：10 路高并发
   - 之后：5 路 + 30-100ms 自适应间隔
4. **重试机制**：EXPIRE 状态立即重试，最多 2000 次

## 成功标志

当脚本输出以下信息时表示抢购成功：

```
[Rush] ✅ 抢购成功！支付链接:
    https://open.bigmodel.cn/pay/...
[Rush] ⚠️  请立即打开支付链接完成付款（通常15分钟内有效）
```

**请立即复制支付链接在浏览器中打开完成付款！**

## 常见问题

### 浏览器打开后无法登录/点击无反应
- **已修复(v1.1)**：全局路由拦截 `page.route("**/*")` 会阻塞每个网络请求导致 JS 交互失效
- 现改为非阻塞事件监听 `page.on("request")` + `page.on("response")`，不影响页面正常交互
- 登录流程（手机验证码/微信扫码/OAuth）完全正常

### Cookie 过期
- Cookie 有效期通常 1-2 小时，建议在 9:50 左右重新捕获
- 如果抢购时返回 401/403，说明 Cookie 已过期，需要重新登录

### WAF 限流
- 过于频繁的请求可能触发 WAF 限流（405/403），脚本已内置随机抖动降低检测概率
- 建议提前 5 分钟启动脚本，让时间同步稳定

## 依赖

- Python 3.8+
- httpx（异步 HTTP 客户端）
- Playwright（浏览器自动化）
- Chromium（Playwright 自动安装）

## 免责声明

本脚本仅供学习研究使用。使用本脚本可能违反智谱平台的服务条款，请自行承担风险。脚本作者不对因使用本脚本导致的账号封禁、财产损失等后果负责。
