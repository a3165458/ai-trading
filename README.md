# JEV × Lighter

`this-that-model` / Jev 决策 → Lighter.xyz BTC、ETH 永续。Web UI 把每一笔下单标成 **BUY** 或 **SELL**。

默认 **纸上交易**：真实 Lighter 盘口，不签名、不打真实单。

## 为什么不 fork 现成仓库

看过这些项目，没有一个同时满足「this-that OpenAI 协议 + Lighter BTC/ETH 下单 + 买卖动作 UI」：

| 仓库 | 能用的部分 | 缺口 |
| --- | --- | --- |
| [web3w/jev-trader](https://github.com/web3w/jev-trader) | 决策环、SSE、买卖展示 | Kuru / Hyperliquid，不是 Lighter |
| [EthanAlgoX/jev-trading](https://github.com/EthanAlgoX/jev-trading) | buy/sell/hold 工作台 | 股票信号，不下单 |
| [FLock-io/this-that-model](https://github.com/FLock-io/this-that-model) | OpenAI 兼容 `/v1/chat/completions`（必须带 enum） | 不是交易系统 |
| [elliottech/lighter-python](https://github.com/elliottech/lighter-python) | 官方 SDK / `create_order` | 无模型、无 UI |
| [dex-original/ai-trading-agent](https://github.com/dex-original/ai-trading-agent) | Lighter + LLM | OpenRouter 散文模型，不是 typed decision |

本仓库按 jev-trader 的环（快照 → 决策 → 下单 → 流水）重写，执行层走 Lighter 官方接口。

`flock-io/this-that-model-1.0` 是 typed-decision 模型，不是行情预测模型。置信度门限在代码里，不在模型里。

## 跑起来

```bash
cd /root/ai-trading
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m app
```

打开 http://127.0.0.1:3000（本机 3000 被占用时改 `PORT`）。进程启动后自动连续决策，界面只做展示。

`MODEL=mock` 或未配决策后端时用内置 mock（动量 + 盘口不平衡）。

## 接 this-that / Jev

`MODEL` 可选 `auto` / `thisthat` / `jev` / `mock`。`auto`：有 `JEV_API_KEY` 走官方 Jev，有 `OPENAI_BASE_URL` 走本地 this-that，否则 mock。

### 本地 this-that-model（当前默认）

```bash
python -m thisthat.server --port 8000
```

`.env`：

```
MODEL=thisthat
OPENAI_BASE_URL=http://127.0.0.1:8000/v1
OPENAI_API_KEY=not-needed
OPENAI_MODEL=flock-io/this-that-model-1.0
```

客户端请求形状（this-that **拒绝没有 enum 的请求**）：

- 倒数第二条 user message = 市场状态
- 最后一条 user message = 问题
- `response_format.json_schema.schema.properties.answer.enum = ["buy","sell"]`
- `logprobs=true` 读分布；若响应带 `this_that` 字段也会用

### 官方 Jev（TypeSafe）

需要 `JEV_API_KEY`（也认 `TYPESAFE_API_KEY` / `TYPESAFE_AI_API_KEY`）：

```
MODEL=jev
JEV_API_URL=https://api.typesafe.ai/v1/systemone
JEV_API_KEY=jv_live_...
JEV_MODEL=jev-1.13.0
```

走 `POST /v1/systemone`，`choice` 问题的 options 是 buy / sell。

## Lighter 实盘

进程启动即连续下单。需要：

1. `pip install lighter-sdk`
2. 在 [app.lighter.xyz](https://app.lighter.xyz) 创建 API key（index 2–254，0/1 留给官方 UI）
3. `.env`：

```
TRADING_MODE=live
LIGHTER_BASE_URL=https://mainnet.zklighter.elliot.ai
LIGHTER_API_PRIVATE_KEY=...
LIGHTER_ACCOUNT_INDEX=...
LIGHTER_API_KEY_INDEX=2
LOOP_SECONDS=0
COOLDOWN_SECONDS=0
```

测试网把 URL 换成 `https://testnet.zklighter.elliot.ai`。

市价单：`ORDER_TYPE_MARKET` + IOC。ETH `market_id=0`，BTC `market_id=1`（启动时再向 `/api/v1/orderBooks` 确认）。`price` 是可接受最差价 = mid × (1±`SLIPPAGE`)。

## 风控（代码，不是模型）

- `MIN_CONFIDENCE` 以下 → 跳过本轮，不下单
- `MAX_POSITION_USD` 限制同向加仓（`0` 或不设 = 不限）
- `COOLDOWN_SECONDS` 每市场冷却（`0` = 不冷却）
- `LOOP_SECONDS` 两轮之间的最短间隔（`0` = 连续跑，只让出 50ms）
- `TRADE_NOTIONAL_USD` 单笔名义（且满足交易所 `min_base` / `min_quote`）

这不是投资建议。实盘会亏钱。
