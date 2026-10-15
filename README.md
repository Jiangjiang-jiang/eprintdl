# eprintdl

批量下载 IACR ePrint（<https://eprint.iacr.org>）论文 PDF 的爬虫工具，可通过 MCP 接入 Claude Code 和 Codex。纯 Python 标准库，只依赖本机 Chrome、Chromium 或 Edge。支持 Windows、Windows (WSL)、Linux。

文件名为“年份-编号_标题”，内容是站点原始 PDF：

```
2024-1000_File-Injection_Attacks_on_Searchable_Encryption,_Based_on_Binomial_Structures.pdf
```

## 已知问题

元数据可以从 OAI-PMH 接口拿到，但 PDF 下载 受 Cloudflare 人机验证保护，站点 robots.txt 也不允许爬虫下载 PDF。用 urllib、requests、curl 请求，或在浏览器里用脚本发 fetch，均返回 403，只有浏览器直接打开地址才能下载。

## 工作原理

1. 用 Chrome DevTools Protocol 控制一个独立配置目录的浏览器打开 PDF 地址，拦截响应，把原始字节写到磁盘。
2. 验证页和 PDF 是同一个地址。拦截验证页会让页面脚本跑不起来，导致过不了验证，所以识别到验证页就暂时放行，让浏览器自己过验证，之后同一会话里的下载都能直接通过。
3. 独占浏览器调试端口，用文件锁让多个进程排队下载。

## 安装

需要 Python 3.10 以上和桌面环境，下载时会弹出浏览器窗口。Ubuntu 自带的 snap 版 Chromium 只能读写家目录，数据目录要放在家目录下。

```bash
pip install .
eprintdl doctor
```

`doctor` 会检查 Python、浏览器和网络，并启动一次浏览器试连。也可以在仓库目录下用 `python -m eprintdl`，或者直接调用 `eprintdl.cmd`、`eprintdl.sh`。

## 使用

### 接入 Claude Code 或 Codex（推荐）

```bash
python tools/setup_llm_cli.py
```

脚本会写出 `.mcp.json`、`.codex/config.toml` 和用户级 `~/.codex/config.toml`，重启 Claude Code 或 Codex 后的输入prompt“下载 eprint 2024/1000”即可。

WSL 下脚本会自动选 Windows 版 Python，这是因为 WSL2 默认网络下 Linux 进程连不上 Windows Chrome 的调试端口，无法找到时用 `--python /mnt/c/.../python.exe` 指定。返回的文件路径为 Windows 路径，需用 `wslpath -u` 转换。默认下载目录为 `eprint-data/`。

### 命令行

```bash
eprintdl harvest --years 2024                               # 采集元数据
eprintdl download --ids 2024/1000 2023/1234                 # 按编号下载，不需要先采集
eprintdl download --years 2024 --keyword lattice --limit 20 # 按年份和关键词下载
eprintdl status                                             # 进度
eprintdl verify --redownload                                # 校验并重下损坏文件
eprintdl retry                                              # 重试失败项
eprintdl export --years 2024 --out papers.csv               # 导出元数据
```

加 `--json` 逐行输出 JSON，末行是汇总，`file_path` 是绝对路径；`--format json` 输出单个对象。退出码 0 成功，1 部分失败，2 参数错误，3 等锁超时。

### 注意

- 下载时会打开浏览器窗口，请勿关闭。单篇通常耗时 0.5 到 2 秒，首篇需完成人机验证，约 2 到 8 秒。
- 已下载的文件自动跳过，中断后重新运行同一命令即可继续。
- 默认下载间隔至少为 1 秒，后期需随机化延迟以及控制并发 **[TODO]**

## 声明

仅供学习交流，请勿重新分发文件。持续 403 请停止下载，以免 IP 被封。

## LICENSE

MIT，见 `LICENSE`。
