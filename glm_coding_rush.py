#!/usr/bin/env python3
"""
智谱 GLM Coding Plan 抢购脚本
===============================
基于 Playwright + httpx 的混合抢购方案：
  - Phase 1: Playwright 打开浏览器，拦截 API 请求，自动提取 Cookie 和接口参数
  - Phase 2: 使用 httpx 高并发异步引擎，在放量时刻精准抢购

用法:
  1. 仅捕获模式（先探路，不抢购）:
     python glm_coding_rush.py --mode capture

  2. 定时抢购模式（默认 09:59:58 自动开抢）:
     python glm_coding_rush.py --mode rush --plan lite

  3. 完整模式（先捕获再抢购）:
     python glm_coding_rush.py --mode full --plan lite

依赖安装:
  pip install httpx playwright
  playwright install chromium

参考:
  - 放量时间: 每天上午 10:00 (北京时间)
  - 目标页面: https://open.bigmodel.cn/glm-coding
  - GitHub qtaxm/glm-rush (Tampermonkey 版本)
"""

import asyncio
import time
import json
import sys
import os
import re
import hashlib
import random
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, List, Any
from dataclasses import dataclass, field
from collections import defaultdict

import httpx

# ============================================================================
# 配置
# ============================================================================

CST = timezone(timedelta(hours=8))  # 北京时间

# 目标页面
TARGET_URL = "https://open.bigmodel.cn/glm-coding"
BASE_URL = "https://open.bigmodel.cn"

# ── 套餐定义 ──────────────────────────────────────────────
# GLM Coding Plan 有三个套餐等级，每个套餐对应特定的 SKU/产品ID
# 脚本通过 POST 请求体中的 productId / sku / planType 等字段区分套餐
# 在捕获阶段，你点击哪个套餐的按钮，脚本就记录哪个套餐的参数
PLANS = {
    "lite": {
        "name": "Lite",
        "price": "¥49/月",
        "desc": "基础套餐 - 适合个人开发者 / 小型项目",
        # 以下是请求体中可能出现的套餐标识关键词（用于自动检测）
        "keywords": ["lite", "basic", "standard", "coding_lite", "glm_coding_lite"],
    },
    "pro": {
        "name": "Pro",
        "price": "¥149/月",
        "desc": "专业套餐 - 5倍额度 / GLM-5 优先 / 中大型项目",
        "keywords": ["pro", "professional", "coding_pro", "glm_coding_pro"],
    },
    "max": {
        "name": "Max",
        "price": "¥469/月",
        "desc": "旗舰套餐 - 20倍额度 / 最高并发优先级",
        "keywords": ["max", "ultimate", "coding_max", "glm_coding_max"],
    },
}

# 已知的 API 端点模式（会自动从浏览器拦截中更新）
API_PATTERNS = {
    "preview": "/api/coding-plan/subscribe/preview",   # 下单预览
    "check": "/api/coding-plan/subscribe/check",       # 校验订单
    "pay": "/api/coding-plan/subscribe/pay",           # 获取支付链接
    "plan_info": "/api/coding-plan/info",              # 计划信息
}

# 抢购参数
@dataclass
class RushConfig:
    """抢购引擎配置"""
    # 套餐
    plan: str = "lite"                # 目标套餐: lite / pro / max

    # 并发引擎
    burst_concurrency: int = 10       # 前 N 秒高并发数
    normal_concurrency: int = 5       # 普通并发数
    burst_duration: float = 5.0       # 高并发持续秒数
    max_retries: int = 600            # 普通阶段最大重试秒数（默认 10 分钟，原 2000=33 分钟太夸张）

    # 间隔策略 (ms)
    burst_count: int = 20             # 前 N 次零延迟爆发
    fast_interval_ms: int = 30        # 快速重试间隔
    slow_interval_ms: int = 100       # 慢速重试间隔
    jitter_pct: float = 0.30          # 间隔随机抖动比例

    # 超时
    request_timeout: float = 5.0      # 单次请求超时(秒)

    # 放量时间
    release_hour: int = 10
    release_minute: int = 0
    fire_ahead_ms: int = 50           # 提前多少毫秒开火 (应对网络延迟)


# ============================================================================
# 时间同步
# ============================================================================

