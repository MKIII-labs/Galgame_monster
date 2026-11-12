# 界面重构基线（2026-11-13）

> 采集工具：`scripts/ui_probe.mjs`（Edge headless + CDP，每个模式独立加载后测量）
> 采集地址：`http://127.0.0.1:8791/`
> 产物：`data/ui-baseline/<时间戳>-desktop/` 与 `-mobile/`（截图 + `metrics.json`）

## 1. 资源与 DOM

| 指标 | 值 |
|---|---|
| 首屏引用 JS/CSS | 89 个（65 script + 24 CSS） |
| 首屏原始体积 | 3,685.6 KB |
| 首屏 gzip | 1,042.3 KB |
| `web/index.html` | 2,991 行 |
| DOM 基线（Solo） | 28,129 节点；850 id / 353 button / 143 input / 25 select / 16 textarea |
| 控制台错误 | 0 |

## 2. 桌面 1440×900

| 模式 | FCP | LCP | CLS | TBT | DOM 节点 | 横向溢出 |
|---|---|---|---|---|---|---|
| Solo | 128 ms | 128 ms | 0.0008 | 234 ms | 28,129 | 0 |
| IDE | 132 ms | 696 ms | 0.5502 | 493 ms | 28,449 | 0 |
| 大厅 | 168 ms | 736 ms | 0.0007 | 192 ms | 28,190 | 0 |
| 工作台 | 128 ms | 128 ms | 0.0007 | 257 ms | 28,216 | 0 |
| 设置 | 124 ms | 3,924 ms | 0.0008 | 289 ms | 30,119 | 0 |

说明：
- IDE 的 CLS 0.55 来自 Monaco 初始化后的布局位移；这是当前已知问题，重构目标是不高于基线。
- 设置的 LCP 3,924 ms 是“打开设置弹窗”这一动作被记为新的最大内容绘制；不代表首屏。
- `load` 在部分模式下为 0 / 较短，因为页面本身是静态壳 + 异步脚本，按 DCL 与 FCP 看更稳定。

## 3. 手机 390×844

| 模式 | FCP | LCP | CLS | TBT | DOM 节点 | 横向溢出 |
|---|---|---|---|---|---|---|
| Solo | 136 ms | 640 ms | 0 | 278 ms | 28,139 | 0 |
| IDE | 164 ms | 164 ms | 0 | 698 ms | 28,449 | 0 |
| 大厅 | 116 ms | 116 ms | 0 | 207 ms | 28,200 | 0 |
| 工作台 | 140 ms | 140 ms | 0 | 264 ms | 28,216 | 0 |
| 设置 | 132 ms | 3,900 ms | 0 | 287 ms | 30,119 | 0 |

## 4. 验收口径

- 首屏 JS/CSS 数量 ≤ 89，gzip 体积 ≤ 1,042.3 KB。
- FCP / LCP / TBT / CLS 相对本基线不退化超过 5%。
- 五档视口无意外横向溢出（IDE 的横向滚动为设计内行为）。
- 控制台 0 未捕获错误。

## 5. 复采命令

```powershell
# 先启动服务
.venv\Scripts\python.exe -c "from app.httpd import create_server; s,p=create_server('127.0.0.1',8791); print('READY',p); s.serve_forever()"

# 桌面 / 手机
node scripts/ui_probe.mjs --url http://127.0.0.1:8791/ --out data/ui-baseline/after-desktop
node scripts/ui_probe.mjs --url http://127.0.0.1:8791/ --out data/ui-baseline/after-mobile --mobile
```
