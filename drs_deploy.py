#!/usr/bin/env python3
from __future__ import annotations  # PEP 563 兼容 Python 3.7(推迟注解求值)
# drs_deploy.py — 自动化部署 DRS(数据复制软件)高可用集群
#
# 高可用部署架构:
#   3 台物理机:每台同时跑 GaussDB(中间库);其中 2 台还跑 DRS-Service(主 + 备)
#   ≥2 台物理机:DRS-Node(可创建主备任务,与 Service 异机)
#   Monitor-Agent 部署到上述所有节点
#
# 安装流程(脚本按此顺序执行):
#   1) 元数据库 GaussDB(3 节点集中式 HA quorum/paxos)
#   2) DRS-Service 主节点
#   3) DRS-Service 备节点(HA 时)
#   4) DRS-Node(每节点独立,可横向扩展)
#   5) Monitor-Agent(部署到所有节点,自动识别组件)
#   6) 验证:TCP 端口探测 DRS-Service
#
# 用法:
#   python3 drs_deploy.py drs_deploy.conf                       # 全流程部署
#   python3 drs_deploy.py drs_deploy.conf --check-conf          # 仅校验 conf 结构
#   python3 drs_deploy.py drs_deploy.conf --phase gaussdb       # 只跑元数据库
#   python3 drs_deploy.py drs_deploy.conf --phase drs_service_primary
#   python3 drs_deploy.py drs_deploy.conf --phase drs_service_standby
#   python3 drs_deploy.py drs_deploy.conf --phase drs_node
#   python3 drs_deploy.py drs_deploy.conf --phase monitor_agent
#   python3 drs_deploy.py drs_deploy.conf --phase verify
#   python3 drs_deploy.py drs_deploy.conf --dry-run             # 不实际下发,只打印计划
#
# 入参:扁平 conf 文件(严格按 key = "value",无 ENV 兜底)
# 日志:logback 风格 [{time}] [{LEVEL}] {msg}

import argparse
import glob
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from typing import Dict, List, Optional, Tuple  # 兼容 Python 3.7(无 PEP 585/604)


# 日志脱敏:打印远程命令预览时,把 password/ca_phrase/passwd 字段的值替换成 ***
_LOG_REDACT = re.compile(r"(password|ca_phrase|passwd)\s*=\s*\S+")

# shell 元字符(出现在 conf 字段值中会被外层 bash 解析,需要提前拒绝)
_SHELL_META = re.compile(r"[;|&$<>()`'\"]")

# 硬性硬件门槛(2026-09-19 用户要求:phase_precheck 直接 fatal,不达标就拦下)
_MIN_CPU_CORES = 4
_MIN_MEM_GB = 8
_MIN_DISK_GB = 300


def _safe_for_remote(value: str, key: str) -> str:
    """比 _safe_for_shell 更严:禁止任何 shell 元字符。
    用于会被 f-string 拼进远程 bash 脚本的字段(heredoc 之外的脚本体)。"""
    if not value:
        return value
    if "\n" in value or "\r" in value:
        fatal(f"参数 {key} 含换行符 | 修复:填单行值")
    if _SHELL_META.search(value):
        fatal(f"参数 {key} 含 shell 元字符 | 修复:不要在密码/IP 中使用 ;|&$()<>`'\"")
    return value


# === logback 风格日志(简化版:无 PID/脚本名,与 create-instance.py 一致)===
def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _log(level: str, msg: str, stream=None) -> None:
    s = stream or (sys.stderr if level in ("ERROR", "FATAL") else sys.stdout)
    print(f"[{_now()}]  [{level}]  {msg}", file=s)


def info(msg: str) -> None: _log("INFO", msg)
def warn(msg: str) -> None: _log("WARN", msg)
def error(msg: str) -> None: _log("ERROR", msg)
def fatal(msg: str) -> None:
    _log("FATAL", msg)
    sys.exit(1)
def debug(msg: str) -> None:
    # debug 默认开启(无 ENV 变量),靠 --quiet 抑制;调试时改 enable_debug() 即可
    if _DEBUG_ENABLED:
        _log("DEBUG", msg)


_DEBUG_ENABLED = False


# === conf 加载(扁平 key = "value",与 create-instance.py 严格一致;无 ENV 兜底)===
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_CONF: Dict[str, str] = {}
CONF_FILE: str = ""  # 加载后被 set,供 require_conf 报错时引用


def _safe_for_shell(v: str, key: str) -> str:
    """拒绝会破坏 shell 解析的值(换行 / EOF marker / shell 元字符)。
    conf 中要进入远程 heredoc 或 shell 字符串的字段都应先过这道关。"""
    if "\n" in v or "\r" in v:
        fatal(f"参数 {key} 含换行符,会被 shell 解析破坏 | 修复:在 conf 中填单行值")
    if not v:
        return v
    # 字段名类(用户/路径)允许的字符集
    if key.endswith("_user") or key.endswith("os_user") or "_group" in key:
        if not re.match(r"^[A-Za-z0-9_.-]+$", v):
            fatal(f"参数 {key}={v!r} 含非法字符 | 修复:仅允许 [A-Za-z0-9_.-]")
    return v


def _strip_inline_comment(v: str) -> str:
    """从等号右侧剥掉行内注释(`#` 前必须是空白,且 `#` 不在引号字符串内)。
    比 create-instance.py 的版本更鲁棒 — 后者只匹配单个空格的 ' #'。"""
    in_quote: Optional[str] = None
    for i, ch in enumerate(v):
        if in_quote:
            if ch == in_quote:
                in_quote = None
            continue
        if ch in ('"', "'"):
            in_quote = ch
            continue
        if ch == "#" and i > 0 and v[i - 1].isspace():
            return v[:i].rstrip()
    return v


def load_conf(conf_path: str) -> None:
    """解析 conf 写入 _CONF 字典。不读环境变量(用户明确要求)。"""
    global CONF_FILE
    CONF_FILE = conf_path
    if not os.path.isfile(conf_path):
        fatal(f"conf 文件不存在: {conf_path}")
    info(f"加载 conf: {conf_path}")
    with open(conf_path) as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip()
            # 行内注释:任意空白 + # 后面的内容(避免误伤 url/路径里的 #)
            # 用正则匹配"前面是空白、且不在引号字符串内的 #"
            v = _strip_inline_comment(v)
            # 剥引号(双引号/单引号)
            if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                v = v[1:-1]
            if not k:
                warn(f"conf 第 {lineno} 行: 空 key,跳过 (line={raw.rstrip()})")
                continue
            _CONF[k] = v


def _conf(key: str) -> str:
    """必填参数:从 _CONF 字典读,缺失 fatal。"""
    v = _CONF.get(key, "").strip()
    if not v:
        fatal(f"参数缺失: {key} (请检查 {CONF_FILE})")
    return v


def _opt_conf(key: str, default: str = "") -> str:
    """可选参数:从 _CONF 字典读,缺失返回 default。"""
    return _CONF.get(key, "").strip() or default


def _conf_bool(key: str, default: bool = False) -> bool:
    """布尔型:yes/no/true/false/0/1,大小写不敏感。"""
    raw = _opt_conf(key, "yes" if default else "no").lower()
    return raw in ("yes", "true", "1", "on")


def _conf_int(key: str) -> int:
    """整数型,缺/非整数直接 fatal。"""
    raw = _opt_conf(key, "")
    if not raw:
        fatal(f"参数缺失(需整数): {key} (in {CONF_FILE})")
    try:
        return int(raw)
    except ValueError:
        fatal(f"参数 {key}={raw!r} 不是合法整数 | 修复:在 conf 中填入数字")


def _split_list(value: str) -> List[str]:
    """逗号分隔列表,自动 trim 空白、过滤空项。"""
    return [s.strip() for s in value.split(",") if s.strip()]


