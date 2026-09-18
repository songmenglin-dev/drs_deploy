# drs_deploy — 项目规范

> 自动化部署 DRS(数据复制软件)高可用集群的 Python 脚本与配套文档。
> 本文件记录本项目的硬性要求与风格约定,所有改动必须遵守。

## 硬性要求(用户明确指定,不可违背)

1. **入参只走 conf 文件,不读环境变量**
   - 即使 `os.environ` 里有同名 key,也不能作为兜底
   - 所有入参(包括密码、IP、路径)在 `drs_deploy.conf` 中显式声明

2. **交付给客户的文档中,不得出现内部参考文档的引用**
   - `README.md`、`USAGE.md`、`drs_deploy.conf.example` 是客户文档
   - 禁止出现:`PDF`、`HCS`、`安装指南`、`第 X 章`、`表 4-X`、`步骤 X` 等指向内部 PDF 的引用
   - 客户看不到内部 PDF — 任何「参考 PDF X.Y」的描述都会让客户困惑
   - 客户文档要自洽:技术细节直接写出来,而不是「见 PDF 第 X 章」
   - 内部开发者注释(`drs_deploy.py` 内的 `#` 注释)不受此限

3. **conf 文件格式:扁平 `key = "value"`,字符串带引号,数字/布尔不带**
   - 行内注释:`#` 前必须有空格才视为注释(避免误伤 URL / 路径中的 `#`)
   - 注释剥离必须严格,不能因为字段内有空格就截断(参考 `_strip_inline_comment`)

4. **SSH 走密钥认证,不依赖密码提示**
   - 配置 `ssh_key` 字段指向控制节点的私钥
   - ssh 参数固定带 `BatchMode=yes`,无密钥时直接失败,不卡密码提示
   - 脚本不会帮你跑 `sshpass`(也没有 `sshpass` 这种外部依赖)

## 风格基线

参考 `/mnt/c/sml/project/py_project/gaussdb_instance_install_dev/03-create-instance/create-instance.py`(同仓兄弟项目)。

- **日志**:`[{time}] [{LEVEL}] {msg}`(`%Y-%m-%d %H:%M:%S.%f` 精确到 ms)
- **INFO/WARN → stdout,ERROR/FATAL → stderr**
- **`fatal()` 直接 `sys.exit(1)`**,不抛异常
- **conf 字段命名**:全小写下划线,按 phase 加前缀(`gaussdb_*`、`drs_service_*`、`drs_node_*`、`monitor_*`、`ssh_*`、`package_*`、`verify_*`)
- **`# === xxx ===` 分节标题** 标识函数分组
- **依赖**:仅标准库;`Python ≥ 3.7`(用 `from __future__ import annotations` + `Dict/List/Optional/Tuple` 兼容 3.7)

## 安全要求

1. **conf 字段进入远程 shell 字符串前必须校验**
   - `_safe_for_remote()`:禁止换行 + 任何 shell 元字符(`;|&$()<>\`'"`)
   - `_safe_for_shell()`:拒绝换行;`*_user` / `*_group` 类字段强制 `[A-Za-z0-9_.-]+`
   - 写入 heredoc(<<'EOF')的密码字段也要过 `_safe_for_shell`(quoted EOF 防不住换行符)

2. **日志脱敏**
   - `_LOG_REDACT` 正则匹配 `password|ca_phrase|passwd` 字段值,替换成 `***`
   - `ssh_run()` 打印 `remote_cmd` 预览前先脱敏,避免密码进 stdout/日志文件

3. **不要把交互式输入拼给 `bash -s` 当脚本执行**
   - Monitor-Agent 的 answers 必须用真 here-doc:`sh install.sh <<'EOF'\n...answers...\nEOF`

4. **DRY-RUN 检查不要遗漏:**
   - `phase_gaussdb` 上传前要校验 `installer_tar` / `metadb_tar` 都已配置(否则 scp 一半再 fatal)
   - `phase_precheck` + `phase_monitor_agent` 的 IP 列表必须 `if ip` 过滤空串
   - 重跑前 `rm -rf /root/package/X-*` 清理残留,避免 glob 命中多个导致后续 `cd` 失败

## 文件布局

```
drs_deploy/
├── drs_deploy.py              # 主脚本(单文件,Python ≥ 3.7)
├── drs_deploy.conf.example    # 示例 conf(客户文档,无 PDF 引用)
├── README.md                  # 项目总览(客户文档,无 PDF 引用)
├── USAGE.md                   # 使用说明(客户文档,无 PDF 引用)
├── CLAUDE.md                  # 本文件(开发者内部规范)
└── docs/                      # 内部参考资料(不交付给客户)
```

## 改动流程

1. 改前先 `cat drs_deploy.py | head -50` 看顶部 docstring + 阶段注释确认无 PDF 残留
2. 改完跑:
   ```bash
   python3 -m py_compile drs_deploy.py                       # 语法检查
   python3 drs_deploy.py drs_deploy.conf.example --check-conf # conf 结构校验
   ```
3. 在客户文档中加任何文字前,自查不出现 `PDF`/`HCS`/`安装指南`/`第 X 章`/`表 4-X`/`步骤 X`
4. 涉及 conf 字段进入 shell 字符串的代码,先过 `_safe_for_remote` / `_safe_for_shell`
5. 涉及日志输出包含 conf 字段的代码,先过 `_LOG_REDACT`

## 不要做的事

- 不要加 `bandit` / `pytest` / `black` 等第三方依赖(本脚本刻意零依赖)
- 不要把 `install.conf` 改成 scp 推送本地文件(是大重构,目前 heredoc + 校验已足够防御)
- 不要加 `--phase` 之间的依赖约束(显式约束会限制单阶段调试的灵活性)
- 不要把 `create-instance.py` 复制粘贴过来 — 风格参考它的「形」(日志、conf、分节),而不是「内容」

## 阶段映射(开发者内部备忘)

脚本内的阶段编号对应原 PDF 章节(仅供内部参考,不写入客户文档):

| 阶段 | 含义 | 原 PDF 章节 |
|---|---|---|
| precheck | 校验 conf + 软件包 + SSH | — |
| gaussdb | 元数据库 3 节点集中式 HA | 4.1 |
| drs_service_primary | DRS-Service 主节点 | 4.2 |
| drs_service_standby | DRS-Service 备节点 | 4.2 |
| drs_node | DRS-Node | 4.4 |
| monitor_agent | Monitor-Agent | 4.5 |
| verify | TCP 端口探测 | 5 |