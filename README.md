# eprintdl

批量下载 IACR ePrint（<https://eprint.iacr.org>）论文 PDF 的爬虫工具，可通过 MCP 接入 Claude Code 和 Codex。纯 Python 标准库，只依赖本机的 Chrome、Chromium 或 Edge。支持 Windows、Linux、macOS、WSL。

文件名为“年份-编号_标题”，内容是站点原始 PDF：

```
2024-1000_File-Injection_Attacks_on_Searchable_Encryption,_Based_on_Binomial_Structures.pdf
```

## 已知问题

元数据可以从 OAI-PMH 接口拿到，但 PDF 受 Cloudflare 人机验证保护，站点 robots.txt 也不允许爬虫下载 PDF。用 urllib、requests、curl 请求，或在浏览器里用脚本发 fetch，一律返回 403，只有浏览器直接打开地址才能下载。

## 工作原理

1. 用 Chrome DevTools Protocol 控制一个独立配置目录的浏览器打开 PDF 地址，拦截响应，把原始字节写到磁盘。
2. 验证页和 PDF 是同一个地址。拦截验证页会让页面脚本跑不起来，验证永远过不去，所以识别到验证页就暂时放行，让浏览器自己过验证，之后同一会话里的下载都能直接通过。
3. 浏览器调试端口是独占的，用文件锁让多个进程排队下载。

## 安装

需要 Python 3.10 以上和桌面环境，下载时会弹出浏览器窗口，没有图形界面的服务器用不了。Ubuntu 自带的 snap 版 Chromium 只能读写家目录，数据目录要放在家目录下。

```bash
pip install .
eprintdl doctor
```

`doctor` 会检查 Python、浏览器和网络，并真的启动一次浏览器试连。不装也行，在仓库目录下用 `python -m eprintdl`，或者直接调用 `eprintdl.cmd`、`eprintdl.sh`。

## 使用

### 接入 Claude Code 或 Codex

```bash
python tools/setup_llm_cli.py
```

脚本会写出 `.mcp.json`、`.codex/config.toml` 和用户级 `~/.codex/config.toml`，重启 Claude Code 或 Codex 后直接说“下载 eprint 2024/1000”即可。Codex 只在信任的项目里读项目级配置，所以也写了用户级，不想改的话加 `--no-codex-user`。

WSL 下脚本会自动选 Windows 版 Python，因为 WSL2 默认网络下 Linux 进程连不上 Windows Chrome 的调试端口。找不到时用 `--python /mnt/c/.../python.exe` 指定。返回的路径是 Windows 格式，用 `wslpath -u` 转一下。

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

- 浏览器窗口不要关。单篇 0.5 到 2 秒，首篇要过验证，2 到 8 秒。中断后重跑同一命令即可续传。
- WSL 里直接用命令行下载，需要 WSL 内有 Linux 版浏览器，或把 WSL 网络改成镜像模式，否则用 Windows 版 Python 跑。
- 数据默认在仓库下的 `eprint-data/`；装成命令后在别的目录跑，默认在 `~/eprint-data/`，用 `eprintdl init` 生成配置文件可改。
- 采集时年度页返回 403 是当前网络被站点验证拦了，换个网络或稍后再试，OAI-PMH 不受影响。
- 默认下载间隔至少为 1 秒，后期需随机化延迟以及控制并发 **[TODO]**

## 声明

仅供学习交流，请勿重新分发文件。持续 403 请停止下载，以免 IP 被封。

## LICENSE

MIT，见 `LICENSE`。