class TimeSync:
    """通过 HTTP HEAD 请求同步服务器时间，计算本地与服务器的时钟偏差"""

    def __init__(self, url: str = BASE_URL):
        self.url = url
        self.offset_ms: float = 0.0  # 本地时间 - 服务器时间

    async def sync(self, samples: int = 5) -> float:
        """多次采样取中位数，返回偏差(ms)"""
        offsets = []
        async with httpx.AsyncClient() as client:
            for _ in range(samples):
                try:
                    t0 = time.time()
                    resp = await client.head(self.url, timeout=5.0)
                    t1 = time.time()
                    rtt = (t1 - t0) / 2
                    server_time_str = resp.headers.get("date", "")
                    if server_time_str:
                        from email.utils import parsedate_to_datetime
                        server_dt = parsedate_to_datetime(server_time_str)
                        server_ts = server_dt.timestamp()
                        local_ts = t0 + rtt
                        offset_ms = (local_ts - server_ts) * 1000
                        offsets.append(offset_ms)
                except Exception:
                    pass
                await asyncio.sleep(0.1)

        if offsets:
            offsets.sort()
            self.offset_ms = offsets[len(offsets) // 2]  # 中位数
            print(f"[TimeSync] 服务器时钟偏差: {self.offset_ms:+.1f}ms "
                  f"(采样{samples}次, RTT≈{abs(self.offset_ms):.0f}ms)")
        else:
            print("[TimeSync] 警告: 无法同步时间，使用本地时钟")

        return self.offset_ms

    def server_now(self) -> float:
        """返回估算的服务器当前时间戳"""
        return time.time() - self.offset_ms / 1000

    def ms_until_target(self) -> float:
        """距离下一次放量还有多少毫秒"""
        now_cst = datetime.fromtimestamp(self.server_now(), tz=CST)
        target = now_cst.replace(
            hour=RushConfig.release_hour,
            minute=RushConfig.release_minute,
            second=0, microsecond=0
        )
        if now_cst >= target:
            # 如果已经过了今天 10:00，瞄准明天
            target = target + timedelta(days=1)
        diff_sec = (target - now_cst).total_seconds()
        return diff_sec * 1000


# ============================================================================
# API 拦截器 (Playwright 阶段)
# ============================================================================

class APIInterceptor:
    """使用 Playwright 打开页面，拦截 API 请求以获取真实端点和参数"""

    def __init__(self, headless: bool = False, target_plan: str = "lite"):
        self.headless = headless
        self.target_plan = target_plan
        self.captured_requests: List[Dict] = []
        self.captured_cookies: Dict[str, str] = {}
        self.captured_headers: Dict[str, str] = {}
        # 按 URL 关键词分类存储
        self.endpoints: Dict[str, Dict] = {}
        self.detected_plan: Optional[str] = None
        self.authorization: Optional[str] = None  # Bearer token
        self._playwright = None
        self._browser = None
        self._page = None

    async def start(self):
        """启动浏览器并打开目标页面"""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            print("[ERROR] 请安装 Playwright: pip install playwright && playwright install chromium")
            sys.exit(1)

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process",
            ]
        )
        context = await self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            java_script_enabled=True,
        )
        self._page = await context.new_page()

        # 非阻塞事件监听
        self._page.on("request", self._on_request)
        self._page.on("response", self._on_response)

        print(f"[Browser] 正在打开 {TARGET_URL} ...")
        print(f"[Browser] 当前目标套餐: {PLANS[self.target_plan]['name']} ({PLANS[self.target_plan]['price']})")
        await self._page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
        # 等待页面 JS 充分初始化（SPA 框架渲染需要时间）
        await asyncio.sleep(4)

        # 提取 Cookie
        cookies = await context.cookies()
        self.captured_cookies = {c["name"]: c["value"] for c in cookies}
        self.captured_headers["cookie"] = "; ".join(
            f"{k}={v}" for k, v in self.captured_cookies.items()
        )

        print(f"[Browser] 已捕获 {len(self.captured_cookies)} 个 Cookie")

        # ── 核心: 从页面内部提取套餐配置（无需点击按钮） ──
        await self._scrape_plan_data()

        # 提取 Authorization token（页面加载完通常已有匿名/缓存 token）
        self.authorization = await self._extract_auth_token()

        # 打印捕获结果
        print(f"[Browser] 已拦截 {len(self.captured_requests)} 个 API 请求")
        for ep_name, ep_info in self.endpoints.items():
            print(f"  ✓ {ep_name}: {ep_info['method']} {ep_info['url']}")

        print("\n" + "=" * 60)
        print(f"[Browser] 🎯 目标套餐: {PLANS[self.target_plan]['name']} ({PLANS[self.target_plan]['price']})")
        print(f"[Browser]    {PLANS[self.target_plan]['desc']}")
        if self.plan_metadata:
            print(f"[Browser] 📦 从页面提取到套餐元数据:")
            for k, v in self.plan_metadata.items():
                if k != "all_plans":
                    print(f"[Browser]    {k}: {v}")
        print("[Browser]")
        print("[Browser] 请在浏览器中完成以下操作:")
        print("  1. 如果未登录，请先登录账号")
        print(f"  2. ⭐ 关键：在页面上找到 [{PLANS[self.target_plan]['name']}] 套餐卡片，")
        print(f"     点击它的「订阅/购买」按钮（即使弹窗显示「售罄」也请点一下，")
        print(f"     这样能拦截到真实的产品 ID 和请求参数）")
        print("  3. 关闭弹窗后，按 Enter 继续...")
        print("=" * 60)

    # ── 页面数据提取 ────────────────────────────────

    async def _extract_auth_token(self) -> Optional[str]:
        """
        关键修复：从 localStorage / sessionStorage / cookies 中提取 token，
        用于在抢购阶段发 Authorization: Bearer <token> 头。

        智谱的 API 不认 Cookie，所以光发 cookie 会被 1001 拒掉。
        """
        # JS 把所有 storage 里的键值都拿出来，再让 Python 选
        extract_js = """
        (() => {
            const ls = {}, ss = {};
            for (let i = 0; i < localStorage.length; i++) {
                const k = localStorage.key(i);
                if (k) ls[k] = localStorage.getItem(k) || '';
            }
            for (let i = 0; i < sessionStorage.length; i++) {
                const k = sessionStorage.key(i);
                if (k) ss[k] = sessionStorage.getItem(k) || '';
            }
            return JSON.stringify({localStorage: ls, sessionStorage: ss});
        })()
        """
        try:
            raw = await self._page.evaluate(extract_js)
            storage = json.loads(raw) if raw else {}
        except Exception as e:
            print(f"[Browser] ⚠️ 读取 storage 失败: {e}", flush=True)
            storage = {}

        # 候选 token 键（按优先级）
        token_keys = [
            "token", "access_token", "authorization", "auth_token",
            "user_token", "bigmodel_token", "glm_token", "jwt", "bearer",
            "access-token", "x-auth-token",
        ]

        def score(key: str, value: str) -> int:
            """给候选打分，分越高越像真的 token"""
            if not value or len(value) < 16:
                return -1
            s = 0
            lk = key.lower()
            # JWT 通常以 ey 开头
            if value.startswith("ey"):
                s += 100
            # 键名匹配
            for i, k in enumerate(token_keys):
                if k in lk:
                    s += (len(token_keys) - i) * 5
            # 长度合理（一般 > 50）
            if len(value) > 50:
                s += 10
            elif len(value) > 20:
                s += 5
            return s

        candidates: List[tuple] = []  # (score, source, value)
        for k, v in (storage.get("localStorage") or {}).items():
            sc = score(k, v)
            if sc > 0:
                candidates.append((sc, f"localStorage.{k}", v))
        for k, v in (storage.get("sessionStorage") or {}).items():
            sc = score(k, v)
            if sc > 0:
                candidates.append((sc, f"sessionStorage.{k}", v))
        for ck, cv in self.captured_cookies.items():
            sc = score(ck, cv)
            if sc > 0:
                candidates.append((sc, f"cookie.{ck}", cv))

        if not candidates:
            print("[Browser] ⚠️ 未找到 Authorization token ——"
                  "抢购时会缺 Authorization 头", flush=True)
            return None

        candidates.sort(reverse=True, key=lambda x: x[0])
        best_score, best_source, best_value = candidates[0]
        preview = best_value[:30] + "..." if len(best_value) > 30 else best_value
        print(f"[Browser] 🔑 提取到 token (score={best_score}, 源={best_source}, "
              f"长度={len(best_value)}, 前30={preview})", flush=True)
        if best_score < 50:
            print(f"[Browser] ⚠️ token 候选分较低 ({best_score})，可能选错，"
                  f"请检查上面其他候选", flush=True)
        return best_value

    async def _scrape_plan_data(self):
        """
        从页面内部提取套餐配置 — 无需点击按钮。
        即使页面显示「暂时售罄」，前端 Store 中也已加载了 productId/sku 等数据。
        提取来源优先级: Vuex/Pinia Store → window 全局变量 → <script> JSON → DOM 元素
        """
        self.plan_metadata: Dict[str, Any] = {}
        self.plan_post_bodies: Dict[str, str] = {}  # plan_key → POST body

        scrape_js = """
        (() => {
            const result = {
                windowKeys: [],
                plansFromDom: [],
                vuexState: null,
                piniaState: null,
                globalState: null,
                scriptData: [],
            };

            // ── 1. window 上的全局状态 ─────────────────
            const WINDOW_KEYS = [
                '__INITIAL_STATE__', '__NEXT_DATA__', '__NUXT__',
                '__APP_STATE__', '__STORE__', '__DATA__',
                'pageData', 'appData', 'planData', 'productData',
                '__ZHIPU_STATE__', '__BIGMODEL_STATE__',
            ];
            for (const key of WINDOW_KEYS) {
                if (window[key] !== undefined) {
                    try {
                        result.globalState = {key, value: JSON.parse(JSON.stringify(window[key]))};
                        result.windowKeys.push(key);
                    } catch (e) {
                        result.globalState = {key, value: String(window[key]).substring(0, 2000)};
                        result.windowKeys.push(key);
                    }
                    break;  // 取第一个存在的
                }
            }

            // ── 2. Vue 3 应用状态 ──────────────────────
            try {
                const appEl = document.querySelector('#app, [data-v-app], .app, main');
                if (appEl && appEl.__vue_app__) {
                    const vm = appEl.__vue_app__;
                    result.vuexState = {hasApp: true};
                }
            } catch (e) {}

            // ── 3. Pinia Store ─────────────────────────
            try {
                const allElements = document.querySelectorAll('*');
                for (const el of allElements) {
                    if (el.__vue_app__) {
                        const app = el.__vue_app__;
                        if (app.config && app.config.globalProperties) {
                            const store = app.config.globalProperties.$store;
                            if (store && store.state) {
                                result.piniaState = JSON.parse(JSON.stringify(store.state));
                                break;
                            }
                        }
                        // Try pinia
                        if (app._context && app._context.provides) {
                            const provides = app._context.provides;
                            for (const pk of Object.keys(provides)) {
                                if (pk.includes('store') || pk.includes('pinia')) {
                                    try {
                                        result.piniaState = JSON.parse(JSON.stringify(provides[pk]));
                                    } catch (e) {}
                                    break;
                                }
                            }
                        }
                        break;
                    }
                }
            } catch (e) {}

            // ── 4. 页面中 <script> 标签里包含的 JSON ────
            try {
                const scripts = document.querySelectorAll('script[type="application/json"], script[id]');
                for (const s of scripts) {
                    const text = s.textContent || '';
                    if (text.includes('productId') || text.includes('planType') ||
                        text.includes('sku') || text.includes('planId') || text.includes('plans')) {
                        result.scriptData.push({
                            id: s.id || '',
                            type: s.type || 'text/javascript',
                            snippet: text.substring(0, 3000)
                        });
                    }
                }
            } catch (e) {}

            // ── 5. DOM 中的套餐卡片数据 ─────────────────
            try {
                const planCards = document.querySelectorAll(
                    '[class*="plan"], [class*="Plan"], [class*="pricing"], [class*="Pricing"], ' +
                    '[class*="product"], [class*="Product"], [class*="package"], [class*="Package"], ' +
                    '[class*="subscription"], [data-plan], [data-product]'
                );
                for (const card of planCards) {
                    const text = (card.textContent || '').trim();
                    const dataset = {};
                    for (const attr of card.attributes) {
                        if (attr.name.startsWith('data-')) {
                            dataset[attr.name] = attr.value;
                        }
                    }
                    if (text.length > 0 && (text.includes('¥') || text.includes('月') || text.includes('年'))) {
                        result.plansFromDom.push({
                            text: text.substring(0, 500),
                            className: card.className || '',
                            dataset: dataset,
                        });
                    }
                }
            } catch (e) {}

            // ── 6. 按钮上的数据 ─────────────────────────
            try {
                const buttons = document.querySelectorAll('button, a[class*="btn"], [class*="subscribe"]');
                for (const btn of buttons) {
                    const text = (btn.textContent || '').trim();
                    if (text.includes('订阅') || text.includes('购买') || text.includes('售罄') ||
                        text.includes('Subscribe') || text.includes('Buy')) {
                        const parentCard = btn.closest('[class*="plan"], [class*="Plan"], [class*="card"], [class*="Card"]');
                        const parentText = parentCard ? parentCard.textContent.trim() : '';
                        const btnDataset = {};
                        for (const attr of btn.attributes) {
                            if (attr.name.startsWith('data-') || attr.name === 'id') {
                                btnDataset[attr.name] = attr.value;
                            }
                        }
                        if (!result.plansFromDom.find(p => p.text === parentText)) {
                            result.plansFromDom.push({
                                text: parentText.substring(0, 500),
                                btnText: text,
                                className: (parentCard && parentCard.className) || '',
                                dataset: btnDataset,
                            });
                        }
                    }
                }
            } catch (e) {}

            // ── 7. 全局搜索 productId / planId ───────────
            try {
                const htmlText = document.documentElement.innerHTML;
                const idPatterns = [
                    /productId["\\s:=]+["\\s]*([a-zA-Z0-9_-]+)/gi,
                    /planId["\\s:=]+["\\s]*([a-zA-Z0-9_-]+)/gi,
                    /sku["\\s:=]+["\\s]*([a-zA-Z0-9_-]+)/gi,
                    /planType["\\s:=]+["\\s]*([a-zA-Z0-9_-]+)/gi,
                    /"plan"\\s*:\\s*"([^"]+)"/gi,
                    /coding_plan_(lite|pro|max)/gi,
                ];
                result.productIds = [];
                for (const pattern of idPatterns) {
                    let match;
                    const seen = new Set();
                    while ((match = pattern.exec(htmlText)) !== null) {
                        const val = match[1] || match[0];
                        if (!seen.has(val)) {
                            seen.add(val);
                            result.productIds.push(val);
                        }
                        if (result.productIds.length > 20) break;
                    }
                    if (result.productIds.length > 20) break;
                }
            } catch (e) {}

            return JSON.stringify(result);
        })();
        """

        try:
            raw = await self._page.evaluate(scrape_js)
            data = json.loads(raw)
        except Exception as e:
            print(f"[Browser] ⚠️ 页面数据提取失败: {e}")
            data = {}

        # ── 解析提取结果 ────────────────────────────────
        # 优先从 productIds 识别套餐
        product_ids = data.get("productIds", [])
        if product_ids:
            print(f"[Browser] 从页面 HTML 提取到 {len(product_ids)} 个 ID: {product_ids[:10]}")

        # 从 DOM 卡片中解析套餐信息
        for card in data.get("plansFromDom", []):
            text = (card.get("text", "") + " " + card.get("btnText", "")).lower()
            dataset = card.get("dataset", {})
            for plan_key, plan_info in PLANS.items():
                if self._match_plan_keyword(plan_info, text, text):  # url_lower = text_lower 即可
                    if self.detected_plan != plan_key:
                        self.detected_plan = plan_key
                        print(f"[Browser] 🔍 DOM 检测到套餐: {plan_info['name']} "
                              f"({plan_info['price']})", flush=True)
                    # 提取可能的 productId —— 关键修复：绑到具体 plan_key
                    for dk, dv in dataset.items():
                        if "id" in dk.lower() or "sku" in dk.lower() or "product" in dk.lower():
                            # 用 "lit__productId" / "pro__productId" / "max__productId" 分开存
                            self.plan_metadata[f"{plan_key}__{dk}"] = str(dv)
                    break  # 一个 card 只对应一个套餐

        # 从 productIds 反查套餐
        if not self.detected_plan and product_ids:
            for pid in product_ids:
                pid_lower = pid.lower()
                for plan_key, plan_info in PLANS.items():
                    # 强匹配（单词边界），避免 "max" 命中 "maxxxx" 之类
                    strong_hit = any(
                        re.search(rf"\b{re.escape(kw)}\b", pid_lower)
                        for kw in plan_info["keywords"] if "_" in kw
                    )
                    if strong_hit:
                        self.detected_plan = plan_key
                        self.plan_metadata[f"{plan_key}__productId"] = pid
                        print(f"[Browser] 🔍 从 HTML ID 检测到套餐: {plan_info['name']} "
                              f"({plan_info['price']}), productId={pid}", flush=True)
                        break
                if self.detected_plan:
                    break

        # 从 Vuex/Pinia store 提取
        state = data.get("piniaState") or data.get("vuexState") or data.get("globalState")
        if state:
            state_str = json.dumps(state, ensure_ascii=False) if isinstance(state, dict) else str(state)
            self.plan_metadata["has_store_data"] = True
            state_dict = state if isinstance(state, dict) else {}
            # 递归查找 plan 相关字段
            def find_in_obj(obj, target_keys, path=""):
                if isinstance(obj, dict):
                    for key in target_keys:
                        if key in obj:
                            val = obj[key]
                            if isinstance(val, (str, int, float, bool)):
                                self.plan_metadata[path + key] = str(val)
                    for k, v in obj.items():
                        find_in_obj(v, target_keys, path + k + ".")
                elif isinstance(obj, list):
                    for i, item in enumerate(obj):
                        if isinstance(item, dict):
                            find_in_obj(item, target_keys, path + f"[{i}].")
            find_in_obj(state_dict, ["productId", "planId", "sku", "planType", "plan", "id", "name"])
            # 尝试从 store 中匹配套餐关键词
            for plan_key, plan_info in PLANS.items():
                for kw in plan_info["keywords"]:
                    if kw in state_str.lower():
                        if not self.detected_plan:
                            self.detected_plan = plan_key
                            print(f"[Browser] 🔍 从 Store 检测到套餐: {plan_info['name']} "
                                  f"({plan_info['price']})")
                        break

        if self.detected_plan:
            print(f"[Browser] ✅ 已识别套餐: {PLANS[self.detected_plan]['name']}")
        else:
            print(f"[Browser] ⚠️ 未能自动识别套餐 — 将使用 --plan 指定的 {PLANS[self.target_plan]['name']}")

        # ── 尝试构造 POST body ──────────────────────────
        # 关键修复：每个套餐用各自的 productId（之前是所有套餐共用一个）
        for plan_key in PLANS:
            pid = (
                self.plan_metadata.get(f"{plan_key}__productId")
                or self.plan_metadata.get("productId")
            )
            if pid:
                body = json.dumps({"productId": pid}, separators=(",", ":"))
                self.plan_post_bodies[plan_key] = body
        if self.plan_post_bodies.get(self.target_plan):
            print(f"[Browser] 📝 已构造 {self.target_plan} POST body: "
                  f"{self.plan_post_bodies[self.target_plan]}", flush=True)
        else:
            print(f"[Browser] ⚠️ 未能为目标套餐 {PLANS[self.target_plan]['name']} "
                  f"构造 POST body —— 建议在浏览器中点击 [{PLANS[self.target_plan]['name']}] "
                  f"订阅按钮后再回车", flush=True)

    @staticmethod
    def _match_plan_keyword(plan_info: Dict, text_lower: str, url_lower: str) -> bool:
        """
        严格匹配套餐关键词，避免 "max" 误命中 "maxAge" / "maximize" / "elite套餐"。

        - 强关键词（带下划线如 glm_coding_lite）：用 ASCII 字母边界 (?<![a-zA-Z])...(?![a-zA-Z])
          （用 ASCII 边界而非 \\b 是因为 CJK 字符在 Python re Unicode 模式下属于 \\w，会破坏 \\b 语义）
        - 弱关键词（如 lite/max/pro/basic）：必须紧邻 plan/package/套餐/订阅 上下文，
          且关键词本身也用 ASCII 字母边界 —— 这样 "elite套餐" 里的 "lite" 不会被误命中
        """
        text = text_lower
        url = url_lower
        # 强关键词
        for kw in plan_info["keywords"]:
            if "_" not in kw:
                continue
            pat = rf"(?<![a-zA-Z]){re.escape(kw)}(?![a-zA-Z])"
            if re.search(pat, text) or re.search(pat, url):
                return True
        # 弱关键词 + 上下文
        contexts = (r"\bplan\b", r"\bpackage\b", r"\bproduct\b", "套餐", "订阅")
        for kw in plan_info["keywords"]:
            if "_" in kw:
                continue
            kw_pat = rf"(?<![a-zA-Z]){re.escape(kw)}(?![a-zA-Z])"
            for ctx in contexts:
                if re.search(rf"{ctx}.*{kw_pat}|{kw_pat}.*{ctx}", text, re.DOTALL):
                    return True
        return False

    def _record_request(self, url: str, method: str, headers: Dict, post_data: Optional[str]):
        """记录并分类一个 API 请求"""
        self.captured_requests.append({
            "url": url,
            "method": method,
            "headers": headers,
            "post_data": post_data,
            "timestamp": time.time(),
        })

        # ── 检测套餐类型（仅对订阅/价格相关请求生效，避免被无关 API 干扰） ──
        # 之前的问题是：任何含 "max" 子串的请求体（maxAge/maxCount/...）都会误判为 Max 套餐
        url_lower = url.lower()
        is_plan_request = any(
            marker in url_lower
            for marker in ("subscribe", "plan", "pricing", "package", "coding")
        )
        if is_plan_request and post_data:
            post_lower = post_data.lower()
            for plan_key, plan_info in PLANS.items():
                if self._match_plan_keyword(plan_info, post_lower, url_lower):
                    if self.detected_plan != plan_key:
                        self.detected_plan = plan_key
                        print(f"\n[Capture] 🔍 检测到套餐: {plan_info['name']} "
                              f"({plan_info['price']})", flush=True)
                    break  # 一个请求只对应一个套餐，避免 lite/pro/max 互相覆盖

        # 自动分类端点
        path = url.replace(BASE_URL, "")
        matched = False
        for name, pattern in API_PATTERNS.items():
            if pattern in url:
                self.endpoints[name] = {
                    "url": url,
                    "method": method,
                    "headers": headers,
                    "post_data": post_data,
                }
                matched = True
                break

        if not matched:
            # 兜底：按路径关键词匹配
            for name in ["preview", "check", "pay", "subscribe", "plan"]:
                if name in path.lower():
                    key = name if name != "subscribe" else "preview"
                    if key not in self.endpoints:
                        self.endpoints[key] = {
                            "url": url,
                            "method": method,
                            "headers": headers,
                            "post_data": post_data,
                        }
                    break

        # 全量记录
        if len(self.captured_requests) <= 30:
            print(f"[Capture] {method} {path}")

    def _on_request(self, request):
        """非阻塞 request 事件监听"""
        try:
            url = request.url
            method = request.method
            resource_type = request.resource_type

            # 只记录 bigmodel.cn 的 XHR/Fetch 请求
            if resource_type in ("xhr", "fetch") and "bigmodel.cn" in url:
                headers = dict(request.headers)
                post_data = request.post_data
                self._record_request(url, method, headers, post_data)
        except Exception:
            pass  # 静默忽略，绝不阻断请求

    def _on_response(self, response):
        """非阻塞 response 事件监听 — 补充请求体信息"""
        try:
            request = response.request
            url = request.url
            resource_type = request.resource_type

            if resource_type in ("xhr", "fetch") and "bigmodel.cn" in url:
                # 如果请求阶段已经记录了，跳过
                already_recorded = any(
                    r["url"] == url and r["method"] == request.method
                    for r in self.captured_requests[-10:]  # 只检查最近 10 条
                )
                if not already_recorded:
                    headers = dict(request.headers)
                    post_data = request.post_data
                    self._record_request(url, request.method, headers, post_data)
        except Exception:
            pass

    async def wait_for_user(self):
        """
        等待用户完成登录 + 点击目标套餐按钮。
        页面数据已在 start() 中自动提取，这里只需等用户完成操作后确认。
        """
        target_name = PLANS[self.target_plan]['name']
        print("\n" + "-" * 40)
        print(f"[Browser] 1. 如果尚未登录，请先在浏览器中完成登录")
        print(f"[Browser] 2. ⭐ 在套餐卡片上点击 [{target_name}] 的「订阅/购买」按钮")
        print(f"[Browser]    （售罄时弹窗会提示，但请求已被脚本拦截，可直接关闭弹窗）")
        print(f"[Browser] 3. 完成后回到终端，按 Enter 继续...")
        print("-" * 40)
        input()  # 阻塞等待 Enter

        # 重新提取 Cookie（登录后可能有新的）
        context = self._page.context
        cookies = await context.cookies()
        self.captured_cookies = {c["name"]: c["value"] for c in cookies}
        self.captured_headers["cookie"] = "; ".join(
            f"{k}={v}" for k, v in self.captured_cookies.items()
        )

        # 登录后 token 通常才完整 —— 再提取一次
        fresh_token = await self._extract_auth_token()
        if fresh_token:
            self.authorization = fresh_token

        print(f"\n[Browser] 更新后共 {len(self.captured_cookies)} 个 Cookie", flush=True)
        print(f"[Browser] 共拦截 {len(self.captured_requests)} 个 API 请求", flush=True)
        if self.endpoints:
            print(f"[Browser] 识别到 {len(self.endpoints)} 个关键端点:", flush=True)
            for name, ep in self.endpoints.items():
                print(f"  - {name}: {ep['method']} ...{ep['url'][-50:]}", flush=True)
        else:
            print(f"[Browser] ⚠️ 未拦截到订阅 API 请求（页面售罄时正常现象）", flush=True)
            print(f"[Browser] 将使用从页面源码提取的套餐参数 + 推测的 API 端点", flush=True)

        # ── 套餐确认（关键修复：以用户目标为最高优先级） ──
        if self.detected_plan:
            plan = PLANS[self.detected_plan]
            print(f"\n[Browser] 🔍 自动检测到套餐: {plan['name']} ({plan['price']})", flush=True)
            if self.detected_plan != self.target_plan:
                print(f"[Browser] ⚠️  检测到的套餐 ({plan['name']}) "
                      f"与目标套餐 ({target_name}) 不一致!", flush=True)
                print(f"[Browser] ✅ 将以你指定的 [{target_name}] 为准（用户优先级最高）",
                      flush=True)
        else:
            print(f"\n[Browser] 🔍 未自动识别到套餐 —— 将直接使用你指定的 [{target_name}]",
                  flush=True)

        # 列出已构造的 POST body
        if self.plan_post_bodies:
            print(f"\n[Browser] 📦 已构造的 POST body:", flush=True)
            for pk, body in self.plan_post_bodies.items():
                marker = " ← 目标" if pk == self.target_plan else ""
                print(f"  - {pk}{marker}: {body[:120]}", flush=True)

    async def close(self):
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    def export_config(self) -> Dict:
        """导出捕获的配置供 RushEngine 使用"""
        return {
            "cookies": self.captured_cookies,
            "cookie_header": self.captured_headers.get("cookie", ""),
            "endpoints": self.endpoints,
            "all_requests": self.captured_requests,
            "detected_plan": self.detected_plan or self.target_plan,
            "target_plan": self.target_plan,
            "plan_metadata": getattr(self, "plan_metadata", {}),
            "plan_post_bodies": getattr(self, "plan_post_bodies", {}),
            "authorization": self.authorization,  # Bearer token（关键）
        }


