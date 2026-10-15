# eprintdl

批量下载 IACR Cryptology ePrint Archive（<https://eprint.iacr.org>）论文 PDF 的爬虫工具，支持通过 MCP 接入 Claude Code 和 Codex。仅用 Python 标准库实现，无需第三方包，只需安装 Chrome、Chromium 或 Edge。支持 Windows、Linux、macOS 和 WSL。

下载文件为站点原始 PDF，文件名格式为“年份-编号_标题”：

```
2024-1000_File-Injection_Attacks_on_Searchable_Encryption,_Based_on_Binomial_Structures.pdf
```

## 已知问题

ePrint 元数据可通过 OAI-PMH 接口获取，其中的日期为最后修改时间。站点robots.txt不允许爬虫批量下载论文 PDF ，因此需绕过该规则以及 Cloudflare 人机验证。单篇论文 PDF 只能通过浏览器手动下载，直接用 urllib、requests 或 curl 请求，在浏览器内通过脚本发起 fetch 请求均会返回 403。

## 工作原理

1. 通过 Chrome DevTools Protocol 控制使用独立配置目录的浏览器，访问 PDF 地址，拦截响应并将原始字节写入磁盘。
2. 验证页与 PDF 使用同一地址。拦截验证页会阻止页面脚本执行，导致验证无法完成。程序识别验证页后会暂时放行，由浏览器完成验证，后续下载可复用同一会话。
3. 浏览器调试端口独占使用，通过文件锁让多个进程依次下载，避免相互干扰。

## 安装

Python 3.10 或更新版本。

```bash
pip install .
eprintdl doctor
```

或在仓库目录下用 `python -m eprintdl` 代替 `eprintdl`。Windows 可使用 `eprintdl.cmd`，Linux、macOS 和 WSL 可使用 `eprintdl.sh`，两个脚本均可从任意目录调用。

## 使用方法

### 方式一：接入 Claude Code 或 Codex（推荐）

1. 生成 MCP 注册配置。脚本会探测本机 Python 路径，写出 `.mcp.json`（Claude Code）和 `.codex/config.toml`（Codex）：

```bash
python tools/setup_llm_cli.py
```

2. 重启 Claude Code 或 Codex。在 Claude Code 中用 `/mcp` 确认 `eprintdl` 已连接。
3. 在Claude Code 或 Codex使用prompt："下载 eprint 2024/1000"，模型会调用工具完成下载并返回 PDF 的绝对路径。

WSL 用户生成配置时需指定 Windows 版 Python，如 `python tools/setup_llm_cli.py --python /mnt/c/.../python.exe`。WSL2 默认网络模式下，Linux 进程无法连接 Windows Chrome 的调试端口，所以服务端需在 Windows 运行。返回的文件路径为 Windows 路径，需用 `wslpath -u` 转换。

### 方式二：命令行

只下载指定编号时可跳过第一步，程序会自动抓取标题。

```bash
eprintdl harvest --years 2024                               # 1. 采集 2024 年元数据，存入本地库
eprintdl download --ids 2024/1000 2023/1234                 # 2. 下载指定编号
eprintdl download --years 2024 --keyword lattice --limit 20 #    或按年份和关键词下载
eprintdl status                                             # 3. 查看进度
eprintdl verify --redownload                                #    校验文件，重新下载损坏项
eprintdl retry                                              #    重试失败项
eprintdl export --years 2024 --out papers.csv               #    导出元数据
```

供脚本调用时加 `--json`，逐行输出 JSON 事件，末行为汇总结果，每篇的 `file_path` 为绝对路径。`--format json` 输出单个 JSON 对象。退出码：0 表示全部成功，1 表示部分失败，2 表示参数或配置错误，3 表示等待下载锁超时。

```bash
eprintdl --json download --ids 2024/1000
eprintdl --format json search --keyword lattice --limit 10
```

### 注意事项

- 下载时会打开浏览器窗口，请勿关闭。单篇通常耗时 0.5 到 2 秒，首篇需完成人机验证，约 2 到 8 秒。
- 已下载的文件自动跳过，中断后重新运行同一命令即可继续。
- 默认下载间隔至少为 1 秒，后期需随机化延迟以及控制并发 **[TODO]**

## 声明

工具仅供学习交流使用，请勿重新分发文件，若持续出现 403，请停止下载防止IP被封。

## LICENSE

MIT，见 `LICENSE`。
