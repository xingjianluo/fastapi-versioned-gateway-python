# fastapi-versioned-gateway-python

在 FastAPI 服务之上实现的一层面向外部调用方的网关能力：**版本协商与生命周期
管理、按调用方限流（速率 + 并发）、写请求幂等保护**，在各入口下表现一致、
拒绝结论可区分、可解释。

## 快速开始

```bash
uv sync
uv run python main.py          # 启动服务 http://127.0.0.1:8000
uv run pytest -q               # 运行全部单测（22 个）
uv run pytest -s               # 打印关键用例的输入与判定依据
```

## 入口

| 方法/路径 | 说明 |
| --- | --- |
| `GET  /gateway/ping` | 只读入口，经过版本协商与限流 |
| `POST /gateway/orders` | 写入口，产生真实副作用，支持幂等 |
| `POST /gateway/payments` | 另一个写入口，行为与 orders 一致 |

## 请求约定

- `X-Caller-Id`（必填）：调用方标识，限流与幂等均按它隔离；
- `X-API-Version`（选填）：日期版本，如 `2025-01-01`；缺省走稳定默认版本；
- `Idempotency-Key`（写请求选填）：填写后启用幂等保护。

## 能力概览

- **版本**：`2025-01-01` stable（默认）/ `2024-01-01` deprecated（可用且响应
  明确标记）/ `2023-01-01` retired（`410 version_retired`）/ 未知版本
  （`400 version_unknown`）。除“未声明版本”外绝不悄悄回落默认版本。
- **限流**：默认每调用方 5 请求/秒（滑动窗口）+ 2 并发；调用方互不影响；
  check-and-reserve 原子计数，突发并发下不多不少；窗口滑过/请求结束后恢复。
- **幂等**：相同键+相同内容返回与首次完全一致的结果（并发也只产生一次副作用）；
  相同键被不同内容复用以 `409 idempotency_conflict` 拒绝并给出双方指纹。

详细策略、错误码表、手工与自动化验证方法见 [`docs/gateway.md`](docs/gateway.md)。

## 代码结构

```
app/gateway/
  errors.py       # 错误码 + 统一错误信封 + 异常处理器
  versions.py     # 版本生命周期表与协商逻辑
  ratelimit.py    # 按调用方的滑动窗口速率 + 并发槽（线程安全、原子预留）
  idempotency.py  # 幂等占位/回放/冲突/并发等待（anyio 原语）
  middleware.py   # 纯 ASGI 中间件：串联完整决策链
  routes.py       # 示例入口（副作用可审计，便于测试“恰好一次”）
app/main.py       # 应用装配
tests/            # 版本 / 限流 / 幂等 / 跨切面契约测试
```