# ============================================================================
# 并发抢购引擎
# ============================================================================

class RushEngine:
    """高并发异步抢购引擎"""

    def __init__(
        self,
        cookie_str: str,
        config: RushConfig,
        endpoints: Optional[Dict[str, Dict]] = None,
        proxy: Optional[str] = None,
        detected_plan: Optional[str] = None,
        plan_metadata: Optional[Dict] = None,
        plan_post_bodies: Optional[Dict[str, str]] = None,
        authorization: Optional[str] = None,
    ):
        self.cookie_str = cookie_str
        self.config = config
        self.endpoints = endpoints or {}
        self.proxy = proxy
        self.detected_plan = detected_plan
        self.plan_metadata = plan_metadata or {}
        self.plan_post_bodies = plan_post_bodies or {}
        self.authorization = authorization

        # ── 补齐缺失的端点（从已知模式推断） ──
        self._ensure_endpoints()

        # 运行时状态
        self.stats = defaultdict(int)
        self.start_time: float = 0
        self.stop_event = asyncio.Event()
        self.concurrency_level = config.normal_concurrency
        self.session: Optional[httpx.AsyncClient] = None
        self._semaphore: Optional[asyncio.Semaphore] = None

        # 结果
        self.result: Optional[Dict] = None
        self.payment_url: Optional[str] = None

    def _ensure_endpoints(self):
        """如果网络捕获阶段没有拦截到 API 端点，从已知模式推断"""
        # 注意：base 只到 host，后面拼 "/api/..." 时不要再带 /api 前缀
        observed_api_base = BASE_URL
        for ep in self.endpoints.values():
            url = ep.get("url", "")
            if "/api/" in url:
                idx = url.find("/api/")
                observed_api_base = url[:idx]  # 保留 host，不重复 /api
                break

        SUBSCRIBE_PATH_CANDIDATES = [
            "/api/coding-plan/subscribe",
            "/api/v1/coding-plan/subscribe",
            "/api/coding/subscribe",
            "/api/subscribe",
        ]

        for ep_name in ("preview", "check", "pay"):
            if ep_name in self.endpoints:
                continue
            for candidate in SUBSCRIBE_PATH_CANDIDATES:
                url = f"{observed_api_base}{candidate}/{ep_name}"
                self.endpoints[ep_name] = {
                    "url": url,
                    "method": "POST",
                    "headers": {},
                    "post_data": self._build_post_body(ep_name),
                }
                break

        if self.endpoints:
            print("[Rush] API 端点配置:")
            for name, ep in self.endpoints.items():
                print(f"  {name}: {ep['method']} {ep['url']}")

    def _build_post_body(self, ep_name: str) -> str:
        """根据页面提取的元数据构造 POST 请求体。

        优先级（关键修复）：
          1) 用户在 --plan 显式指定的套餐 + 它专属的 POST body
          2) 自动检测到的套餐 + 它专属的 POST body
          3) 退而求其次：用 plan_metadata 里任意 productId/planId/sku
          4) 实在没有就用 {"planType": 用户套餐}
        """
        # 1) 用户指定优先
        user_plan = self.config.plan
        if user_plan in self.plan_post_bodies:
            return self.plan_post_bodies[user_plan]
        # 2) 检测到的兜底
        if self.detected_plan and self.detected_plan in self.plan_post_bodies:
            return self.plan_post_bodies[self.detected_plan]
        # 3) 用通用 metadata
        body = {}
        for key in ("productId", "planId", "sku", "planType"):
            if key in self.plan_metadata:
                body[key] = self.plan_metadata[key]
        if body:
            return json.dumps(body, separators=(",", ":"))
        # 4) 纯保底
        return json.dumps({"planType": user_plan}, separators=(",", ":"))

    def _build_client(self) -> httpx.AsyncClient:
        """构建带指纹伪装的 HTTP 客户端"""
        # 关键修复：服务器要求 Authorization 头（cookie 单发会被 1001 拒）
        auth_header = self._resolve_authorization_header()

        headers = {
            "accept": "application/json, text/plain, */*",
            "accept-language": random.choice([
                "zh-CN,zh;q=0.9,en;q=0.8",
                "zh-CN,zh;q=0.9",
            ]),
            "content-type": "application/json",
            "cookie": self.cookie_str,
            "origin": BASE_URL,
            "referer": TARGET_URL,
            "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            # 关键修复：补上现代浏览器 fetch 必带的 sec-fetch-* 头
            # 缺这三个，CDN/WAF 会识别为非浏览器请求而挡掉（返回 405）
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "sec-fetch-dest": "empty",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            # 随机请求指纹 —— x-timestamp 改为每次请求更新（之前是建 client 时一次，
            # WAF 会把同一时间戳的请求当爬虫）
            "x-request-id": hashlib.md5(
                f"{time.time()}{random.random()}".encode()
            ).hexdigest()[:16],
        }
        if auth_header:
            headers["authorization"] = auth_header
        else:
            print("[Rush] ⚠️ 未配置 Authorization —— 几乎一定会被 1001 拒掉。\n"
                  "       请先跑 --mode capture，登录后脚本会自动从 localStorage 提取 token",
                  flush=True)

        # 把 client 存起来，让 _make_request 读取它的默认头
        client = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(self.config.request_timeout),
            proxy=self.proxy,
            http2=True,
            follow_redirects=True,
        )
        # 关键修复：把 auth 头单独缓存，方便 run() 打印
        client._glm_auth = auth_header
        return client

    def _resolve_authorization_header(self) -> Optional[str]:
        """
        解析 Authorization 头的最终值。优先级：
          1) 显式传入的 self.authorization
          2) 捕获的 endpoint headers 里的 authorization
          3) plan_metadata 里的 authorization
        自动加 'Bearer ' 前缀（如果还没有）
        """
        candidates = []
        if self.authorization:
            candidates.append(self.authorization)
        for ep in self.endpoints.values():
            h = (ep or {}).get("headers") or {}
            v = h.get("authorization") or h.get("Authorization")
            if v:
                candidates.append(v)
        v = (self.plan_metadata or {}).get("authorization")
        if v:
            candidates.append(v)

        for raw in candidates:
            if not raw:
                continue
            s = str(raw).strip()
            if not s:
                continue
            if s.lower().startswith("bearer "):
                return s
            return f"Bearer {s}"
        return None

    async def _jitter_sleep(self, base_ms: int):
        """带抖动的异步等待"""
        jitter = random.uniform(-self.config.jitter_pct, self.config.jitter_pct)
        wait_ms = base_ms * (1 + jitter)
        await asyncio.sleep(wait_ms / 1000)

    async def _make_request(
        self, endpoint_name: str, method: str, url: str, data: Optional[str] = None
    ) -> Optional[httpx.Response]:
        """发起单次请求，带重试"""
        try:
            # 关键修复：每次请求都更新 x-timestamp / x-request-id，
            # 避免 WAF 把同一时间戳的一波请求当爬虫
            per_call = {
                "x-request-id": hashlib.md5(
                    f"{time.time()}{random.random()}".encode()
                ).hexdigest()[:16],
                "x-timestamp": str(int(time.time() * 1000)),
            }
            if method == "POST":
                resp = await self.session.post(
                    url, content=data, headers=per_call
                )
            else:  # GET
                resp = await self.session.get(url, headers=per_call)
            return resp
        except Exception as e:
            self.stats["errors"] += 1
            return None

    async def _attempt_subscribe(self) -> bool:
        """
        尝试一次完整的订阅流程: preview → check → pay
        返回 True 表示成功获取到支付链接
        """
        # 调试用：前 3 次失败时打印响应体（避免日志爆炸）
        debug_shown = self.stats.get("debug_shown", 0)
        async with self._semaphore:
            try:
                # Step 1: Preview - 创建预订单
                preview_ep = self.endpoints.get("preview", {})
                if preview_ep:
                    resp = await self._make_request(
                        "preview",
                        preview_ep.get("method", "POST"),
                        preview_ep["url"],
                        preview_ep.get("post_data"),
                    )
                    if resp and resp.status_code == 200:
                        self.stats["preview_ok"] += 1
                        try:
                            body = resp.json()
                            biz_id = body.get("data", {}).get("bizId") or body.get("bizId")
                            if biz_id:
                                # Step 2: Check - 校验订单
                                check_ep = self.endpoints.get("check", {})
                                if check_ep:
                                    check_data = check_ep.get("post_data", "{}")
                                    # 尝试替换 bizId
                                    try:
                                        cd = json.loads(check_data)
                                        cd["bizId"] = biz_id
                                        check_data = json.dumps(cd)
                                    except (json.JSONDecodeError, KeyError):
                                        pass

                                    resp2 = await self._make_request(
                                        "check",
                                        check_ep.get("method", "POST"),
                                        check_ep["url"],
                                        check_data,
                                    )
                                    if resp2 and resp2.status_code == 200:
                                        self.stats["check_ok"] += 1
                                        body2 = resp2.json()
                                        # 检查是否过期
                                        if "EXPIRE" in str(body2).upper():
                                            self.stats["expired"] += 1
                                            return False
                                        # Step 3: 获取支付链接
                                        pay_url = (
                                            body2.get("data", {}).get("payUrl")
                                            or body2.get("payUrl")
                                        )
                                        if pay_url:
                                            self.payment_url = pay_url
                                            self.result = body2
                                            return True
                            else:
                                # 可能返回了错误（如已售罄）
                                if any(kw in str(body) for kw in ["售罄", "none", "empty", "null"]):
                                    self.stats["sold_out"] += 1
                                elif debug_shown < 3:
                                    print(f"[Rush] preview 200 但无 bizId, body={json.dumps(body, ensure_ascii=False)[:200]}",
                                          flush=True)
                                    self.stats["debug_shown"] = debug_shown + 1
                        except Exception:
                            pass
                    elif resp:
                        self.stats[f"preview_{resp.status_code}"] += 1
                        # 调试：前 3 次失败时打印 body 摘要，立刻知道为什么
                        if debug_shown < 3:
                            text = (resp.text or "")
                            # 405 来自 WAF，body 是 HTML，只取 <title>
                            if resp.status_code == 405 and "<title>" in text:
                                m = re.search(r"<title>([^<]*)</title>", text)
                                snippet = f"<WAF page title='{m.group(1) if m else '?'}'>"
                            else:
                                snippet = text[:120].replace("\n", " ")
                            print(f"[Rush] preview HTTP {resp.status_code}: {snippet}",
                                  flush=True)
                            self.stats["debug_shown"] = debug_shown + 1
                    else:
                        self.stats["preview_no_resp"] += 1

                return False

            except Exception as e:
                self.stats["errors"] += 1
                if debug_shown < 3:
                    print(f"[Rush] attempt exception: {e!r}", flush=True)
                    self.stats["debug_shown"] = debug_shown + 1
                return False

    async def _burst_worker(self, worker_id: int):
        """高并发 worker —— 零延迟爆发模式"""
        count = 0
        while not self.stop_event.is_set() and count < self.config.max_retries:
            # 自适应间隔
            if count < self.config.burst_count:
                pass  # 零延迟
            elif count < self.config.burst_count + 100:
                await self._jitter_sleep(self.config.fast_interval_ms)
            else:
                await self._jitter_sleep(self.config.slow_interval_ms)

            if self.stop_event.is_set():
                break

            success = await self._attempt_subscribe()
            count += 1

            if success:
                self.stop_event.set()
                return

    async def run(self, fire_at_timestamp: Optional[float] = None):
        """
        启动抢购引擎
        Args:
            fire_at_timestamp: Unix 时间戳，到达后立即开始。如果为 None，立即开始。
        """
        self.start_time = time.time()
        self.session = self._build_client()
        self._semaphore = asyncio.Semaphore(self.config.burst_concurrency)

        # 等待开火时间
        if fire_at_timestamp is not None:
            now = time.time()
            wait_ms = (fire_at_timestamp - now) * 1000
            if wait_ms > 0:
                target_dt = datetime.fromtimestamp(fire_at_timestamp, tz=CST)
                print(f"\n[Rush] ⏰ 等待放量时间 {target_dt.strftime('%H:%M:%S.%f')[:-3]} ...")
                print(f"[Rush] 距离开火还有 {wait_ms/1000:.1f} 秒")
                # 分两段等待：先 sleep 到接近目标，再忙等精准触发
                if wait_ms > 200:
                    await asyncio.sleep((wait_ms - 150) / 1000)
                # 最后 150ms 忙等
                while time.time() < fire_at_timestamp:
                    await asyncio.sleep(0.0005)  # 0.5ms 精度

        print(f"\n[Rush] 🔥 开火! {datetime.now(CST).strftime('%H:%M:%S.%f')[:-3]}", flush=True)
        plan = PLANS[self.config.plan]
        print(f"[Rush] 🎯 抢购套餐: {plan['name']} ({plan['price']}) — {plan['desc']}", flush=True)

        # 关键诊断：打印实际使用的 URL 和 Authorization
        preview_url = (self.endpoints.get("preview") or {}).get("url", "<missing>")
        check_url = (self.endpoints.get("check") or {}).get("url", "<missing>")
        pay_url = (self.endpoints.get("pay") or {}).get("url", "<missing>")
        print(f"[Rush] 📡 端点 URL:", flush=True)
        print(f"    preview: {preview_url}", flush=True)
        print(f"    check:   {check_url}", flush=True)
        print(f"    pay:     {pay_url}", flush=True)
        # 标记 URL 是捕获的还是脚本猜的
        for ep_name in ("preview", "check", "pay"):
            ep = self.endpoints.get(ep_name) or {}
            # 如果 headers 里有真实内容（来自浏览器），算"已捕获"
            captured = bool(ep.get("headers"))
            tag = "✅ 捕获" if captured else "⚠️  推断"
            print(f"    [{tag}] {ep_name}", flush=True)

        auth_header = getattr(self.session, "_glm_auth", None)
        if auth_header:
            preview = auth_header[:40] + ("..." if len(auth_header) > 40 else "")
            print(f"[Rush] 🔑 Authorization: {preview} (length={len(auth_header)})", flush=True)
        else:
            print(f"[Rush] 🔑 Authorization: <未配置> ⚠️", flush=True)
        print(f"[Rush] 🍪 Cookie 长度: {len(self.cookie_str)} 字符", flush=True)
        if self.detected_plan and self.detected_plan != self.config.plan:
            print(f"[Rush] ⚠️  警告: 捕获的套餐 ({PLANS[self.detected_plan]['name']}) "
                  f"与目标 ({plan['name']}) 不一致! —— 实际以用户指定 {plan['name']} 为准",
                  flush=True)
        print(f"[Rush] 并发引擎: 极速模式 {self.config.burst_concurrency}路 × "
              f"{self.config.burst_duration}秒", flush=True)

        # 启动极速并发 workers
        tasks = []
        for i in range(self.config.burst_concurrency):
            tasks.append(asyncio.create_task(self._burst_worker(i)))

        # 极速阶段持续 burst_duration 秒
        await asyncio.sleep(self.config.burst_duration)

        # 如果还没抢到，降低并发继续
        if not self.stop_event.is_set():
            print(f"[Rush] 极速阶段结束，切换到普通模式 ({self.config.normal_concurrency}路)",
                  flush=True)
            # 调整 semaphore —— 关键修复：新建一个 semaphore，
            # 不要把 self._semaphore 直接换掉（worker 已持有的旧 semaphore 引用会乱）
            self._semaphore = asyncio.Semaphore(self.config.normal_concurrency)

            # 等待直到成功或自动停止
            remaining = asyncio.create_task(self._wait_manual_stop())
            await remaining

        # 清理
        self.stop_event.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        # 输出统计
        elapsed = time.time() - self.start_time
        print(f"\n[Rush] {'='*50}")
        print(f"[Rush] 抢购结束 (耗时 {elapsed:.1f}s)")
        print(f"[Rush] 统计: preview_ok={self.stats['preview_ok']}, "
              f"check_ok={self.stats['check_ok']}, "
              f"expired={self.stats['expired']}, "
              f"errors={self.stats['errors']}")

        if self.payment_url:
            print(f"\n[Rush] ✅ 抢购成功！支付链接:")
            print(f"    {self.payment_url}")
            print(f"\n[Rush] ⚠️  请立即打开支付链接完成付款（通常15分钟内有效）")
        else:
            print(f"\n[Rush] ❌ 本次未抢到，可能已售罄或 Cookie 过期")

        await self.session.aclose()

    async def _wait_manual_stop(self):
        """等待手动停止 (Ctrl+C) 或自动停止 —— 关键修复：每 10 秒输出一次进度"""
        deadline_seconds = min(self.config.max_retries, 600)  # 最多 10 分钟
        total_retries = 0
        last_log = time.time()
        while not self.stop_event.is_set():
            await asyncio.sleep(1)
            total_retries += 1
            # 每 10 秒打一次进度，让你看到"我没卡"
            if time.time() - last_log >= 10:
                elapsed = time.time() - self.start_time
                print(
                    f"[Rush] ⏳ 仍在重试... 已用时 {elapsed:.0f}s, "
                    f"已重试 {total_retries}/{deadline_seconds} 次 | "
                    f"stats: {dict(self.stats)}",
                    flush=True,
                )
                last_log = time.time()
            if total_retries >= deadline_seconds:
                print(
                    f"\n[Rush] ⏰ 达到时间上限 ({deadline_seconds}s)，自动停止。"
                    f"通常是已售罄或 Cookie 失效。",
                    flush=True,
                )
                self.stop_event.set()
                break

    async def dry_run(self):
        """干跑测试：验证 Cookie 和端点是否有效"""
        self.session = self._build_client()
        self._semaphore = asyncio.Semaphore(1)

        plan = PLANS[self.config.plan]
        print(f"\n[DryRun] 🎯 目标套餐: {plan['name']} ({plan['price']})")
        if self.detected_plan:
            print(f"[DryRun] 捕获的套餐: {PLANS[self.detected_plan]['name']}")
        print("[DryRun] 测试 API 连通性...")
        # 测试 plan_info 端点
        plan_ep = self.endpoints.get("plan_info")
        if plan_ep:
            resp = await self._make_request(
                "plan_info",
                plan_ep.get("method", "GET"),
                plan_ep["url"],
            )
            if resp:
                print(f"  [DryRun] plan_info: HTTP {resp.status_code}")
                if resp.status_code == 200:
                    print(f"  [DryRun] ✅ 连接正常")
                    try:
                        body = resp.json()
                        print(f"  [DryRun] 响应: {json.dumps(body, ensure_ascii=False)[:200]}")
                    except Exception:
                        pass
                else:
                    print(f"  [DryRun] ⚠️  Cookie 可能已过期 (HTTP {resp.status_code})")

        # 测试 preview
        preview_ep = self.endpoints.get("preview")
        if preview_ep:
            resp = await self._make_request(
                "preview",
                preview_ep.get("method", "POST"),
                preview_ep["url"],
                preview_ep.get("post_data"),
            )
            if resp:
                print(f"  [DryRun] preview: HTTP {resp.status_code}")
                body_snippet = (resp.text or "")[:200]
                print(f"  [DryRun] 响应: {body_snippet}")
                if any(kw in body_snippet for kw in ["售罄", "sold out", "已抢光", "none"]):
                    print(f"  [DryRun] ⚠️  当前可能已售罄（正常现象，等下一波放量即可）")
            else:
                print(f"  [DryRun] preview: 请求失败")

        await self.session.aclose()