# === SSH/SCP 封装(基于 sshpass 或密钥,无第三方依赖)===
def _ssh_base_args(ip: str) -> List[str]:
    """构造 ssh 命令的固定前缀(用户/端口/严格 host key / 禁 known_hosts / BatchMode)。"""
    user = _opt_conf("ssh_user", "root")
    port = _opt_conf("ssh_port", "22")
    cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=10",
        "-o", "BatchMode=yes",  # 无密钥时不要卡密码提示,直接失败
        "-p", port,
    ]
    key = _opt_conf("ssh_key", "")
    if key:
        cmd += ["-i", key]
    cmd.append(f"{user}@{ip}")
    return cmd


def ssh_run(ip: str, remote_cmd: str, timeout: int = 1800,
            check: bool = True, input_data: Optional[str] = None) -> Tuple[int, str, str]:
    """在远端节点执行命令,返回 (rc, stdout, stderr)。
    走 ssh 密钥认证(ssh_key 可选:留空走 ssh 默认的 ~/.ssh/id_rsa 等,
    填了则 -i 显式指定)。BatchMode=yes 避免无密钥时卡在密码提示。
    input_data 会拼到 remote_cmd 后面送进 stdin — 调用方需保证不是把交互式
    答案拼给 bash -s 当脚本执行(那种场景应改用 here-doc)。"""
    # ssh_key 留空时走 ssh 默认行为,BatchMode=yes 保证无密钥时直接 Permission denied,
    # 不会卡密码提示 — 留空也安全;连通性由 phase_precheck 的 echo OK 兜底。
    cmd = _ssh_base_args(ip) + ["bash", "-s"]
    # 日志脱敏:password/ca_phrase/passwd 值替换成 ***,避免泄露到日志
    preview = _LOG_REDACT.sub(r"\1 = ***", remote_cmd[:200])
    info(f"[{ip}] $ {preview}{'...' if len(remote_cmd) > 200 else ''}")
    stdin_payload = remote_cmd
    if input_data:
        stdin_payload = remote_cmd + "\n" + input_data
    try:
        proc = subprocess.run(
            cmd,
            input=stdin_payload,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        fatal(f"[{ip}] 命令超时(>{timeout}s): {remote_cmd[:120]}")
    except FileNotFoundError as e:
        fatal(f"ssh 不可用: {e} | 修复:安装 openssh-client(apt/yum install openssh-client)")
    if proc.returncode != 0 and check:
        error(f"[{ip}] rc={proc.returncode} | stderr={proc.stderr.strip()[:500]}")
        sys.exit(proc.returncode or 1)
    return proc.returncode, proc.stdout, proc.stderr


def scp_push(local_path: str, ip: str, remote_path: str) -> None:
    """本地文件推送到远端 /root/package/。"""
    user = _opt_conf("ssh_user", "root")
    port = _opt_conf("ssh_port", "22")
    key = _opt_conf("ssh_key", "")
    cmd = ["scp", "-P", port]
    if key:
        cmd += ["-i", key]
    cmd += ["-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            # 与 _ssh_base_args 一致:无密钥时不要卡密码提示,直接失败(秒级感知 vs 600s 超时)
            "-o", "BatchMode=yes",
            "-o", "PasswordAuthentication=no",
            "-o", "ConnectTimeout=10",
            local_path, f"{user}@{ip}:{remote_path}"]
    info(f"scp {local_path} -> [{ip}]:{remote_path}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        fatal(f"scp 超时: {local_path} -> [{ip}]")
    if proc.returncode != 0:
        fatal(f"scp 失败 rc={proc.returncode}: {proc.stderr.strip()}")


# === 阶段 0:预检(本地 conf + 软件包 + SSH 连通性)===


# 远端硬件探测脚本:输出 "HW:cpu=N mem_mb=N disk_gb=N"。
# 优先 lsblk 取物理磁盘总容量;若环境无 lsblk,回退到 df --total。
# stdin 喂给 ssh bash -s,因此可换行,可直接写 shell 语法。
_HW_PROBE_SCRIPT = r"""printf 'HW:cpu=%s mem_mb=%s disk_gb=%s\n' \
  "$(nproc)" \
  "$(free -m | awk '/^Mem:/{print $2}')" \
  "$(
    disk_bytes=$(lsblk -bn -d -o SIZE 2>/dev/null | awk 'BEGIN{s=0}{if($1~/^[0-9]+$/)s+=$1}END{print s+0}')
    if [ "${disk_bytes:-0}" -eq 0 ]; then
      disk_bytes=$(df -B1 --total 2>/dev/null | awk '/^total/{print $2}')
    fi
    echo $(( disk_bytes / 1073741824 ))
  )"
"""


def _check_hardware(ip: str) -> None:
    """硬性硬件检测:CPU ≥ _MIN_CPU_CORES 核、内存 ≥ _MIN_MEM_GB GB、硬盘 ≥ _MIN_DISK_GB GB。
    任一项不达标直接 fatal,避免规格不够时安装到一半才被安装器报错。"""
    rc, out, _ = ssh_run(ip, _HW_PROBE_SCRIPT, timeout=20, check=False)
    if rc != 0:
        fatal(f"[{ip}] 硬件检测命令执行失败 rc={rc} | "
              f"确保目标节点装有 nproc/free/lsblk 或 df(procps + util-linux)")
    m = re.search(r"HW:cpu=(\d+)\s+mem_mb=(\d+)\s+disk_gb=(\d+)", out)
    if not m:
        fatal(f"[{ip}] 硬件检测输出解析失败: {out.strip()[:200]!r}")
    cpu, mem_mb, disk_gb = int(m.group(1)), int(m.group(2)), int(m.group(3))
    mem_gb = (mem_mb + 1023) // 1024  # MB → GB,向上取整(避免 7.99GB 显示成 7)
    info(f"  [{ip}] 硬件: CPU={cpu}核, 内存={mem_gb}GB({mem_mb}MB), 硬盘={disk_gb}GB")
    fails = []
    if cpu < _MIN_CPU_CORES:
        fails.append(f"CPU {cpu}核 < {_MIN_CPU_CORES}核")
    if mem_gb < _MIN_MEM_GB:
        fails.append(f"内存 {mem_gb}GB < {_MIN_MEM_GB}GB")
    if disk_gb < _MIN_DISK_GB:
        fails.append(f"硬盘 {disk_gb}GB < {_MIN_DISK_GB}GB")
    if fails:
        fatal(f"[{ip}] 硬件不达标: {'; '.join(fails)}")


def check_conf() -> None:
    """仅校验 conf 解析 + 必填字段 + IP 合法性(不检查包存在、不探测 SSH)。"""
    info("===== check-conf: 校验 conf 结构 =====")
    # 1. 部署场景
    scene = _opt_conf("deploy_scene", "ha").lower()
    if scene not in ("ha", "independent"):
        fatal(f"deploy_scene={scene!r} 非法 | 修复:ha 或 independent")
    info(f"部署场景: {scene}")

    # 2. 元数据库节点(3 节点)
    for i in (1, 2, 3):
        ip = _opt_conf(f"gaussdb_node{i}_ip", "")
        if not ip:
            fatal(f"gaussdb_node{i}_ip 缺失 | 修复:在 conf 中填 GaussDB 节点 {i} 的管理 IP")
        if not _is_valid_ip(ip):
            fatal(f"gaussdb_node{i}_ip={ip!r} 不是合法 IP")

    # 3. GaussDB 必填密码
    require_conf(
        "gaussdb_os_user", "gaussdb_os_user_password", "gaussdb_root_password",
        "gaussdb_rdsAdminPasswd", "gaussdb_rdsMetricPasswd",
        "gaussdb_rdsReplPasswd", "gaussdb_rdsBackupPasswd", "gaussdb_dbUserPasswd",
    )

    # 4. DRS-Service:HA 需 primary + standby;independent 只需 primary
    primary_ip = _opt_conf("drs_service_primary_ip", "")
    if not _is_valid_ip(primary_ip):
        fatal("drs_service_primary_ip 缺失或非法 | 修复:填 DRS-Service 主节点 IP")
    if scene == "ha":
        standby_ip = _opt_conf("drs_service_standby_ip", "")
        if not _is_valid_ip(standby_ip):
            fatal("drs_service_standby_ip 缺失或非法(高可用场景) | 修复:填 DRS-Service 备节点 IP")
        if standby_ip == primary_ip:
            fatal("drs_service_primary_ip 与 drs_service_standby_ip 相同 | 修复:高可用场景必须分主备")
    require_conf("drs_service_admin_password")

    # 5. DRS-Node IPs(逗号分隔)
    node_ips_raw = _opt_conf("drs_node_ips", "")
    if not node_ips_raw:
        warn("drs_node_ips 为空:跳过 DRS-Node 安装阶段(只装 GaussDB + Service)")
    else:
        for ip in _split_list(node_ips_raw):
            if not _is_valid_ip(ip):
                fatal(f"drs_node_ips 中 {ip!r} 不是合法 IP")

    # 6. package_dir 必须配置(5 个 tar 由 _resolve_packages 在 phase_precheck 解析)
    if not _opt_conf("package_dir", ""):
        warn("package_dir 未配置:phase_precheck 会 FATAL")

    info("check-conf OK")


# 软件包 glob 解析规则 — 用户在 conf 中只填一个 package_dir,脚本按名字匹配
# (conf_key 沿用旧字段名,保证下游 phase_xxx 函数读 _CONF 的代码无感)
_PACKAGE_GLOBS = [
    ("gaussdb_installer_tar", "GaussDBInstaller_*.tar.gz",         "GaussDBInstaller"),
    ("gaussdb_metadb_tar",    "DBS-MetaDB_*_Centralized_*.tar.gz", "DBS-MetaDB"),
    ("drs_service_tar",       "DRS-Service-*.tar.gz",              "DRS-Service"),
    ("drs_node_tar",          "DRS-Node-*.tar.gz",                 "DRS-Node"),
    ("monitor_agent_tar",     "Monitor-Agent-*.tar.gz",            "Monitor-Agent"),
]


def _resolve_packages(package_dir: str) -> None:
    """从单一 package_dir 解析 5 个安装包,写回 _CONF。
    - 0 匹配:FATAL,提示用户把对应包放到目录
    - >1 匹配:FATAL,列出全部候选让用户删多余(避免脚本偷偷选错版本)
    - =1 匹配:INFO 打印实际选中的文件名,便于核对版本"""
    if not package_dir:
        fatal("package_dir 未配置 | 修复:在 conf 中填一个绝对路径,把 5 个 tar 放进去")
    if not os.path.isdir(package_dir):
        fatal(f"package_dir={package_dir!r} 不是目录或不存在 | 修复:mkdir -p 后把 5 个 tar 放进去")
    info(f"从 package_dir={package_dir} 解析安装包")
    for conf_key, glob_pattern, label in _PACKAGE_GLOBS:
        matches = sorted(glob.glob(os.path.join(package_dir, glob_pattern)))
        if not matches:
            fatal(f"{label}: {package_dir} 下未找到 {glob_pattern} | 修复:把 {label} 安装包放到该目录")
        if len(matches) > 1:
            listing = "\n  ".join(matches)
            fatal(f"{label}: {package_dir} 下 {glob_pattern} 匹配到 {len(matches)} 个,请只保留一个版本:\n  {listing}")
        _CONF[conf_key] = matches[0]
        info(f"  {label} → {os.path.basename(matches[0])}")


def phase_precheck() -> None:
    """完整预检:conf 结构 + 软件包解析 + SSH 连通 + 硬性硬件检测。"""
    info("===== Phase 0: 预检(precheck) =====")
    check_conf()

    # 软件包:从单一 package_dir 按 glob 解析,失败模式见 _resolve_packages
    _resolve_packages(_conf("package_dir"))

    key = _opt_conf("ssh_key", "")
    # ssh_key 可选:留空走 ssh 默认行为(尝试 ~/.ssh/id_rsa 等默认位置/默认名),
    # 填了则用 -i <key> 显式指定。BatchMode=yes 保证无密钥时直接 Permission denied,
    # 不会卡密码提示 — 留空也安全。
    if key:
        info(f"使用 ssh_key={key} 验证连通性 + 硬性硬件检测 "
             f"(门槛: CPU≥{_MIN_CPU_CORES}核 内存≥{_MIN_MEM_GB}GB 硬盘≥{_MIN_DISK_GB}GB)")
    else:
        info(f"ssh_key 未配置,走 ssh 默认行为验证连通性 + 硬性硬件检测 "
             f"(门槛: CPU≥{_MIN_CPU_CORES}核 内存≥{_MIN_MEM_GB}GB 硬盘≥{_MIN_DISK_GB}GB)")

    # 聚合所有目标 IP(去重保序):GaussDB + DRS-Service + DRS-Node
    scene = _opt_conf("deploy_scene", "ha").lower()
    node_ips_raw = _opt_conf("drs_node_ips", "")
    raw_targets = [
        _opt_conf("gaussdb_node1_ip"),
        _opt_conf("gaussdb_node2_ip"),
        _opt_conf("gaussdb_node3_ip"),
        _opt_conf("drs_service_primary_ip"),
        _opt_conf("drs_service_standby_ip") if scene == "ha" else "",
    ]
    if node_ips_raw:
        raw_targets += _split_list(node_ips_raw)
    # dict.fromkeys 保序去重(同一节点既是 GaussDB 又是 DRS-Service 也只跑一次)
    targets: List[str] = list(dict.fromkeys(ip for ip in raw_targets if ip))

    for ip in targets:
        rc, _, _ = ssh_run(ip, "echo OK", timeout=15, check=False)
        if rc != 0:
            fatal(f"SSH 连通性失败: {ip} | 修复:确认 {key} 已分发到 {ip}:~/.ssh/authorized_keys")
        info(f"  [{ip}] SSH OK")
        _check_hardware(ip)

    info("预检通过")


def _is_valid_ip(s: str) -> bool:
    """简单 IPv4 校验(够用即可,不严格处理 IPv6)。"""
    if not s:
        return False
    parts = s.split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(p) <= 255 for p in parts)
    except ValueError:
        return False


def require_conf(*keys: str) -> None:
    missing = [k for k in keys if not _CONF.get(k, "").strip()]
    if missing:
        fatal(f"conf 缺少必填参数: {' '.join(missing)} (in {CONF_FILE})")


# === 阶段 1:安装元数据库 GaussDB(3 节点集中式 HA)===
# 元数据库安装流程(3 节点集中式 HA):
#   互信 -> 上传包 -> 解压 -> 生成 install_cluster.conf + install_cluster.json
#   -> python3 gaussdb_install.py --action main -> 等待 "installCluster installation is successful."

GAUSSDB_INSTALL_DIR = "/data/GaussDBInstaller"
GAUSSDB_PKG_DIR = "/data/GaussDBInstaller/pkgDir"
GAUSSDB_PKG_STAGE = "/root/package/gauss"  # 步骤 4


def phase_gaussdb() -> None:
    info("===== Phase 1: 安装元数据库 GaussDB(3 节点集中式 HA)=====")
    nodes = [_opt_conf(f"gaussdb_node{i}_ip") for i in (1, 2, 3)]
    primary = nodes[0]

    # 1.1 上传软件包到三节点(用 scp,推到 /root/package/gauss/)
    info("--- 1.1 上传 GaussDB 软件包到 3 节点 ---")
    installer_tar = _opt_conf("gaussdb_installer_tar", "")
    metadb_tar = _opt_conf("gaussdb_metadb_tar", "")
    # 两个 tar 必须都配置,否则下游 unpack 会失败并把集群留成半残状态
    if not installer_tar or not metadb_tar:
        fatal("gaussdb_installer_tar 与 gaussdb_metadb_tar 必须全部配置才能跑 gaussdb 阶段")
    for ip in nodes:
        ssh_run(ip, f"mkdir -p {GAUSSDB_PKG_STAGE}", check=False)
        scp_push(installer_tar, ip, f"{GAUSSDB_PKG_STAGE}/GaussDBInstaller.tar.gz")
        scp_push(metadb_tar, ip, f"{GAUSSDB_PKG_STAGE}/DBS-MetaDB.tar.gz")

    # 1.2 解压 installer + metadb 到 /data(主节点执行,其他节点同步)
    info("--- 1.2 解压 GaussDBInstaller 与 DBS-MetaDB ---")
    ssh_run(primary, f"mkdir -p /data && tar -xvf {GAUSSDB_PKG_STAGE}/GaussDBInstaller.tar.gz -C /data")
    # 解压 metadb(包含 Adaptor / Kernel / OM 三包,解压后是目录)
    ssh_run(primary, f"cd {GAUSSDB_PKG_STAGE} && tar -xf DBS-MetaDB.tar.gz")
    # 把 3 个子包拷到 /data/GaussDBInstaller/pkgDir/
    ssh_run(primary, f"mkdir -p {GAUSSDB_PKG_DIR} && "
                  f"find {GAUSSDB_PKG_STAGE} -maxdepth 3 -name '*.tar.gz' -exec cp -n {{}} {GAUSSDB_PKG_DIR}/ \\;")

    # 1.3 生成 install_cluster.conf(步骤 10)
    info("--- 1.3 生成 install_cluster.conf ---")
    conf_text = _render_install_cluster_conf(nodes)
    ssh_run(primary, f"cat > {GAUSSDB_INSTALL_DIR}/install_cluster.conf <<'CONF_EOF'\n{conf_text}\nCONF_EOF")

    # 1.4 拷贝 3_nodes_centralized.json 模板(步骤 12)
    ssh_run(primary,
            f"cp -n {GAUSSDB_INSTALL_DIR}/jsonFileSample/3_nodes_centralized.json "
            f"{GAUSSDB_INSTALL_DIR}/install_cluster.json",
            check=False)

    # 1.5 生成 install_cluster.json(步骤 13)
    info("--- 1.5 生成 install_cluster.json ---")
    json_text = _render_install_cluster_json(nodes)
    ssh_run(primary, f"cat > {GAUSSDB_INSTALL_DIR}/install_cluster.json <<'JSON_EOF'\n{json_text}\nJSON_EOF")

    # 1.6 触发安装(步骤 14)
    info("--- 1.6 触发 gaussdb_install.py --action main ---")
    ssh_run(primary, f"cd {GAUSSDB_INSTALL_DIR} && python3 gaussdb_install.py --action main", timeout=3600)

    # 1.7 三节点 SSH 互信校验(gaussdb_os_user 身份,版本 1 全 6 条有向边)
    # 在 --action main 完成后、grep 回显之前:对 3 节点两两组合的 6 条有向边
    # (A→B、A→C、B→A、B→C、C→A、C→B)逐条 ssh_run 验证,日志逐条打印结果。
    # 任一失败 → 汇总后 fatal(不会立刻退出,先把 6 条都跑完再 fatal)。
    info("--- 1.7 三节点 SSH 互信校验(gaussdb_os_user,版本 1:全 6 条有向边) ---")
    _verify_gaussdb_mutual_trust(nodes)

    # 1.8 检查回显(步骤 15.1)
    info("--- 1.8 校验 installCluster installation is successful. ---")
    rc, out, _ = ssh_run(primary,
                         f"grep -F 'installCluster installation is successful.' "
                         f"{GAUSSDB_INSTALL_DIR}/install_cluster.log",
                         check=False)
    if rc != 0:
        fatal("GaussDB 安装回显未匹配 installCluster installation is successful. "
              f"| 查看 [{primary}]:{GAUSSDB_INSTALL_DIR}/install_cluster.log")

    # 1.9 集群健康校验(fatal,对应安装指南 步骤 15.2):
    # 仅看 install_cluster.log 的回显行不够 — gaussdb_install.py 若中途静默退出 0,
    # 日志里仍可能残留旧的 "installCluster installation is successful." 行。
    # 必须 cm_ctl query -Cvd 列出所有节点为 Normal,才说明 cm_agent/etcd/CN/DN 真正就绪。
    db_port = _opt_conf("gaussdb_db_port", "30100")
    os_user = _safe_for_shell(_conf("gaussdb_os_user"), "gaussdb_os_user")
    info(f"--- 1.9 切换 {os_user} cm_ctl 集群健康校验(步骤 15.2) ---")
    rc, out, err = ssh_run(primary,
                           f"su - {os_user} -c \"cm_ctl query -Cvd\"",
                           check=False, timeout=60)
    if rc != 0 or "Normal" not in out:
        fatal(f"GaussDB 集群健康校验失败 rc={rc} | {err.strip()[:500]}\n"
              f"     节点状态输出:\n{out.strip()[:1000]}\n"
              f"     修复:在 [{primary}] 上以 {os_user} 身份手动 "
              f"cm_ctl query -Cvd 查看节点状态,"
              f"参考 {GAUSSDB_INSTALL_DIR}/install_cluster.log 排查")

    info("Phase 1 完成:GaussDB 3 节点 HA 安装成功")


def _verify_gaussdb_mutual_trust(nodes: List[str]) -> None:
    """版本 1 全校验:验证 GaussDB 三节点之间以 gaussdb_os_user 身份互信的全部 6 条有向边。

    对每条 (src, dst) 边(src ≠ dst,共 3×2 = 6 条):
      1. 通过脚本自身的 ssh_key 从控制节点 ssh 到 src(走外层 ssh_run)
      2. 在 src 上 `su` 到 gaussdb_os_user
      3. 在 os_user 上下文里用 BatchMode=yes ssh 到 dst 的 os_user 跑 echo OK
      4. 收集 inner rc + stderr 回到日志,逐条打印 OK / ❌

    设计要点:
      - BatchMode=yes + PasswordAuthentication=no:无密钥直接报 Permission denied,
        不会卡密码提示(与 _ssh_base_args 一致;操作员也应确保脚本"硬性要求"第 4 条)
      - 全 6 条边都跑完再 fatal — 不在第一条失败时就退出,避免半残诊断
      - conf 字段进入 shell 前过 _safe_for_shell / _safe_for_remote
      - 每个 inner ssh 都用 30s 超时,避免某条边卡住拖死整个阶段

    失败时的 error 信息会列出所有失败的方向 + inner ssh 的 stderr 头 200 字,
    操作员可据此判断是 authorized_keys 没写对、还是 src 上没 ssh 私钥、还是其它。
    """
    os_user = _conf("gaussdb_os_user")
    ssh_port = _opt_conf("ssh_port", "22")
    # conf 字段进入 inner ssh 命令前过安全校验
    os_user_safe = _safe_for_shell(os_user, "gaussdb_os_user")
    ssh_port_safe = _safe_for_shell(ssh_port, "ssh_port")

    # 6 条有向边:src × dst,排除 src == dst
    pairs: List[Tuple[str, str]] = [
        (src, dst)
        for src in nodes
        for dst in nodes
        if src != dst
    ]
    info(f"  校验 {len(pairs)} 条有向边 (gaussdb_os_user={os_user_safe}, ssh_port={ssh_port_safe})")

    # inner ssh 选项:严控超时 + 显式禁密码 + BatchMode
    inner_ssh_opts = (
        "-o BatchMode=yes "
        "-o StrictHostKeyChecking=no "
        "-o UserKnownHostsFile=/dev/null "
        "-o ConnectTimeout=10 "
        "-o PasswordAuthentication=no "
        f"-p {ssh_port_safe}"
    )

    results: List[Tuple[str, str, bool, str]] = []  # (src, dst, ok, detail)

    for src, dst in pairs:
        # 这段 bash 送到 src 上执行:
        #   - su 到 os_user
        #   - inner ssh 到 dst,跑 echo OK
        #   - 无论成败,把 exit_code + stdout + stderr 都送回 stdout 段,通过 outer ssh 收回
        remote_cmd = f"""
if su {os_user_safe} -c "ssh {inner_ssh_opts} {os_user_safe}@{dst} echo OK" > /tmp/_drs_mutual.out 2> /tmp/_drs_mutual.err; then
    echo "exit_code=0"
else
    echo "exit_code=$?"
fi
echo "---stdout---"
cat /tmp/_drs_mutual.out
echo "---stderr---"
cat /tmp/_drs_mutual.err
rm -f /tmp/_drs_mutual.out /tmp/_drs_mutual.err
""".strip()

        outer_rc, out, err = ssh_run(src, remote_cmd, timeout=30, check=False)

        if outer_rc != 0:
            # 外层 SSH(控制节点 → src)就挂了 — 节点不可达 / 认证失败
            detail = (err or out).strip()[:200]
            results.append((src, dst, False, f"outer_ssh rc={outer_rc}: {detail}"))
            warn(f"  [互信][{src} → {dst}] ❌ 外层 SSH 失败 rc={outer_rc}: {detail[:120]}")
            continue

        # 解析内层 su + ssh 的执行结果(走 stdout 段标记)
        inner_rc: Optional[int] = None
        inner_stdout = ""
        inner_stderr = ""
        section = "header"
        for line in out.splitlines():
            if line == "---stdout---":
                section = "stdout"
                continue
            if line == "---stderr---":
                section = "stderr"
                continue
            if section == "header":
                if line.startswith("exit_code="):
                    try:
                        inner_rc = int(line[len("exit_code="):])
                    except ValueError:
                        inner_rc = None
            elif section == "stdout":
                inner_stdout += line + "\n"
            elif section == "stderr":
                inner_stderr += line + "\n"

        detail = (inner_stderr.strip() or inner_stdout.strip() or "(内层 ssh 无输出)")[:200]
        if inner_rc == 0:
            results.append((src, dst, True, ""))
            info(f"  [互信][{src} → {dst}] ✅ OK")
        else:
            results.append((src, dst, False, f"rc={inner_rc}: {detail}"))
            warn(f"  [互信][{src} → {dst}] ❌ rc={inner_rc} | {detail[:120]}")

    failed = [(s, d, m) for (s, d, ok, m) in results if not ok]
    passed_n = len(results) - len(failed)

    if failed:
        summary_lines = "\n".join(
            f"  ❌ {s} → {d}: {m}" for (s, d, m) in failed
        )
        fatal(
            f"GaussDB 三节点 SSH 互信校验失败 ({passed_n}/{len(pairs)} 通过):\n{summary_lines}\n"
            f"修复:在 src 节点上以 root 执行 ssh-copy-id,确认 {os_user_safe}@{dst} 的 "
            f"~/.ssh/authorized_keys 包含 src 的公钥。\n"
            f"     也可在所有节点之间为 {os_user_safe} 重新建立互信 "
            f"(例如用 ssh-keygen + ssh-copy-id 批量)。\n"
            f"     注意:本校验使用 BatchMode=yes + PasswordAuthentication=no,"
            f"密码 SSH 不会被接受 — 必须改为公钥互信。"
        )

    info(f"  互信校验全部通过 ({passed_n}/{len(pairs)})")


def _render_install_cluster_conf(nodes: List[str]) -> str:
    """install_cluster.conf 字段(高可用 3 节点集中式 HA)。
    必填字段从 conf 读,其余保持默认。
    密码走 heredoc 写入(<<'EOF' 不会扩展),但换行 / EOF marker 仍会破坏 — 因此先校验。"""
    user = _safe_for_shell(_conf("gaussdb_os_user"), "gaussdb_os_user")
    group = _safe_for_shell(_opt_conf("gaussdb_os_user_group", "dbgrp"), "gaussdb_os_user_group")
    user_pwd = _safe_for_shell(_conf("gaussdb_os_user_password"), "gaussdb_os_user_password")
    root_pwd = _safe_for_shell(_conf("gaussdb_root_password"), "gaussdb_root_password")
    ssh_port = _safe_for_remote(_opt_conf("ssh_port", "22"), "ssh_port")
    # 路径类字段进入 heredoc 前必须过 _safe_for_remote:拒绝换行 + 任何 shell 元字符
    # (防止 conf 写成 "/tmp/x$(touch /tmp/pwned)" 在主节点执行任意命令,
    #  或 conf 写成 "22\npwned" 提前关闭 heredoc 执行后续 shell)
    data_dir = _safe_for_remote(_opt_conf("gaussdb_data_dir", "/data/cluster"), "gaussdb_data_dir")
    gauss_home = _safe_for_remote(_opt_conf("gaussdb_home", "/opt/gaussdb/app"), "gaussdb_home")
    # node_ip_list 必须以英文逗号分隔
    node_ip_list = ",".join(nodes)
    return (
        f"os_user = {user}\n"
        f"os_user_group = {group}\n"
        f"os_user_home = /home/{user}\n"
        f"os_user_password = {user_pwd}\n"
        f"root_passwd = {root_pwd}\n"
        f"ssh_port = {ssh_port}\n"
        f"node_ip_list = {node_ip_list}\n"
        f"gauss_home = {gauss_home}\n"
        f"om_agent_port = 1888\n"
        f"mgr_net =\n"
        f"data_net =\n"
        f"virtual_net =\n"
        f"log_dir = /var/log/gaussdb\n"
        f"cn_dir = /opt/gaussdb/app/components/cn\n"
        f"gtm_dir = /opt/gaussdb/app/components/gtm\n"
        f"cm_dir = /opt/gaussdb/app/components/cm\n"
        f"tmp_dir = /opt/gaussdb/app/tmp\n"
        f"data_dir = {data_dir}\n"
        f"tool_dir = /opt/gaussdb/app/tool\n"
        f"etcd_dir = /opt/gaussdb/app/components/etcd\n"
    )


def _render_install_cluster_json(nodes: List[str]) -> str:
    """install_cluster.json 字段(clusterConf.cm 是 3 节点拓扑:az/rack/ip/dataIp/virtualIp)。
    clusterConf.cm 是 3 节点 GAUSSDB 主机的拓扑(az/rack/ip/dataIp/virtualIp)。"""
    db_port = _opt_conf("gaussdb_db_port", "30100")
    # JSON 里的所有密码都需先校验 — 与 _render_install_cluster_conf 同理
    for k in ("gaussdb_rdsAdminPasswd", "gaussdb_rdsMetricPasswd",
              "gaussdb_rdsReplPasswd", "gaussdb_rdsBackupPasswd", "gaussdb_dbUserPasswd"):
        _safe_for_shell(_conf(k), k)
    cm: List[dict] = []
    for i, ip in enumerate(nodes, 1):
        cm.append({
            # rack/az/dataIp/virtualIp 进 JSON-heredoc 前过 _safe_for_remote,
            # 拒绝换行(防止 "x\nJSON_EOF" 提前关闭 heredoc)+ shell 元字符
            "rack": _safe_for_remote(_opt_conf(f"gaussdb_node{i}_rack", f"rack{i}"),
                                     f"gaussdb_node{i}_rack"),
            "az": _safe_for_remote(_opt_conf(f"gaussdb_node{i}_az", f"AZ{i}"),
                                   f"gaussdb_node{i}_az"),
            "ip": ip,
            "dataIp": _safe_for_remote(_opt_conf(f"gaussdb_node{i}_data_ip", ip),
                                       f"gaussdb_node{i}_data_ip"),
            "virtualIp": _safe_for_remote(_opt_conf(f"gaussdb_node{i}_virtual_ip", ip),
                                          f"gaussdb_node{i}_virtual_ip"),
        })
    payload = {
        "rdsAdminUser": "rdsAdmin",
        "rdsAdminPasswd": _conf("gaussdb_rdsAdminPasswd"),
        "rdsMetricUser": "rdsMetric",
        "rdsMetricPasswd": _conf("gaussdb_rdsMetricPasswd"),
        "rdsReplUser": "rdsRepl",
        "rdsReplPasswd": _conf("gaussdb_rdsReplPasswd"),
        "rdsBackupUser": "rdsBackup",
        "rdsBackupPasswd": _conf("gaussdb_rdsBackupPasswd"),
        "dbPort": db_port,
        "dbUser": "root",
        "dbUserPasswd": _conf("gaussdb_dbUserPasswd"),
        "clusterMode": "ha",
        "params": {},
        "cnparams": {},
        "dnparams": {},
        "cmparams": {},
        "clusterConf": {
            "clusterName": "Gauss_XuanYuan",
            "encoding": "utf8",
            "shardingNum": 1,
            "replicaNum": 3,
            "solution": "hws",
            "consistencyProtocol": "quorum",
            "cm": cm,
            "shards": cm,  # 与 cm 配置保持一致
            "etcd": cm,    # 与 cm 配置保持一致
        },
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


# === 阶段 2/3:安装 DRS-Service(高可用时先主后备)===
# DRS-Service install.conf 有 [meta db] 和 [drs] 两个 section
DRS_SERVICE_INSTALL_DIR_PREFIX = "/root/package/DRS-Service"


def phase_drs_service_primary() -> None:
    ip = _opt_conf("drs_service_primary_ip")
    info(f"===== Phase 2: DRS-Service 主节点 [{ip}] =====")
    _install_drs_service(ip, node_type="primary")


def phase_drs_service_standby() -> None:
    scene = _opt_conf("deploy_scene", "ha").lower()
    if scene != "ha":
        info("deploy_scene=independent: 跳过 DRS-Service 备节点安装")
        return
    ip = _opt_conf("drs_service_standby_ip")
    info(f"===== Phase 3: DRS-Service 备节点 [{ip}] =====")
    # HA 重建场景下,可能只重装备节点(primary 已存在)。但若 primary 未装且
    # 用户 --phase drs_service_standby 误用,集群会留半残状态,这里 warn 提示
    primary_ip = _opt_conf("drs_service_primary_ip")
    if primary_ip:
        rc, _, _ = ssh_run(primary_ip, "test -d /opt/drs/service", check=False, timeout=10)
        if rc != 0:
            warn(f"DRS-Service 主节点 [{primary_ip}] 上 /opt/drs/service 不存在,"
                 f"备节点单独安装后集群将处于半残状态 | 确认是 HA 重建,否则请先跑 --phase drs_service_primary")
    _install_drs_service(ip, node_type="standby")


def _install_drs_service(ip: str, node_type: str) -> None:
    """DRS-Service 安装通用流程:上包、解压、生成 install.conf、预检、安装。"""
    # 2.1 上传并解包
    info(f"--- DRS-Service[{node_type}]: 上传并解包到 [{ip}] ---")
    ssh_run(ip, "mkdir -p /root/package", check=False)
    tar = _opt_conf("drs_service_tar", "")
    if not tar:
        fatal("drs_service_tar 未配置 | 修复:在 conf 中填 DRS-Service 安装包路径")
    scp_push(tar, ip, "/root/package/DRS-Service.tar.gz")
    # 解包前清理残留目录,避免重跑时 glob 命中多个匹配导致后续 cd 失败
    # 同时校验解压后目录数恰好 1(嵌套子目录会被 `cd DRS-Service-*` 静默跳过)
    rc, out, _ = ssh_run(
        ip,
        "cd /root/package && rm -rf DRS-Service-* && "
        "tar -xzf DRS-Service.tar.gz && "
        "n=$(ls -d DRS-Service-* 2>/dev/null | wc -l) && "
        "[[ $n == 1 ]] && ls -d DRS-Service-* || "
        "{ echo \"expected 1 dir, got $n\"; exit 2; }",
        check=False,
    )
    if rc != 0:
        fatal(f"DRS-Service[{node_type}] [{ip}] 解压后目录数异常 | {out.strip()[:300]}")

    # 2.2 渲染 install.conf
    info(f"--- DRS-Service[{node_type}]: 渲染 install.conf ---")
    conf_text = _render_drs_service_install_conf(node_type)
    ssh_run(ip, f"cat > /root/package/DRS-Service-*/install.conf <<'CONF_EOF'\n{conf_text}\nCONF_EOF")

    # 2.3 预检(步骤 8)
    info(f"--- DRS-Service[{node_type}]: 预检 precheck_env.sh ---")
    rc, _, err = ssh_run(ip, "cd /root/package/DRS-Service-* && sh precheck_env.sh",
                         check=False, timeout=300)
    if rc != 0:
        fatal(f"DRS-Service[{node_type}] 预检失败 | 修复:查看 stderr 或看 stderr\n{err}")

    # 2.4 安装(步骤 9)
    info(f"--- DRS-Service[{node_type}]: 安装 install.sh ---")
    ssh_run(ip, "cd /root/package/DRS-Service-* && sh install.sh", timeout=3600)

    # 2.5 post-install 校验(fatal,对应安装指南 步骤 10):仅 ssh_run 返回 0 不够,
    # 还要确认 install_dir 真生成、external_port 真正监听。否则远程 install.sh
    # 静默退出 0 但实际未装好(磁盘满、依赖缺失等)会被误报成功。
    info(f"--- DRS-Service[{node_type}]: post-install 校验(步骤 10) ---")
    install_dir = _opt_conf("drs_service_install_dir", "/opt/drs/service")
    rc, _, _ = ssh_run(ip, f"test -d {install_dir}", check=False, timeout=10)
    if rc != 0:
        fatal(f"DRS-Service[{node_type}] 安装目录 {install_dir} 未创建,"
              f"安装可能未真正完成 | 查看 [{ip}] install.log")
    port = _opt_conf("drs_service_external_port", "7443")
    # 不只看端口 LISTEN,还要确认监听进程是 drs-service —
    # 否则别的程序占住 7443 会被误判成功。
    # ss -ltnp 可能需要 root 权限才能看到其他用户的进程 cmd,所以失败时降级 warn
    rc, out, err = ssh_run(ip,
                           f"ss -ltnp 'sport = :{port}' 2>/dev/null | grep -E 'drs[_-]?service|drs_server'",
                           check=False, timeout=10)
    if rc != 0:
        # 降级:只看 LISTEN(可能 ss 无权读 cmd)
        rc2, _, _ = ssh_run(ip, f"ss -ltn 'sport = :{port}' | grep -q LISTEN",
                            check=False, timeout=10)
        if rc2 != 0:
            fatal(f"DRS-Service[{node_type}] 端口 {port} 未监听 "
                  f"| 查看 [{ip}] install.log\n{err.strip()[:300]}")
        warn(f"DRS-Service[{node_type}] 端口 {port} 在监听但 ss 无法看到进程名"
             f"(可能 ss 无权限) | 手动 ss -ltnp 确认是 drs-service")

    info(f"DRS-Service[{node_type}] 安装完成(post-install 校验通过)")


def _render_drs_service_install_conf(node_type: str) -> str:
    """DRS-Service install.conf 字段(meta db: 元数据库连接;drs: 服务端口、admin 账号、部署场景)。"""
    gauss_nodes = [_opt_conf(f"gaussdb_node{i}_ip") for i in (1, 2, 3)]
    db_port = _opt_conf("gaussdb_db_port", "30100")
    meta_db_address = ",".join(f"{ip}:{db_port}" for ip in gauss_nodes)
    ca_phrase = _opt_conf("drs_service_ca_phrase", "")
    if not ca_phrase:
        ca_phrase = _conf("drs_service_admin_password")  # 默认与 drs_admin_password 一致
    # 进入 heredoc 的密码字段都需要校验
    _safe_for_shell(_conf("gaussdb_dbUserPasswd"), "gaussdb_dbUserPasswd")
    _safe_for_shell(_conf("gaussdb_rdsReplPasswd"), "gaussdb_rdsReplPasswd")
    _safe_for_shell(_conf("drs_service_admin_password"), "drs_service_admin_password")
    _safe_for_shell(ca_phrase, "drs_service_ca_phrase")
    return (
        "[meta db]\n"
        f"metaDB_engine = gaussdb\n"
        f"metaDB_address = {meta_db_address}\n"
        f"metaDB_root_user = root\n"
        f"metaDB_root_password = {_conf('gaussdb_dbUserPasswd')}\n"
        f"metaDB_drs_user = drs\n"
        f"metaDB_drs_password = {_conf('gaussdb_rdsReplPasswd')}\n"
        "\n"
        "[drs]\n"
        # 非密码字段(external_ip/port/admin_user/deploy_scene/use_cgroup/is_rebuild)
        # 进 heredoc 前过 _safe_for_remote,拒绝换行 + shell 元字符,防止 conf
        # 写成 "x\nCONF_EOF" 提前关闭 heredoc 执行后续 shell
        f"external_ip = {_safe_for_remote(_opt_conf(f'drs_service_{node_type}_ip'), f'drs_service_{node_type}_ip')}\n"
        f"external_port = {_safe_for_remote(_opt_conf('drs_service_external_port', '7443'), 'drs_service_external_port')}\n"
        f"drs_admin_username = {_safe_for_remote(_opt_conf('drs_service_admin_user', 'admin'), 'drs_service_admin_user')}\n"
        f"drs_admin_password = {_conf('drs_service_admin_password')}\n"
        f"ca_phrase = {ca_phrase}\n"
        f"deploy_scene = {_safe_for_remote(_opt_conf('deploy_scene', 'ha'), 'deploy_scene')}\n"
        f"node_type = {node_type}\n"
        f"use_cgroup = {_safe_for_remote(_opt_conf('drs_service_use_cgroup', 'no'), 'drs_service_use_cgroup')}\n"
        f"is_rebuild = {_safe_for_remote(_opt_conf('drs_service_is_rebuild', 'no'), 'drs_service_is_rebuild')}\n"
    )


# === 阶段 4:安装 DRS-Node(每节点独立 install.conf)===
def phase_drs_node() -> None:
    info("===== Phase 4: 安装 DRS-Node =====")
    node_ips = _split_list(_opt_conf("drs_node_ips", ""))
    if not node_ips:
        warn("drs_node_ips 为空,跳过 DRS-Node 安装")
        return
    tar = _opt_conf("drs_node_tar", "")
    if not tar:
        fatal("drs_node_tar 未配置 | 修复:在 conf 中填 DRS-Node 安装包路径")
    for ip in node_ips:
        info(f"--- DRS-Node [{ip}] ---")
        ssh_run(ip, "mkdir -p /root/package", check=False)
        scp_push(tar, ip, "/root/package/DRS-Node.tar.gz")
        # 校验解压后恰好 1 个 DRS-Node-* 目录,避免嵌套子目录导致
        # 后续 `cd /root/package/DRS-Node-*` 装错树
        rc, out, _ = ssh_run(ip,
                             "cd /root/package && rm -rf DRS-Node-* && "
                             "tar -xzf DRS-Node.tar.gz && "
                             "n=$(ls -d DRS-Node-* 2>/dev/null | wc -l) && "
                             "[[ $n == 1 ]] && ls -d DRS-Node-* || "
                             "{ echo \"expected 1 dir, got $n\"; exit 2; }",
                             check=False)
        if rc != 0:
            fatal(f"DRS-Node [{ip}] 解压后目录数异常 | {out.strip()[:300]}")
        conf_text = _render_drs_node_install_conf(ip)
        ssh_run(ip, f"cat > /root/package/DRS-Node-*/install.conf <<'CONF_EOF'\n{conf_text}\nCONF_EOF")
        rc, _, err = ssh_run(ip, "cd /root/package/DRS-Node-* && sh precheck_env.sh",
                             check=False, timeout=300)
        if rc != 0:
            fatal(f"DRS-Node [{ip}] 预检失败:\n{err}")
        ssh_run(ip, "cd /root/package/DRS-Node-* && sh install.sh", timeout=3600)
        # Post-install 校验(fatal):install.sh 静默退出 0 不等于安装成功。
        # 至少确认 install_dir 存在;DRS-Node 的 data_port(默认 443)用于业务接入,
        # 若未监听则后续 drs_node 注册到 DRS-Service 也会失败。
        info(f"--- DRS-Node [{ip}]: post-install 校验 ---")
        node_install_dir = _opt_conf("drs_node_install_dir", "/opt/drs/node")
        rc, _, _ = ssh_run(ip, f"test -d {node_install_dir}", check=False, timeout=10)
        if rc != 0:
            fatal(f"DRS-Node [{ip}] 安装目录 {node_install_dir} 未创建,"
                  f"安装可能未真正完成 | 查看 [{ip}] install.log")
        data_port = _opt_conf("drs_node_data_port", "443")
        # 不只看端口 LISTEN,还要确认监听进程是 drs-node — 否则别的程序占端口会误判
        rc, out, err = ssh_run(ip,
                               f"ss -ltnp 'sport = :{data_port}' 2>/dev/null | grep -E 'drs[_-]?node|drs_node'",
                               check=False, timeout=10)
        if rc != 0:
            rc2, _, _ = ssh_run(ip, f"ss -ltn 'sport = :{data_port}' | grep -q LISTEN",
                                check=False, timeout=10)
            if rc2 != 0:
                fatal(f"DRS-Node [{ip}] data_port {data_port} 未监听 "
                      f"| 查看 [{ip}] install.log\n{err.strip()[:300]}")
            warn(f"DRS-Node [{ip}] data_port {data_port} 在监听但 ss 无法看到进程名"
                 f"| 手动 ss -ltnp 确认是 drs-node")

        # 常见失败模式:DRS-Node 本地装好但未注册到 DRS-Service。
        # 不强制 fatal(日志路径/格式可能因版本变化),仅 warn 提示操作员确认。
        rc, _, _ = ssh_run(ip,
                           f"grep -rE 'register.*success|registered.*drs[_-]?service|node joined' "
                           f"/opt/drs/node/log/ 2>/dev/null | head -3",
                           check=False, timeout=10)
        if rc != 0:
            warn(f"DRS-Node [{ip}] 未检测到注册成功日志(可能未注册到 DRS-Service)"
                 f"| 手动登录 DRS-Service 控制台或 drs_node --list 确认")
    info("Phase 4 完成:DRS-Node 安装成功(post-install 校验通过)")


def _render_drs_node_install_conf(node_ip: str) -> str:
    """DRS-Node install.conf 字段。"""
    gauss_nodes = [_opt_conf(f"gaussdb_node{i}_ip") for i in (1, 2, 3)]
    db_port = _opt_conf("gaussdb_db_port", "30100")
    meta_db_address = ",".join(f"{ip}:{db_port}" for ip in gauss_nodes)
    return (
        "[meta db]\n"
        f"metaDB_engine = gaussdb\n"
        f"metaDB_address = {meta_db_address}\n"
        f"metaDB_drs_user = drs\n"
        f"metaDB_drs_password = {_conf('gaussdb_rdsReplPasswd')}\n"
        f"metaDB_database = drs\n"
        "\n"
        "[drs]\n"
        f"node_ip = {node_ip}\n"
        "\n"
        "[system]\n"
        # swap_space 进 heredoc 前过 _safe_for_remote,拒绝换行 + shell 元字符
        f"swap_space = {_safe_for_remote(_opt_conf('drs_node_swap_space', '2G'), 'drs_node_swap_space')}\n"
    )


# === 阶段 5:安装 Monitor-Agent(所有节点自动识别组件)===
def phase_monitor_agent() -> None:
    info("===== Phase 5: 安装 Monitor-Agent =====")
    targets: List[str] = [
        ip for ip in (
            *(_opt_conf(f"gaussdb_node{i}_ip") for i in (1, 2, 3)),
            _opt_conf("drs_service_primary_ip"),
            _opt_conf("drs_service_standby_ip") if _opt_conf("deploy_scene", "ha").lower() == "ha" else "",
        ) if ip
    ]
    if _opt_conf("drs_node_ips", ""):
        targets += _split_list(_opt_conf("drs_node_ips"))
    # 去重保持顺序
    seen: set = set()
    uniq_targets: List[str] = []
    for ip in targets:
        if ip not in seen:
            seen.add(ip)
            uniq_targets.append(ip)
    tar = _opt_conf("monitor_agent_tar", "")
    if not tar:
        fatal("monitor_agent_tar 未配置 | 修复:在 conf 中填 Monitor-Agent 安装包路径")
    failed: List[Tuple[str, str]] = []  # (ip, stderr_head)
    for ip in uniq_targets:
        info(f"--- Monitor-Agent [{ip}] ---")
        ssh_run(ip, "mkdir -p /root/package", check=False)
        scp_push(tar, ip, "/root/package/Monitor-Agent.tar.gz")
        # 校验解压后目录数恰好 1,避免嵌套子目录导致 `cd Monitor-Agent-*` 装错树
        rc, out, _ = ssh_run(ip,
                             "cd /root/package && rm -rf Monitor-Agent-* && "
                             "tar -xzf Monitor-Agent.tar.gz && "
                             "n=$(ls -d Monitor-Agent-* 2>/dev/null | wc -l) && "
                             "[[ $n == 1 ]] && ls -d Monitor-Agent-* || "
                             "{ echo \"expected 1 dir, got $n\"; exit 2; }",
                             check=False)
        if rc != 0:
            fatal(f"Monitor-Agent [{ip}] 解压后目录数异常 | {out.strip()[:300]}")
        # install.sh 会提示输入 db ip:port / drs user / drs password
        # 用 here-doc 喂入,避免交互卡住。若 install.sh 走 /dev/tty 而非 stdin,
        # 此处会失败 — 需手动到节点执行 cd /root/package/Monitor-Agent-* && sh install.sh
        db_port = _opt_conf("gaussdb_db_port", "30100")
        # 默认 ip:port 是 127.0.0.1:30100(本机),三副本节点要换成三 IP 列表
        # 这里保守取第一个 gaussdb 节点 IP(简化,生产可按节点角色定制)
        gauss_ip = _opt_conf("gaussdb_node1_ip")
        # 进入 install_script 外层 bash 体的字段必须先拒绝 shell 元字符
        # (heredoc 内部安全,但 <<'EOF' 之前的脚本本身仍会被解析)
        db_ip_port = f"{_safe_for_remote(gauss_ip, 'gaussdb_node1_ip')}:{_safe_for_remote(db_port, 'gaussdb_db_port')}"
        drs_pwd = _safe_for_remote(_conf("gaussdb_rdsReplPasswd"), "gaussdb_rdsReplPasswd")
        answers = f"{db_ip_port}\ndrs\n{drs_pwd}\n{drs_pwd}\n"
        # 必须用真 here-doc 把 answers 喂给 install.sh 的 stdin,而不是拼到 bash -s
        # 的脚本里当命令执行(那样会把密码当 shell 命令跑,会触发任意命令执行)
        install_script = (
            "cd /root/package/Monitor-Agent-* && "
            "sh install.sh <<'MON_AGENT_ANSWERS'\n"
            f"{answers}"
            "MON_AGENT_ANSWERS\n"
        )
        rc, out, err = ssh_run(ip, install_script, timeout=600, check=False)
        if rc != 0:
            # 合并 stdout + stderr 末尾,便于操作员定位半成功半失败的 install.sh
            tail = (out + "\n" + err).strip()[-500:]
            failed.append((ip, tail))
    if failed:
        listing = "\n".join(f"  - {ip}: {msg}" for ip, msg in failed)
        fatal(f"Monitor-Agent 安装失败节点({len(failed)}/{len(uniq_targets)}):\n{listing}\n"
              f"修复:逐节点手动执行 cd /root/package/Monitor-Agent-* && sh install.sh,"
              f"确认 answers(ip:port / drs / password)是否正确")
    info("Phase 5 完成:Monitor-Agent 安装成功")


# === 阶段 6:验证(TCP 端口探测 DRS-Service)===
def phase_verify() -> None:
    info("===== Phase 6: 验证安装结果 =====")
    if _conf_bool("verify_skip", False):
        info("verify_skip=true,跳过验证")
        return
    primary = _opt_conf("drs_service_primary_ip")
    port = _opt_conf("drs_service_external_port", "7443")
    # 仅检查 TCP 端口可达(更严格的 HTTPS 调用需要证书)
    info(f"--- TCP 探测 {primary}:{port} ---")
    rc, out, _ = ssh_run(primary,
                         f"(echo > /dev/tcp/{primary}/{port}) 2>&1 && echo PORT_OPEN || echo PORT_CLOSED",
                         check=False, timeout=10)
    info(f"[{primary}]:{port} -> {out.strip() or 'unknown'}")
    if "PORT_CLOSED" in out:
        # verify 阶段作为最后一道闸,失败必须 fatal — 否则 main() 返回 0,
        # CI / 编排器感知不到部署失败。
        fatal(f"DRS-Service 端口 {port} 在 {primary} 上未监听 | 部署未通过验证 "
              f"| 查看 [{primary}] install.log / drs-service 启动日志")
    info("Phase 6 完成(粗粒度验证,详细业务验证请登录 DRS 控制台)")


# === 入口与命令行 ===
ALL_PHASES = [
    "precheck", "gaussdb", "drs_service_primary", "drs_service_standby",
    "drs_node", "monitor_agent", "verify",
]

PHASE_FUNCS = {
    "precheck": phase_precheck,
    "gaussdb": phase_gaussdb,
    "drs_service_primary": phase_drs_service_primary,
    "drs_service_standby": phase_drs_service_standby,
    "drs_node": phase_drs_node,
    "monitor_agent": phase_monitor_agent,
    "verify": phase_verify,
}

# phase 依赖:跑一个阶段前,先自动跑完它的前置阶段(避免用户 --phase drs_node
# 跳过 gaussdb/install.sh 在 metaDB 不存在时神秘失败)。前置阶段只跑一次,
# 顺序遵循 ALL_PHASES。
# 注:drs_service_standby 仅依赖 gaussdb(而非 primary) — HA 重建场景可能需要
# 只重装备节点。但若 --phase drs_service_standby 时 primary 未装,phase 内
# 会检测 primary 可达性并 warn,提示操作员确认场景。
PHASE_PREREQS: Dict[str, List[str]] = {
    "drs_service_primary": ["gaussdb"],
    "drs_service_standby": ["gaussdb"],
    "drs_node": ["drs_service_primary"],
    "monitor_agent": ["drs_service_primary"],
    "verify": ["drs_service_primary"],
}


def _expand_with_prereqs(phases: List[str]) -> List[str]:
    """把 phases 列表按 PHASE_PREREREQS 自动补全前置阶段,保持 ALL_PHASES 顺序,去重。"""
    seen: set = set()
    ordered: List[str] = []
    for name in phases:
        for pre in PHASE_PREREQS.get(name, []) + [name]:
            if pre not in seen:
                seen.add(pre)
                ordered.append(pre)
    # 兜底:按 ALL_PHASES 顺序再排序,确保依赖在前
    return [p for p in ALL_PHASES if p in seen]


def main(argv: Optional[List[str]] = None) -> int:
    global _DEBUG_ENABLED
    p = argparse.ArgumentParser(
        description="自动化部署 DRS 高可用集群",
    )
    p.add_argument("conf", help="conf 文件路径(扁平 key = \"value\")")
    p.add_argument("--phase", choices=ALL_PHASES,
                   help="只跑指定阶段(默认跑 precheck + 全部安装阶段 + verify)")
    p.add_argument("--check-conf", action="store_true",
                   help="只校验 conf,不实际下发")
    p.add_argument("--dry-run", action="store_true",
                   help="打印计划但不执行(目前仅在 select 时打印)")
    p.add_argument("--debug", action="store_true",
                   help="开启 DEBUG 级日志")
    args = p.parse_args(argv)
    _DEBUG_ENABLED = args.debug

    load_conf(args.conf)
    debug(f"_CONF keys = {sorted(_CONF.keys())}")

    if args.check_conf:
        check_conf()
        return 0

    if args.dry_run:
        info(f"[DRY-RUN] 将执行阶段: {args.phase or ALL_PHASES}")
        return 0

    phases = [args.phase] if args.phase else ALL_PHASES
    # 按 PHASE_PREREQS 自动补全前置阶段 — 用户 --phase drs_node 时会先跑
    # precheck + gaussdb + drs_service_primary,避免 metaDB 缺失导致神秘失败
    expanded = _expand_with_prereqs(phases)
    # 任何非 --check-conf / --dry-run 入口都先做完整预检,避免 --phase gaussdb
    # 这种用法跳过校验直接操作远端导致集群留半残状态
    if expanded != ["precheck"]:
        phase_precheck()
    for name in expanded:
        PHASE_FUNCS[name]()
    info("===== DRS 部署全流程完成 =====")
    return 0


if __name__ == "__main__":
    sys.exit(main())