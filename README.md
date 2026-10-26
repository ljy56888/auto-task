# auto-task

每天定时跑一次 GitHub 悬赏巡检，把结果写成 Markdown 摘要 + 可选推飞书。
**跑在 GitHub Actions 的免费虚拟机上，零服务器成本。**

## 它做什么

1. 用 5 个查询搜最新悬赏单（`label:bounty` / `"bounty: $"` / `USDC bounty` 等）
2. 过滤三类噪声：其他 AI 发的「赏金告警贴」、资助申请、虚构货币
3. 抽金额并分级：标签 > 赏金名词 > 裸币种（裸币种必须有赏金平台旁证才认）
4. 去掉已指派的、金额低于 $20 的、24 小时前创建的
5. 输出 `output/digest-YYYY-MM-DD.md` + 写入 Actions 运行摘要
6. 去重状态写回 `state/seen.json`，自动提交回仓库

**只读**：不发评论、不提 PR、不投标、不登录第三方平台。

## 一次性配置（3 步）

### 1. 开启 Actions

仓库页 → **Actions** 标签 → 如果顶部有黄色提示条，点 **"I understand my workflows, go ahead and enable them"**。

### 2. 加飞书 webhook（可选）

**Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|---|---|
| `FEISHU_WEBHOOK` | 飞书群机器人 webhook 地址 |

不加也能跑，只是不推送，结果看 Actions 摘要和 artifact。

`GITHUB_TOKEN` 不用手动加，Actions 自动注入。

### 3. 确认默认分支是 `main`

定时任务**只在默认分支**生效。

## 手动跑一次验证

Actions → 左侧 `auto-task` → **Run workflow** → 绿色按钮。

跑完点进去看 **Summary**，应该有一张表格。

## 改时间

编辑 `.github/workflows/auto_task.yml` 的 cron，**必须用 UTC**：

| 想要（北京时间） | cron |
|---|---|
| 每天 09:00 | `0 1 * * *` |
| 每天 21:00 | `0 13 * * *` |
| 每 6 小时 | `0 */6 * * *` |
| 每周一 09:00 | `0 1 * * 1` |

## 改参数

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `WINDOW_HOURS` | 24 | 回看窗口（小时） |
| `MIN_AMOUNT` | 20 | 金额下限（美元） |

在 workflow 的 `env:` 里加一行即可，或用 `workflow_dispatch` 的输入框临时改。

## 六个坑

| 坑 | 说明 |
|---|---|
| 时区 | cron 只认 UTC，写北京时间会差 8 小时 |
| 分支 | 定时任务只跑默认分支的 workflow |
| Fork | fork 来的仓库 schedule 默认关闭，要手动 Enable |
| 60 天 | 仓库 60 天无提交，GitHub 自动停用定时任务（会发邮件） |
| 计费 | 公开仓库免费不限时长；私有仓库 2000 分钟/月 |
| 准点 | 定时不保证准点，高峰期可能延迟几分钟到半小时 |

## 文件结构

```
.github/workflows/auto_task.yml   定时 + 手动触发
scripts/auto_task.py              主脚本（仅标准库）
requirements.txt                  空（占位，给 pip 缓存用）
state/seen.json                   去重状态，自动提交
output/digest-YYYY-MM-DD.md       每日摘要（artifact 里）
```
