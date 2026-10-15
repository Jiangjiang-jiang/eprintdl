# eprintdl

把 IACR Cryptology ePrint Archive（<https://eprint.iacr.org>）上的论文 PDF 批量下载到本地，并用本地数据库记录元数据和下载状态。纯 Python 标准库实现，不需要安装任何第三方包，只需要系统里有 Chrome、Chromium 或 Edge 中的一种。支持 Windows、Linux、macOS 和 WSL。

下载得到的是站点上的原始 PDF 文件，文件名固定为“年份-编号_标题”，不含空格：

```
2024-1000_File-Injection_Attacks_on_Searchable_Encryption,_Based_on_Binomial_Structures.pdf
```

## 做什么

- 按年份采集元数据（标题、作者、分类、摘要），存进本地 SQLite。
- 按年份、关键词或指定编号下载 PDF；已下载的自动跳过，失败的可以重试。
- 校验已下载文件是否完整，导出元数据为 CSV 或 JSON。
- 提供结构化输出和 MCP 服务端，方便脚本和 Claude Code、Codex 这类工具直接调用。

## 问题

站点的元数据是开放的，通过 OAI-PMH 接口即可获取。但 PDF 路径受 Cloudflare 的托管质询保护：用 urllib、requests 或 curl 直接请求一律返回 403，就连在浏览器页面里用脚本发起的 fetch 请求也不行。只有浏览器地址栏导航这种形状的请求才能拿到文件。

另外，OAI-PMH 返回的日期是最后修改时间，不是论文编号所属的年份，直接按日期归档会把跨年修改的论文归错年。

## 原理

1. 用 Chrome DevTools Protocol 驱动一个独立配置目录的真实浏览器，让它去导航 PDF 地址。在响应阶段拦截 PDF 响应，取出原始字节写到磁盘。这样得到的是站点原文件，不是重新渲染的副本。
2. 质询页和 PDF 用同一个地址返回。如果连质询页也拦截，页面脚本不会执行，质询永远完不成。程序识别出质询响应后暂时放开拦截，让浏览器自己完成质询，之后同一会话内的下载都能直接通过。
3. 论文归属年份以站点的年度列表页为准，OAI-PMH 只用来补充作者、分类和摘要。
4. 浏览器调试端口是独占资源，程序用文件锁把多个进程的下载串行化，避免互相干扰。

## 安装

需要 Python 3.10 或更新版本。

```bash
pip install .            # 装成 eprintdl 命令
eprintdl doctor          # 检查 Python、浏览器和网络
```

不想安装也可以直接在仓库目录下用 `python -m eprintdl` 代替 `eprintdl`。Windows 上可以用 `eprintdl.cmd`，Linux、macOS 和 WSL 上可以用 `eprintdl.sh`，这两个脚本从任意目录都能调用。

## 使用

```bash
eprintdl harvest --years 2024                 # 采集 2024 年的元数据
eprintdl download --ids 2024/1000 2023/1234   # 下载指定编号
eprintdl download --years 2024 --keyword lattice --limit 20
eprintdl status                               # 查看进度
eprintdl verify --redownload                  # 校验，损坏的重新下载
eprintdl retry                                # 重试失败项
eprintdl export --years 2024 --out papers.csv
```

下载时会弹出一个浏览器窗口，这是正常现象，不要关闭它。单篇通常 0.5 到 2 秒，第一篇需要过一次质询，约 2 到 8 秒。中途打断后重跑同一条命令会从断点继续。

默认每篇之间至少间隔 1 秒，请不要为了提速改小间隔或并行开多个实例。

数据默认放在仓库下的 `eprint-data/` 目录，可以在 `eprintdl.toml` 里改。配置按用户目录、项目目录、环境变量 `EPRINTDL_CONFIG`、命令行参数的顺序合并，后者覆盖前者。

### 给脚本和 LLM 工具调用

加 `--json` 会逐行输出 JSON 事件，最后一行是总结果，每篇的 `file_path` 是绝对路径。`--format json` 则输出单个 JSON 对象。退出码 0 表示全部成功，1 表示有失败项，2 表示参数或配置错误，3 表示等待下载锁超时。

```bash
eprintdl --json download --ids 2024/1000
eprintdl --format json search --keyword lattice --limit 10
```

仓库自带 MCP 服务端，暴露 `eprint_search`、`eprint_download`、`eprint_status`、`eprint_verify` 四个工具。运行下面的脚本生成 Claude Code 和 Codex 的注册配置，然后重启对应工具：

```bash
python tools/setup_llm_cli.py
```

在 WSL 里需要指定 Windows 版 Python，例如 `--python /mnt/c/.../python.exe`。原因是 WSL2 默认网络模式下，Linux 进程连不上 Windows 上 Chrome 的调试端口，服务端必须跑在 Windows 侧。此时工具返回的 `file_path` 是 Windows 路径，在 WSL 里读取前用 `wslpath -u` 转换。

## 合规

- 每篇论文的许可以其页面标注为准。本工具只做本地下载和整理，不重新分发内容。
- 站点的 robots.txt 不允许将 PDF 用于训练 AI 模型，也不允许爬虫抓取 PDF。请只下载研究需要的篇目，保持默认的请求间隔。
- 持续出现 403 说明站点在拒绝请求，此时应当停止，而不是加大请求量。

## 目录

```
eprintdl/          主程序
  cli.py           命令行入口
  mcp_server.py    MCP 服务端
  service.py       下载编排，命令行和 MCP 都调用这里
  download.py      通过浏览器抓取 PDF
  cdp.py           Chrome DevTools Protocol 客户端
  oai.py browse.py 元数据采集
  store.py         SQLite 存储
  config.py locking.py platform.py reporter.py utils.py
tools/             配置生成与自测脚本
eprintdl.toml      配置文件
```

## 许可

MIT，见 `LICENSE`。