# ============================================================================
# 配置持久化
# ============================================================================

CONFIG_DIR = Path.home() / ".glm_rush"
CONFIG_FILE = CONFIG_DIR / "config.json"


def save_config(config: Dict):
    """保存捕获的配置到本地"""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    print(f"[Config] 配置已保存到 {CONFIG_FILE}")


def load_config() -> Optional[Dict]:
    """加载本地配置"""
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


# ============================================================================
# 主入口
# ============================================================================

async def mode_capture(target_plan: str = "lite", headless: bool = False):
    """仅捕获模式：打开浏览器让用户操作，保存拦截结果"""
    interceptor = APIInterceptor(headless=headless, target_plan=target_plan)
    try:
        await interceptor.start()
        await interceptor.wait_for_user()
        config = interceptor.export_config()
        save_config(config)

        plan_name = PLANS[target_plan]['name']
        print(f"\n[Capture] ✅ 捕获完成！配置已保存")
        print(f"[Capture] 目标套餐: {plan_name}")
        print(f"[Capture] 下次可直接使用: python glm_coding_rush.py --mode rush --plan {target_plan}")
    finally:
        await interceptor.close()


async def mode_rush(
    cookie_str: str,
    rush_config: RushConfig,
    endpoints: Optional[Dict] = None,
    dry_run: bool = False,
    detected_plan: Optional[str] = None,
    plan_metadata: Optional[Dict] = None,
    plan_post_bodies: Optional[Dict[str, str]] = None,
    authorization: Optional[str] = None,
):
    """抢购模式"""
    engine = RushEngine(
        cookie_str=cookie_str,
        config=rush_config,
        endpoints=endpoints,
        detected_plan=detected_plan,
        plan_metadata=plan_metadata,
        plan_post_bodies=plan_post_bodies,
        authorization=authorization,
    )

    if dry_run:
        await engine.dry_run()
        return

    # 时间同步
    time_sync = TimeSync()
    await time_sync.sync()

    # 计算开火时间
    ms_until = time_sync.ms_until_target()
    if ms_until < 0:
        print(f"[Rush] ⚠️  已经过了今天的放量时间，瞄准明天 10:00")
        ms_until = time_sync.ms_until_target()

    fire_ts = time.time() + ms_until / 1000 - rush_config.fire_ahead_ms / 1000
    fire_dt = datetime.fromtimestamp(fire_ts, tz=CST)
    print(f"[Rush] 🎯 预计开火时间: {fire_dt.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} CST")
    print(f"[Rush] 等待 {ms_until/1000:.1f} 秒...")

    await engine.run(fire_at_timestamp=fire_ts)


