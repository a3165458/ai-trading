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
chmod 600 .env
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

## buy / sell / hold 是怎么真正落地的

方向不由 this-that 投票。`app/policy.py` 用行情打分，模型只在强烈反对时否决：

1. **方向分**（`DIR_MIN`，默认 8）：`ret_5m_bps/8 + ret_1h_bps/20 + 盘口 + 主动成交`。盘口和主动成交各自封顶 ±6，单靠其中一项过不了门槛。另外 5 分钟和 1 小时收益必须和方向一致（大约 12 bps 或 30 bps），避免盘口来回抖就来回开平。成交回报若还没写进持仓，同一标的 8 秒内不再发第二笔。
2. **5 分钟 K 线**：交易所的 candle 流只推当前这根柱。历史从 `/api/v1/candles` 补一次，同一根柱的更新按时间戳覆盖，不再把同一根收盘价重复追加。
3. **模型只做否决**：原始概率里，相反方向 ≥ `MODEL_VETO`（默认 0.55）才拦住。模型几乎全给 hold 时不再挡住开仓。模型请求失败则不开新仓，但分数反向和止损仍会平仓。
4. **仓位**：同向信号加仓（`ALLOW_ADD=true`），每次一笔 `TRADE_NOTIONAL_USD`。开仓后最多再加 `MAX_ADDS` 笔，笔数按交易所持仓成本推算，重启不会重置；同一标的两次下单间隔至少 `ADD_COOLDOWN_SECONDS`；持仓浮盈低于 `ADD_MIN_PNL_BPS` 不加（默认 0，即不给亏损仓位补仓）。反向信号先 reduce-only 全部平掉，下一轮再看要不要开对面。`STOP_LOSS_BPS` / `TAKE_PROFIT_BPS` / `MAX_HOLD_SECONDS` 仍先于分数执行。

界面决策流的「原因」列会写出 开仓 / 持有多仓 / 平多 / 模型偏观望 / 多空差不足 等，可以直接看到每一轮为什么没动。

## 风控（代码，不是模型）

- `MIN_CONFIDENCE` 以下 → 跳过本轮，不下单
- `MAX_POSITION_USD` 单标的名义上限，叠加在 `MAX_ADDS` 之上（`0` 或不设 = 只按笔数限制）
- `COOLDOWN_SECONDS` 每市场冷却（`0` = 不冷却）
- `LOOP_SECONDS` 两轮之间的最短间隔（`0` = 连续跑，只让出 50ms）
- `TRADE_NOTIONAL_USD` 单笔名义（且满足交易所 `min_base` / `min_quote`）

## 隐私与公开范围

- 只提交源码、测试及不含真实凭据的 `.env.example`。`.env` 及其变体、`data/`（含资金曲线、订单记录和备份）、日志、私钥文件和数据库不应进入 Git。
- 服务端密钥仅放在本机 `.env` 或进程环境中；`.env` 权限设为 `600`，实盘 `data/` 目录权限设为 `700`。不要在 issue、日志截图或提交信息里粘贴凭据。
- 仪表盘是**公开只读展示**：余额、持仓、收益、决策和脱敏后的订单摘要仍会公开。它不是私密账户管理页面；如这些指标也需保密，应在反向代理或隧道入口增加访问认证。
- `/api/state` 和 `/api/events` 会移除密钥、账户标识、交易哈希、模型原始提示词与响应，并将异常详情替换为通用错误。外部模型和交易所仍会接收运行策略所必需的请求。
- `/api/start`、`/api/stop`、`/api/tick` 不开放控制；静态文件只从 `web/` 提供，不能将项目根目录或 `data/` 配置为公开目录。
- 页脚显示本次交易使用的钱包地址（链接到 Lighter 浏览器）以及开源仓库地址。实盘地址由交易所接口按账户索引读取，也可用 `LIGHTER_L1_ADDRESS` 覆盖；只有 `0x` + 40 位十六进制的地址才会生成链接。链接模板 `EXPLORER_URL` 与仓库地址 `REPO_URL` 均可替换。
- 钱包地址本身是链上公开标识，页脚公开它等于公开该账户的公开持仓与成交流水；私钥、API key 和账户索引仍不会出现在接口或页面上。
- `.gitignore` 不能清除已经提交过的秘密；如发生泄漏，先撤销或轮换凭据，再处理 Git 历史。

这不是投资建议。实盘会亏钱。
