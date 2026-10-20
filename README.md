# eprintdl

批量下载 IACR ePrint（<https://eprint.iacr.org>）论文 PDF 的爬虫工具，可通过 MCP 接入 Claude Code 和 Codex。纯 Python 标准库，只依赖本机 Chrome、Chromium 或 Edge。支持 Windows、Windows (WSL)、Linux。

文件名为“年份-编号_标题”，内容是站点原始 PDF：

```
2024-1000_File-Injection_Attacks_on_Searchable_Encryption,_Based_on_Binomial_Structures.pdf
```

## 已知问题

元数据可以从 OAI-PMH 接口拿到，但 PDF 下载受 Cloudflare 人机验证保护，站点规则 robots.txt 也不允许爬虫下载 PDF。用 urllib、requests、curl 请求，或在浏览器里用脚本发 fetch，均返回 403，只有在浏览器中手动打开地址才能下载。

## 工作原理

1. 用 Chrome DevTools Protocol 控制一个独立配置目录的浏览器打开 PDF 地址，拦截响应，把原始字节写到磁盘。
2. 由于验证页和 PDF 是同一个地址，拦截验证页会导致页面脚本无法执行，所以如果识别到验证页，暂时放行让浏览器自己过验证，之后同一会话里的下载都能直接通过。
3. 独占浏览器调试端口，用文件锁让多个进程排队下载。

## 环境

Python 3.10 以上和桌面环境，下载要靠一个真实的浏览器窗口，默认最小化启动。Ubuntu 自带的 snap 版 Chromium 只能读写`~/`，数据目录要放在`~/`下。下载目录默认为 `~/eprint-data/`，`eprintdl init` 写出 `eprintdl.toml` 后可改。

## 接入 Claude Code 或 Codex（推荐）

不需要安装，由 `uvx` 拉取并运行。Claude Code 的 `.mcp.json`：

```json
{
  "mcpServers": {
    "eprintdl": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/Jiangjiang-jiang/eprintdl", "eprintdl-mcp"]
    }
  }
}
```

Codex 的 `~/.codex/config.toml`：

```toml
[mcp_servers.eprintdl]
command = "uvx"
args = ["--from", "git+https://github.com/Jiangjiang-jiang/eprintdl", "eprintdl-mcp"]
startup_timeout_sec = 60.0
tool_timeout_sec = 300.0
```

重启后在 Claude Code 里用 `/mcp`、在 Codex 里用 `codex mcp list` 确认 eprintdl 已连接，之后输入prompt“下载 eprint 2024/1000”即可。

WSL 下要用 Windows 版的 `uvx.exe`，Linux 进程连不上 Windows Chrome 的调试端口，返回的路径用 `wslpath -u` 转换。

## 命令行

```bash
pip install .
eprintdl doctor
```

`doctor` 会检查 Python、浏览器和网络，并启动一次浏览器试连。也可以在仓库目录下用 `python -m eprintdl`，或者直接调用 `eprintdl.cmd`、`eprintdl.sh`。

```bash
eprintdl harvest --years 2024                               # 采集元数据
eprintdl download --ids 2024/1000 2023/1234                 # 按编号下载，不需要先采集
eprintdl download --years 2024 --keyword lattice --limit 20 # 按年份和关键词下载
eprintdl status                                             # 进度
eprintdl verify --redownload                                # 校验并重下损坏文件
eprintdl retry                                              # 重试失败项
eprintdl export --years 2024 --out papers.csv               # 导出元数据
```

加 `--json` 逐行输出 JSON，末行是汇总，`file_path` 是绝对路径；`--format json` 输出单个对象。退出码 0 成功，1 部分失败，2 参数错误，3 等锁超时。`python tools/setup_llm_cli.py` 可生成指向本地解释器的 MCP 配置。

## 注意

- 浏览器默认最小化启动，请勿手动关闭。不可用无头模式，会被 Cloudflare 拦截。
- 单篇通常耗时 0.5 到 2 秒，首篇需完成人机验证，约 2 到 8 秒。
- 已下载的文件自动跳过，中断后重新运行同一命令即可继续。
- **[TODO]**: 默认下载间隔至少为 1 秒，后期需随机化延迟以及控制并发，避免被网站识别。

## 声明

仅供学习交流，请勿重新分发文件。持续 403 请停止下载，以免 IP 被封。

## LICENSE

MIT，见 `LICENSE`。
