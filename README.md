# fastapi-versioned-gateway

在 FastAPI 服务上提供统一的入口网关能力：**版本协商与生命周期管理**、**按调用方限流**、**写请求幂等保护**。三者以中间件形式作用于所有入口（`/docs`、`/openapi.json` 等文档路径除外），行为一致、拒绝原因可区分。

## 版本策略

调用方通过请求头 `X-API-Version` 声明版本：

| 情况 | 行为 | 响应 |
| --- | --- | --- |
| 未声明（或空值） | 按稳定默认版本 `v2` 处理 | 正常响应，回显 `X-API-Version: v2` |
| 稳定版本（`v2`） | 正常服务 | 正常响应 |
| 已弃用版本（`v1`） | 仍可使用，但明确标记 | 正常响应 + `Deprecation: true` 与 `Sunset: <日期>` 头 |
| 已下线版本（`v0`） | 拒绝 | `410` + `{"error": {"code": "version_sunset", ...}}` |
| 不存在的版本 | 拒绝 | `400` + `{"error": {"code": "unknown_version", ...}}` |

已下线与不存在的版本**不会**悄悄回落到默认版本，两者以不同的状态码（410 vs 400）和错误码（`version_sunset` vs `unknown_version`）区分。版本生命周期表见 `app/versioning.py: default_registry()`。

## 限流口径

- 维度：按调用方，取自 `X-Caller-Id` 请求头，缺省为 `anonymous`。不同调用方互不影响。
- 双重限制（`app/ratelimit.py: RateLimitConfig`）：
  - **速率**：固定窗口，默认每 60 秒 10 个请求。超限返回 `429` + 错误码 `rate_limited`，并带 `Retry-After` 头。
  - **并发**：同时在途请求数，默认 5。超限返回 `429` + 错误码 `concurrency_limited`。
- 计数正确性：检查与计数在同一把锁内原子完成，突发并发下不会少算或多算；窗口滚动或请求结束释放并发槽后即可重新放行。

## 幂等语义

- 适用范围：携带 `Idempotency-Key` 请求头的写请求（POST/PUT/PATCH/DELETE）。
- 键空间：`(调用方, Idempotency-Key)`，不同调用方互不影响。
- 请求指纹：HTTP 方法 + 路径 + 请求体的 SHA-256。
- 判定：
  - **首次提交**：执行处理器，响应被记录，响应头 `Idempotency-Replayed: false`。
  - **同键同内容重复提交**：直接返回首次的响应（状态码与响应体一致），响应头 `Idempotency-Replayed: true`，不产生重复副作用。
  - **同键不同内容**：拒绝，`409` + 错误码 `idempotency_conflict`，消息中指明冲突的键。
  - **并发同键提交**：只执行一次；其余请求等待首个完成后回放其响应。

## 本地验证

```bash
# 安装依赖
uv sync

# 运行全部测试（版本拒绝与弃用、并发限流、幂等重复与键冲突）
uv run pytest -q

# 查看测试日志中打印的关键输入与判定依据
uv run pytest -q -s --log-cli-level=INFO

# 启动服务手动验证
uv run python main.py   # 监听 127.0.0.1，端口随机（port=0）
```

手动验证示例：

```bash
# 版本：弃用标记 / 下线拒绝 / 未知拒绝
curl -i -H 'X-API-Version: v1' http://127.0.0.1:8000/items
curl -i -H 'X-API-Version: v0' http://127.0.0.1:8000/items
curl -i -H 'X-API-Version: v99' http://127.0.0.1:8000/items

# 幂等：同一键重复提交返回一致结果；换内容则 409
curl -i -X POST -H 'Content-Type: application/json' -H 'X-Caller-Id: u1' \
     -H 'Idempotency-Key: k1' -d '{"name": "a"}' http://127.0.0.1:8000/items
```

## 代码结构

```
app/
  versioning.py   # 版本注册表与协商（默认版本 / 弃用标记 / 下线与未知拒绝）
  ratelimit.py    # 按调用方的固定窗口速率 + 并发上限，锁内原子计数
  idempotency.py  # (caller, key) 幂等存储：执行 / 回放 / 冲突三态
  gateway.py      # FastAPI 中间件与路由，把三者统一挂到所有入口
tests/            # 版本、限流、幂等的单元与并发测试
```
