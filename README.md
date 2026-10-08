# News Notif Bot

个人新闻监控推送（自用）：公司新闻（官网一手源 + Google News 聚合 + LLM 语义过滤）→ ntfy 手机通知。

- 云端 GitHub Actions 每 5 分钟运行（`.github/workflows/check.yml`），状态回写 `state_cloud.json`
- 数据源：官网官方新闻（直解 HTML/RSS）+ Google News 关键词（规则过滤 + LLM 复核 + 同事件去重）
- 推送：ntfy（topic 通过 Secret 注入，仓库不含任何凭证）

## 需要的 Secrets（Settings → Secrets and variables → Actions）

| 名称 | 说明 |
|---|---|
| `NTFY_TOPIC` | ntfy 主题名（推送目标） |
| `LLM_API_KEY` | OpenAI 兼容接口的 key（可选；没有则只用规则过滤） |

## 使用

Fork/复制本仓库 → 配好上述两个 Secret → Actions 页启用 `check` 工作流并手动跑一次（首次为基线，不推送）→ 之后全自动。

配置见 `config.example.json`（监控关键词、官方源、过滤词表、间隔均可调）。
