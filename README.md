# drs_deploy — DRS 高可用自动化部署脚本

DRS(数据复制软件)高可用集群的全流程自动化部署脚本。

## 部署架构

```
┌──────────────────────────────────────────────────────────────────┐
│  GaussDB + DRS-Service 节点(3 台共机)                            │
│   节点 1: GaussDB(node) + DRS-Service(primary) + Monitor-Agent  │
│   节点 2: GaussDB(node) + DRS-Service(standby) + Monitor-Agent  │
│   节点 3: GaussDB(node)                      + Monitor-Agent    │
├──────────────────────────────────────────────────────────────────┤
│  DRS-Node 节点(≥2 台,与上面异机)                                │
│   Node A: DRS-Node + Monitor-Agent                              │
│   Node B: DRS-Node + Monitor-Agent                              │
└──────────────────────────────────────────────────────────────────┘
```

## 安装阶段

| Phase | 内容 |
|---|---|
| 0 precheck | 校验 conf、SSH 连通性、软件包 |
| 1 gaussdb | 3 节点集中式 HA(quorum)安装 |
| 2 drs_service_primary | DRS-Service 主节点 |
| 3 drs_service_standby | DRS-Service 备节点(HA 必跑) |
| 4 drs_node | DRS-Node 安装(每节点独立) |
| 5 monitor_agent | Monitor-Agent 安装(所有节点自动识别) |
| 6 verify | TCP 端口探测 DRS-Service |

## 前置条件

1. **操作系统**:Kylin / UnionTech / BigCloud Enterprise Linux
2. **控制节点**:能 SSH 到所有目标节点,推荐免密(`ssh_key` 字段)
3. **磁盘分区**:已在每台目标节点上完成 `/` ≥100G、`/data/cluster` ≥1024G 的分区
4. **haveged/rngd 补熵服务**:每台目标节点需安装并启动
5. **目标节点 `/root/package/` 目录**:脚本会自动创建

## 快速开始

```bash
# 1. 复制示例 conf 并替换所有 CHANGE_ME
cp drs_deploy.conf.example drs_deploy.conf
vim drs_deploy.conf

# 2. 先做配置校验(不会下发任何命令到目标节点)
python3 drs_deploy.py drs_deploy.conf --check-conf

# 3. 跑单个阶段(推荐分阶段跑,便于排查)
python3 drs_deploy.py drs_deploy.conf --phase gaussdb
python3 drs_deploy.py drs_deploy.conf --phase drs_service_primary
python3 drs_deploy.py drs_deploy.conf --phase drs_service_standby
python3 drs_deploy.py drs_deploy.conf --phase drs_node
python3 drs_deploy.py drs_deploy.conf --phase monitor_agent
python3 drs_deploy.py drs_deploy.conf --phase verify

# 4. 一键全流程
python3 drs_deploy.py drs_deploy.conf
```

## 入参约定

- **conf 格式**:扁平 `key = "value"`,带引号的字符串、不带引号的是数字/布尔
- **行内注释**:`#` 前面必须有空格才视为注释(避免误伤 URL 中的 `#`)
- **不读取任何环境变量**,所有入参走 conf

## 日志风格

logback 风格:

```
[2026-08-26 14:23:01.234]  [INFO]  ===== Phase 1: 安装元数据库 GaussDB(3 节点集中式 HA)=====
[2026-08-26 14:23:01.345]  [INFO]  --- 1.1 上传 GaussDB 软件包到 3 节点 ---
[2026-08-26 14:23:02.123]  [INFO]  scp /root/package/GaussDBInstaller.tar.gz -> [10.0.0.1]:/root/package/gauss/
```

## 常见问题

| 症状 | 排查 |
|---|---|
| `SSH 连通性失败` | 确认 `ssh_key` 已分发到目标节点 `~/.ssh/authorized_keys` |
| `installCluster installation is successful.` 未匹配 | 检查 `/data/GaussDBInstaller/install_cluster.log` |
| DRS-Service 端口未监听 | 检查 `use_cgroup` 是否与场景匹配;查看 `/opt/drs/logs/` 下的 install 日志 |
| Monitor-Agent 卡在交互 | 脚本用 heredoc 喂入 4 行答案;若仍卡,需手动到节点执行 `cd /root/package/Monitor-Agent-* && sh install.sh` |