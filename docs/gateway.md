# 网关能力说明（Versioning · Rate Limit · Idempotency）

所有外部入口统一挂在 `/gateway` 前缀下，由同一个纯 ASGI 中间件
（`app/gateway/middleware.py`）完成决策，因此任意入口对版本、限流、
幂等的处理方式与拒绝结论完全一致、且可解释。

决策链顺序：

```
调用方身份 -> 版本协商 -> 幂等判定 -> 原子限流/并发预留 -> 业务执行 -> 幂等结果落库/回放
```

## 1. 约定的请求头

| 请求头 | 含义 | 是否必填 |
| --- | --- | --- |
| `X-Caller-Id` | 调用方标识，限流与幂等键均按该维度隔离 | 必填，缺失返回 `401 caller_missing` |
| `X-API-Version` | 声明要使用的 API 版本（日期版本，如 `2025-01-01`） | 选填，缺省走稳定默认版本 |
| `Idempotency-Key` | 写请求（POST/PUT/PATCH/DELETE）的幂等标识 | 选填；填写后启用幂等保护 |

响应头：

| 响应头 | 含义 |
| --- | --- |
| `X-Gateway-Api-Version` | 本次请求实际生效的版本 |
| `X-Gateway-Version-Source: default` | 仅在调用方未声明、使用默认版本时出现 |
| `X-Gateway-Version-Deprecation` | 仅在命中已弃用版本时出现（请求仍被处理） |
| `X-Idempotency-Key` / `X-Idempotency-Replayed` | 幂等键；`true` 表示本次是首次结果的回放，未执行业务 |

## 2. 版本协商与生命周期策略

版本表（`app/gateway/versions.py`）：

| 版本 | 状态 | 行为 |
| --- | --- | --- |
| `2025-01-01` | stable（默认） | 正常服务；未声明版本时使用它 |
| `2024-01-01` | deprecated | **仍可使用**，响应带 `X-Gateway-Version-Deprecation` 明确标记 |
| `2023-01-01` | retired | 已下线，返回 `410 version_retired` |
| 其他任意值 / 未知版本 | — | 返回 `400 version_unknown` |

关键原则：**除“未声明版本”外，绝不悄悄回落到默认版本**。已下线与不存在
是两种可区分的拒绝原因（HTTP 410 vs 400，错误码不同），消息体会给出
请求版本、已知版本列表与建议默认版本，便于调用方定位。

## 3. 按调用方维度的限流

默认口径（`app/main.py` 中 `RateLimitConfig(rate_per_second=5,
max_concurrent=2)`）：

- **速率**：同一 `X-Caller-Id`，1 秒滑动窗口内最多 5 个请求；
- **并发**：同一调用方同时在途最多 2 个请求；
- **隔离**：每个调用方独立计数桶，互不影响；
- **恢复**：窗口滑过后自动恢复放行；在途请求结束即归还并发槽。

实现要点（`app/gateway/ratelimit.py`）：窗口淘汰、阈值判断、名额预留
在同一把锁的临界区内一次完成（check-and-reserve 原子），因此突发并发
下既不会少算也不会多算（测试用 200 个真实线程同时打点，恰好放行 5 个）。

超限拒绝（均为 HTTP 429，错误码可区分，且 `details` 给出观察值/重试建议）：

- `rate_limited`：速率超限，`details` 含 `limit`、`observed`、
  `retry_after_seconds`；
- `concurrency_limited`：并发超限，`details` 含 `max_concurrent`、
  `observed`。

说明：幂等“回放”在限流预留之前短路返回，不占用限流名额、不产生副作用；
若请求在通过幂等占位后、执行前被限流拒绝，占位会被丢弃，调用方随后用
同一键重试仍可成为首次真实执行。

## 4. 写请求幂等保护

范围：带 `Idempotency-Key` 的写请求。幂等键按 `(调用方, 键)` 维度隔离。
指纹为请求体原始字节的 SHA-256（`app/gateway/idempotency.py`）。

语义：

1. **首次提交**：执行业务、产生一次副作用，并把最终状态码与响应体落库。
2. **相同键 + 相同内容重复提交**：返回与首次**完全一致**的状态码和响应体
   （`X-Idempotency-Replayed: true`），不再执行业务，不产生重复副作用。
3. **相同键 + 不同内容**：返回 `409 idempotency_conflict`，消息体
   `details` 同时给出 `original_fingerprint` 与 `conflicting_fingerprint`
   说明冲突依据，且不产生任何副作用。
4. **相同键的并发提交**：后来者等待首个请求完成，然后统一回放首个结论；
   无论并发多少个相同请求，副作用恰好一次（测试：10 个线程同键并发，
   副作用恰为 1，且 10 个响应体完全相同）。
5. **首次执行失败（未产出响应，如崩溃）**：占位被丢弃，调用方可用同一键
   重试；一旦已产生最终响应，则以该响应为准。

幂等保护对 `/gateway/orders` 与 `/gateway/payments` 等所有写入口一致生效。

## 5. 统一错误信封

所有拒绝均返回：

```json
{
  "error": {
    "code": "machine_readable_code",
    "message": "人类可读原因与判定依据",
    "details": { }
  }
}
```

| code | HTTP | 触发条件 |
| --- | --- | --- |
| `caller_missing` | 401 | 缺少 `X-Caller-Id` |
| `version_unknown` | 400 | 声明了不存在的版本，不回落 |
| `version_retired` | 410 | 声明了已下线版本 |
| `rate_limited` | 429 | 调用方速率超窗口上限 |
| `concurrency_limited` | 429 | 调用方并发超上限 |
| `idempotency_conflict` | 409 | 幂等键被不同请求内容复用 |

## 6. 本地验证

```bash
# 安装依赖（uv）
uv sync

# 运行服务
uv run python main.py            # http://127.0.0.1:8000

# 运行全部单测（22 个），-s 可打印每个关键用例的输入与判定依据
uv run pytest -q
uv run pytest -s                 # 查看 [CASE] 输入/状态码/响应头/响应体日志
```

手工验证示例：

```bash
# 默认版本（无需声明）
curl -s -D- http://127.0.0.1:8000/gateway/ping -H 'X-Caller-Id: alice'

# 已弃用版本：200，但带 deprecation 响应头
curl -s -D- http://127.0.0.1:8000/gateway/ping \
  -H 'X-Caller-Id: alice' -H 'X-API-Version: 2024-01-01'

# 已下线版本：410 version_retired；不存在版本：400 version_unknown
curl -s -D- http://127.0.0.1:8000/gateway/ping \
  -H 'X-Caller-Id: alice' -H 'X-API-Version: 2023-01-01'

# 幂等：连续两次相同写请求，第二次为回放，副作用只有一次
for i in 1 2; do
  curl -s -D- -X POST http://127.0.0.1:8000/gateway/orders \
    -H 'X-Caller-Id: alice' -H 'Idempotency-Key: k-1' \
    -H 'Content-Type: application/json' -d '{"item":"book"}'
done
```

测试文件对照需求：

- `tests/test_versioning.py`：默认版本、弃用标记、下线/未知的可区分拒绝、不回落；
- `tests/test_ratelimit.py`：窗口速率、调用方隔离、窗口恢复、200 线程突发
  计数、并发槽超限与速率超限错误码可区分；
- `tests/test_idempotency.py`：重复回放一致、键复用冲突、按调用方隔离、
  并发同键单次副作用、跨入口一致、并发混合（回放+冲突）；
- `tests/test_gateway_contract.py`：缺调用方、拒绝信封一致、回放不占限流额度。