async def mode_full(rush_config: RushConfig):
    """完整模式：先捕获再自动进入抢购"""
    # 先尝试加载已有配置
    saved = load_config()
    if saved and saved.get("cookie_header"):
        cookie_age = time.time() - CONFIG_FILE.stat().st_mtime
        if cookie_age < 3600:  # 1小时内有效
            print(f"[Full] 使用已有配置 ({(cookie_age/60):.0f}分钟前)")
            detected = saved.get("detected_plan")
            if detected and detected != rush_config.plan:
                print(f"[Full] ⚠️  已保存的套餐 ({PLANS.get(detected, {}).get('name', detected)}) "
                      f"与目标 ({PLANS[rush_config.plan]['name']}) 不一致")
                print(f"[Full] 继续使用已保存的套餐参数，如需更换请重新捕获")
            await mode_rush(
                cookie_str=saved["cookie_header"],
                rush_config=rush_config,
                endpoints=saved.get("endpoints", {}),
                detected_plan=detected,
                plan_metadata=saved.get("plan_metadata", {}),
                plan_post_bodies=saved.get("plan_post_bodies", {}),
                authorization=saved.get("authorization"),
            )
            return

    # 重新捕获
    print("[Full] 开始捕获阶段...")
    interceptor = APIInterceptor(headless=False, target_plan=rush_config.plan)
    try:
        await interceptor.start()
        await interceptor.wait_for_user()
        saved = interceptor.export_config()
        save_config(saved)
    finally:
        await interceptor.close()

    if not saved.get("cookie_header"):
        print("[Full] ❌ 未捕获到有效 Cookie")
        return

    print("\n[Full] 捕获完成，自动进入抢购等待模式...")
    await mode_rush(
        cookie_str=saved["cookie_header"],
        rush_config=rush_config,
        endpoints=saved.get("endpoints", {}),
        detected_plan=saved.get("detected_plan"),
        plan_metadata=saved.get("plan_metadata", {}),
        plan_post_bodies=saved.get("plan_post_bodies", {}),
        authorization=saved.get("authorization"),
    )


