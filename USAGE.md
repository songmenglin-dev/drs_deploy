# drs_deploy 使用说明

DRS(数据复制软件)高可用集群的自动化部署脚本使用说明。

## 1. 前置

| 项 | 要求 |
|---|---|
| 控制节点 | 已 `ssh-keygen` 生成密钥,`id_rsa.pub` 已追加到所有目标节点 `~/.ssh/authorized_keys` |
| 目标节点 OS | Kylin / UnionTech / BigCloud Enterprise Linux |
| 目标节点磁盘 | 已完成分区(`/ 100G` + `/data/cluster ≥1024G`) |
| 目标节点补熵服务 | `haveged` / `rngd` 已安装并启动 |
| 控制节点 Python | ≥ 3.7(只用标准库,无第三方依赖) |

## 2. 准备 conf

```bash
cp drs_deploy.conf.example drs_deploy.conf
vim drs_deploy.conf
```

最少要改的字段:

```ini
gaussdb_node1_ip = "10.0.0.1"        # 改成真实 IP
gaussdb_node2_ip = "10.0.0.2"
gaussdb_node3_ip = "10.0.0.3"
gaussdb_root_password = "..."        # 三节点 root 密码必须一致
gaussdb_dbUserPasswd = "..."         # 这个密码 DRS-Service 也要用
drs_service_primary_ip = "10.0.0.1"  # HA 时与 node1 同机
drs_service_standby_ip = "10.0.0.2"  # HA 时与 node2 同机
drs_node_ips = "10.0.0.10,10.0.0.11" # DRS-Node 节点
gaussdb_installer_tar / gaussdb_metadb_tar / drs_service_tar /
drs_node_tar / monitor_agent_tar  ← 全部放到 package_dir 下,按 glob 自动匹配
ssh_key = "/root/.ssh/id_rsa"
```

完整字段含义见 `drs_deploy.conf.example` 行内注释。

## 3. 部署

```bash
# 1) 先校验 conf(不联网)
python3 drs_deploy.py drs_deploy.conf --check-conf

# 2) 完整预检(校验 conf + 软件包存在 + SSH 连通)
python3 drs_deploy.py drs_deploy.conf --phase precheck

# 3) 分阶段执行(失败时只重跑当前阶段)
python3 drs_deploy.py drs_deploy.conf --phase gaussdb
python3 drs_deploy.py drs_deploy.conf --phase drs_service_primary
python3 drs_deploy.py drs_deploy.conf --phase drs_service_standby
python3 drs_deploy.py drs_deploy.conf --phase drs_node
python3 drs_deploy.py drs_deploy.conf --phase monitor_agent
python3 drs_deploy.py drs_deploy.conf --phase verify

# 4) 或一键全跑(等同上一步串起来)
python3 drs_deploy.py drs_deploy.conf
```

每个阶段成功后日志会打印 `Phase N 完成:...`。失败会 FATAL 退出,日志保留在控制台。

## 4. 验收

部署结束后:

1. 浏览器打开 `https://<drs_service_primary_ip>:7443/#/login`,默认账号 `admin` / `drs_service_admin_password`
2. 控制台 → 创建任务,等 1 分钟,DRS-Node 应出现在「任务可用 IP」列表
3. 默认 License 有效期 90 天,过期前需申请新 License

## 5. 排错

| 报错 | 看哪里 |
|---|---|
| `SSH 连通性失败` | 控制节点的 `ssh_key` 是否已 `ssh-copy-id` 到目标节点 |
| `installCluster installation is successful.` 未匹配 | `[10.0.0.1]:/data/GaussDBInstaller/install_cluster.log` |
| DRS-Service 端口 `7443` 未监听 | `[10.0.0.1]:/opt/drs/logs/` 下的 install 日志 |
| Monitor-Agent 卡在交互 | 脚本已用 heredoc 喂答案;若仍卡,手动到节点跑 `cd /root/package/Monitor-Agent-* && sh install.sh` |

完整命令列表与字段含义:

```bash
python3 drs_deploy.py --help
```