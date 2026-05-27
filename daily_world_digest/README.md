# Daily World Digest

每日抓取世界 trending 事件，按主题聚类，保留来源链接，并用最近历史记录补充“前因后果”的连续性背景。

## 为什么建议这个抓取时间

如果你在爱尔兰/英国时区阅读，建议每天 **07:30 Europe/Dublin** 运行：

- 亚洲和欧洲的当天早间新闻已经更新。
- 美国前一日晚间的重大事件已经进入主要媒体 feed。
- 你早上阅读时不会太滞后，也不会被半夜碎片新闻打断。

如果你更关注美国新闻，可以改为 **12:30 Europe/Dublin**，这时美国东海岸早间报道开始更新。更理想的节奏是早上 07:30 一封主简报，傍晚 18:30 一封短更新，但第一版建议先从每天一封开始。

## 安装与配置

复制配置文件：

```powershell
Copy-Item config.example.json config.json
```

编辑 `config.json` 里的发件人、收件人和来源。SMTP 凭据不要写进 `config.json`，复制 `.env.example` 为 `.env`：

```powershell
Copy-Item .env.example .env
```

然后编辑 `.env`：

```dotenv
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your-address@example.com
SMTP_PASSWORD=your-app-password
```

Gmail、Outlook 等邮箱通常需要“应用专用密码”，不能直接使用网页登录密码。
`.env` 已经被 `.gitignore` 忽略，不要把它发给别人或提交到版本库。

## 运行

如果 `python` 不在 PATH 里，可以把下面命令里的 `python` 换成完整路径，例如当前 Codex 运行时：

```powershell
& "C:\Users\126105287\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" daily_digest.py --config config.json
```

先生成本地预览，不发邮件：

```powershell
python daily_digest.py --config config.json
```

确认 `data/latest_digest.html` 和 `data/latest_digest.txt` 没问题后发邮件：

```powershell
python daily_digest.py --config config.json --send
```

如果你的 `.env` 放在别的位置，可以显式指定：

```powershell
python daily_digest.py --config config.json --env C:\path\to\.env --send
```

## Windows 每日定时任务

在 PowerShell 中把路径替换为你的实际路径：

```powershell
$script = "C:\Users\126105287\OneDrive - University College Cork\Documents\research reading\daily_world_digest\daily_digest.py"
$workdir = "C:\Users\126105287\OneDrive - University College Cork\Documents\research reading\daily_world_digest"
$python = "C:\Users\126105287\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$action = New-ScheduledTaskAction -Execute $python -Argument "$script --config config.json --send" -WorkingDirectory $workdir
$trigger = New-ScheduledTaskTrigger -Daily -At 7:30am
Register-ScheduledTask -TaskName "DailyWorldDigest" -Action $action -Trigger $trigger -Description "Send a daily sourced world events digest"
```

## 事件连续性如何实现

每次运行后，工具会把事件标题、关键词和来源写入 `data/history.jsonl`。下一次生成简报时，它会把当天事件和最近 21 天的历史事件做关键词匹配，找到相关前情，形成“此前相关进展包括...”这类背景说明。

这不是完整的事实核查系统，也不会替代人工判断；它的目标是给你建立连续性，避免每天只看到碎片标题。

## 后续可增强

- 接入 OpenAI API，把每个事件改写成更自然的中文摘要和时间线。
- 加入 GDELT、Mediastack、NewsAPI 等新闻 API，提升趋势判断质量。
- 为不同区域生成单独栏目，例如中国、美国、欧洲、中东、科技、经济。
- 加入“连续追踪事件”名单，例如俄乌战争、加沙、人权、AI 政策、气候灾害。