def main():
    parser = argparse.ArgumentParser(
        description="智谱 GLM Coding Plan 抢购脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 首次使用：打开浏览器捕获接口和 Cookie（默认 Lite 套餐）
  python glm_coding_rush.py --mode capture --plan lite

  # 抢夺 Pro 套餐
  python glm_coding_rush.py --mode capture --plan pro

  # 干跑测试（验证 Cookie 和接口是否正常）
  python glm_coding_rush.py --mode rush --plan lite --dry-run

  # 使用已保存的配置直接抢购
  python glm_coding_rush.py --mode rush --plan lite

  # 完整模式（捕获 → 自动等待 → 抢购）
  python glm_coding_rush.py --mode full --plan lite

  # 自定义并发参数
  python glm_coding_rush.py --mode rush --plan lite --burst 15 --concurrency 8

套餐说明:
  lite  ¥49/月  - 基础套餐，适合个人开发者 / 小型项目
  pro   ¥149/月 - 专业套餐，5倍额度 / GLM-5 优先体验
  max   ¥469/月 - 旗舰套餐，20倍额度 / 最高并发优先级
        """
    )

    parser.add_argument(
        "--mode", type=str, default="full",
        choices=["capture", "rush", "full"],
        help="运行模式: capture(仅捕获) / rush(仅抢购) / full(捕获+抢购)"
    )
    parser.add_argument(
        "--plan", type=str, default="lite",
        choices=["lite", "pro", "max"],
        help="目标套餐: lite(¥49/月) / pro(¥149/月) / max(¥469/月)，默认 lite"
    )
    parser.add_argument(
        "--cookie", type=str, default="",
        help="Cookie 字符串（rush 模式使用，也可从配置文件中加载）"
    )
    parser.add_argument(
        "--burst", type=int, default=10,
        help="极速阶段并发数 (默认 10)"
    )
    parser.add_argument(
        "--concurrency", type=int, default=5,
        help="普通阶段并发数 (默认 5)"
    )
    parser.add_argument(
        "--burst-duration", type=float, default=5.0,
        help="极速阶段持续秒数 (默认 5)"
    )
    parser.add_argument(
        "--max-retries", type=int, default=2000,
        help="最大重试次数 (默认 2000)"
    )
    parser.add_argument(
        "--fire-ahead", type=int, default=50,
        help="提前开火毫秒数 (默认 50ms，补偿网络延迟)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="干跑测试：验证接口和 Cookie，不实际抢购"
    )
    parser.add_argument(
        "--headless", action="store_true",
        help="浏览器无头模式（capture/full 模式使用）"
    )
    parser.add_argument(
        "--proxy", type=str, default="",
        help="HTTP 代理地址，如 http://127.0.0.1:7890"
    )

    args = parser.parse_args()

    rush_config = RushConfig(
        plan=args.plan,
        burst_concurrency=args.burst,
        normal_concurrency=args.concurrency,
        burst_duration=args.burst_duration,
        max_retries=args.max_retries,
        fire_ahead_ms=args.fire_ahead,
    )

    plan_info = PLANS[args.plan]
    print(f"🎯 目标套餐: {plan_info['name']} ({plan_info['price']}) — {plan_info['desc']}")

    if args.mode == "capture":
        asyncio.run(mode_capture(target_plan=args.plan, headless=args.headless))
        return

    if args.mode == "rush":
        # 优先使用命令行 cookie，其次从配置文件加载
        cookie_str = args.cookie
        endpoints = None
        detected_plan = None
        plan_metadata = None
        plan_post_bodies = None
        authorization = None
        if not cookie_str:
            config = load_config()
            if config:
                cookie_str = config.get("cookie_header", "")
                endpoints = config.get("endpoints", {})
                detected_plan = config.get("detected_plan")
                plan_metadata = config.get("plan_metadata", {})
                plan_post_bodies = config.get("plan_post_bodies", {})
                authorization = config.get("authorization")
                if cookie_str:
                    age = time.time() - CONFIG_FILE.stat().st_mtime
                    print(f"[Rush] 从配置文件加载 Cookie ({(age/60):.0f}分钟前)")
                    if detected_plan:
                        print(f"[Rush] 已保存的套餐: {PLANS.get(detected_plan, {}).get('name', detected_plan)}")
                    if authorization:
                        preview = authorization[:20] + "..." if len(authorization) > 20 else authorization
                        print(f"[Rush] ✅ 已加载 Authorization token ({preview})", flush=True)
                    else:
                        print(f"[Rush] ⚠️ 配置文件中没有 Authorization token —— "
                              f"请重新跑 --mode capture", flush=True)

        if not cookie_str:
            print("[ERROR] 请提供 Cookie: --cookie 'xxx' 或先运行 --mode capture")
            sys.exit(1)

        asyncio.run(mode_rush(
            cookie_str=cookie_str,
            rush_config=rush_config,
            endpoints=endpoints,
            dry_run=args.dry_run,
            detected_plan=detected_plan,
            plan_metadata=plan_metadata,
            plan_post_bodies=plan_post_bodies,
            authorization=authorization,
        ))
        return

    if args.mode == "full":
        asyncio.run(mode_full(rush_config=rush_config))
        return


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n[Exit] 用户中断，已停止所有任务")
    except Exception as e:
        print(f"\n[Error] {e}")
        import traceback
        traceback.print_exc()
