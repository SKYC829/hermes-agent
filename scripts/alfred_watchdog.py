#!/usr/bin/env python3
"""
Alfred 看门狗 v4 — 智能告警系统
核心改进：
- 状态跟踪：只在告警变化时通知（新增/恢复）
- PVE 备份监控：检查 vzdump 任务成败
- 多渠道推送：飞书、钉钉、邮件
- 日志精简：变化日志 + 全量状态文件
"""

import json
import subprocess
import socket
import ssl
import sys
import time
import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

ALFRED_DIR = Path("/opt/orientalgames/obsidian-vault/alfred")
SELF_MODEL_PATH = ALFRED_DIR / "self-model.json"
REGISTRY_PATH = Path("/opt/orientalgames/obsidian-vault/notes/service-registry.yaml")
STATE_PATH = ALFRED_DIR / "logs" / "watchdog_state.json"
LOG_PATH = ALFRED_DIR / "logs" / "watchdog.jsonl"
CHANGE_LOG_PATH = ALFRED_DIR / "logs" / "watchdog_changes.jsonl"

PVE_HOST = "192.168.0.100"
PVE_USER = "root"
PVE_PASS = "ZyfPassw0rd!"
VM_PASS = "SecretPassw0rd!"

# ─── State management ───

def load_state():
    """加载上次的告警状态"""
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except:
            pass
    return {
        "active_alerts": {},      # {alert_key: alert_text}
        "alert_counts": {},       # {alert_key: count} - 每个告警的触发次数
        "suppressed_alerts": {},  # {alert_key: alert_text} - 被抑制的告警（已知问题）
        "last_check": None,
        "last_change": None,
        "last_notification": None,
        "consecutive_same": 0,    # 连续相同告警次数
        "backup_last_ok": None,   # 最后一次备份成功的 ISO 时间
    }


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def alert_key(alert_text):
    """生成告警的唯一 key（去掉变化的部分如数字）"""
    return hashlib.md5(alert_text.encode()).hexdigest()[:16]


# ─── Registry loader ───

def load_registry():
    if not yaml or not REGISTRY_PATH.exists():
        return {"thresholds": {}, "vms": {}}
    with open(REGISTRY_PATH) as f:
        return yaml.safe_load(f) or {"thresholds": {}, "vms": {}}


# ─── PVE helpers ───

def ssh_cmd(host, cmd, password=None, timeout=10, user="guest", retries=2):
    """SSH command with retry logic for transient failures"""
    pw = password or VM_PASS
    for attempt in range(retries):
        try:
            r = subprocess.run(
                ["sshpass", "-p", pw, "ssh", "-o", "StrictHostKeyChecking=no",
                 "-o", "ConnectTimeout=8", f"{user}@{host}", cmd],
                capture_output=True, text=True, timeout=timeout
            )
            if r.returncode == 0:
                return r.stdout.strip()
            # Non-zero exit might be transient, retry
            if attempt < retries - 1:
                time.sleep(1)
                continue
            return None
        except subprocess.TimeoutExpired:
            if attempt < retries - 1:
                time.sleep(1)
                continue
            return None
        except Exception:
            return None
    return None


def pve_ssh(cmd, timeout=10):
    return ssh_cmd(PVE_HOST, cmd, PVE_PASS, timeout, "root")


def get_all_vms():
    vms = []
    out = pve_ssh("pvesh get /nodes/$(hostname)/qemu --output-format json 2>/dev/null")
    if not out:
        return vms
    try:
        data = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return vms
    for vm in data:
        vms.append({
            "vmid": str(vm.get("vmid", "")),
            "name": vm.get("name", ""),
            "status": vm.get("status", ""),
            "mem_mb": vm.get("maxmem", 0) // 1048576,
            "disk_gb": round(vm.get("maxdisk", 0) / 1073741824, 2),
        })
    return vms


def get_backup_vmids():
    out = pve_ssh("cat /etc/pve/jobs.cfg 2>/dev/null")
    if not out:
        return set(), {}
    vmids = set()
    schedule = ""
    for line in out.split("\n"):
        line = line.strip()
        if line.startswith("vmid"):
            parts = line.split(None, 1)
            if len(parts) == 2:
                vmids = set(v.strip() for v in parts[1].split(","))
        if line.startswith("schedule"):
            schedule = line.split(None, 1)[1] if len(line.split(None, 1)) > 1 else ""
    return vmids, {"schedule": schedule}


def check_backup_job_status():
    """检查最近 24 小时的 vzdump 备份任务状态"""
    results = []
    out = pve_ssh(
        "journalctl -u pvescheduler --since '24 hours ago' --no-pager 2>/dev/null | "
        "grep -E '(starting new backup job|Finished Backup|ERROR|WARN|Backup job finished|failed)'"
    )
    if not out:
        return results

    current_job = {}
    for line in out.split("\n"):
        line = line.strip()
        if "starting new backup job" in line.lower():
            if current_job.get("vms"):
                results.append(current_job)
            # Extract VMIDs from the command
            try:
                cmd_part = line.split("vzdump")[1].split("--")[0].strip()
                vmids = cmd_part.split()
            except:
                vmids = []
            current_job = {
                "start_time": line[:19] if len(line) > 19 else "",
                "vms": vmids,
                "vm_status": {},
                "finished": False,
                "success": False
            }
        elif "Starting Backup of VM" in line:
            try:
                vmid = line.split("VM")[1].split("(")[0].strip()
                current_job.setdefault("vm_status", {})[vmid] = "running"
            except:
                pass
        elif "Finished Backup of VM" in line:
            try:
                vmid = line.split("VM")[1].split("(")[0].strip()
                if "vm_status" in current_job:
                    current_job["vm_status"][vmid] = "ok"
            except:
                pass
        elif "Backup job finished successfully" in line:
            current_job["finished"] = True
            current_job["success"] = True
        elif "ERROR" in line or "failed" in line.lower():
            current_job.setdefault("errors", []).append(line)

    if current_job.get("vms"):
        results.append(current_job)

    return results


def get_pbs_backup_freshness(max_age_hours=24):
    """Get backup freshness from PBS. Returns dict or None on SSH failure."""
    pbs_host = "192.168.0.101"
    cmd = (
        "for d in /backup/vm/*/; do "
        "vmid=$(basename $d); "
        "latest=$(ls -td $d*/ 2>/dev/null | head -1); "
        "if [ -n \"$latest\" ]; then "
        "age=$(( ($(date +%s) - $(stat -c %Y \"$latest\")) / 3600 )); "
        "echo \"$vmid:$age\"; "
        "fi; "
        "done"
    )
    out = ssh_cmd(pbs_host, cmd, PVE_PASS, timeout=20, user="root", retries=3)
    if out is None:
        # SSH failed - distinguish from "no backups" (empty string)
        return None  # SSH failure, not "no backups"
    if not out:
        return {}  # SSH succeeded but no output = no backups
    result = {}
    for line in out.strip().split("\n"):
        if ":" in line:
            parts = line.split(":", 1)
            try:
                result[parts[0].strip()] = int(parts[1].strip())
            except ValueError:
                pass
    return result


def get_storage_status():
    storage = []
    out = pve_ssh("pvesm status 2>/dev/null | tail -n +2")
    if not out:
        return storage
    for line in out.split("\n"):
        parts = line.split()
        if len(parts) >= 7:
            storage.append({
                "name": parts[0], "status": parts[2],
                "total_gb": int(parts[3]) / 1024 / 1024,
                "used_gb": int(parts[4]) / 1024 / 1024,
                "pct": float(parts[6].rstrip("%"))
            })
    return storage


# ─── System checks ───

def check_vm_system(host):
    cmd = (
        "echo LOAD:$(cat /proc/loadavg | awk '{print $1}') "
        "MEM_TOTAL:$(free -m | awk '/Mem:/{print $2}') "
        "MEM_USED:$(free -m | awk '/Mem:/{print $3}') "
        "MEM_AVAIL:$(free -m | awk '/Mem:/{print $7}') "
        "DISK_PCT:$(df -h / | awk 'NR==2{print $5}' | tr -d '%') "
        "DISK_AVAIL:$(df -h / | awk 'NR==2{print $4}')"
    )
    out = ssh_cmd(host, cmd)
    if not out:
        return None
    result = {}
    for part in out.split():
        if ":" in part:
            k, v = part.split(":", 1)
            try:
                result[k] = float(v)
            except:
                result[k] = v
    return result


def check_ssl_cert(host, port=443):
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((host, port), timeout=5) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert(binary_form=True)
                import cryptography.x509
                x509 = cryptography.x509.load_der_x509_certificate(cert)
                expiry = x509.not_valid_after_utc
                days_left = (expiry - datetime.now(timezone.utc)).days
                return {"host": host, "days_left": days_left, "expiry": expiry.isoformat()}
    except:
        return None


def check_service_tcp(host, port, timeout=3):
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        return True
    except:
        return False


def check_service_http(url, expect=200, timeout=5):
    try:
        r = subprocess.run(
            ["curl", "-sI", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", str(timeout), url],
            capture_output=True, text=True, timeout=timeout + 2
        )
        return int(r.stdout.strip()) == expect
    except:
        return False


# ─── Main logic ───

def run_checks():
    """运行所有检查，返回 {key: text} 的告警字典"""
    registry = load_registry()
    thresholds = registry.get("thresholds", {})
    reg_vms = registry.get("vms", {})

    t_load = thresholds.get("load_1m", 4.0)
    t_mem = thresholds.get("memory_pct", 90)
    t_disk = thresholds.get("disk_pct", 85)
    t_ssl = thresholds.get("ssl_warn_days", 30)

    alerts = {}  # key -> text

    # 1. PVE VM 列表
    all_vms = get_all_vms()
    if not all_vms:
        alerts["pve-unreachable"] = "❌ 无法连接 PVE 获取 VM 列表"
        return alerts, {}

    # 2. 备份检查 — 覆盖检查
    backed_up, backup_info = get_backup_vmids()
    # 用户主动忽略的 VM（不需要备份）
    backup_excluded = {"104", "106", "109", "112"}
    running = [v for v in all_vms if v["status"] == "running"]
    for vm in running:
        vmid = vm["vmid"]
        if int(vmid) >= 999999990:
            continue
        if vmid in backup_excluded:
            continue
        if vmid not in backed_up:
            alerts[f"no-backup-{vmid}"] = f"⚠️ VM{vmid} ({vm['name']}) 运行中但未纳入备份"

    # 2b. 备份任务状态检查
    backup_jobs = check_backup_job_status()
    for job in backup_jobs:
        if job.get("finished") and not job.get("success"):
            errors = "; ".join(job.get("errors", ["未知错误"]))
            alerts["backup-job-failed"] = f"❌ 备份任务失败: {errors[:200]}"
        if not job.get("finished") and job.get("vms"):
            # 任务还在运行中（正常情况下 03:00 开始，03:10 前结束）
            now_hour = datetime.now(timezone(timedelta(hours=8))).hour
            if now_hour > 4:  # 凌晨 4 点以后还没结束就不正常
                alerts["backup-job-stuck"] = f"⚠️ 备份任务似乎未完成（开始于 {job.get('start_time', '?')}）"

    # 2c. PBS 备份新鲜度
    backup_freshness = get_pbs_backup_freshness()
    if backup_freshness is None:
        # SSH to PBS failed - report this as a warning, not as "no backups"
        alerts["pbs-unreachable"] = "⚠️ 无法连接 PBS (192.168.0.101) 检查备份状态"
    else:
        for vmid in backed_up:
            vm = next((v for v in all_vms if v["vmid"] == vmid), None)
            if not vm or vm["status"] != "running":
                continue
            if vmid in backup_freshness:
                age_hours = backup_freshness[vmid]
                if age_hours > 26:
                    alerts[f"backup-stale-{vmid}"] = f"⚠️ VM{vmid} ({vm['name']}) 最近备份在 {age_hours}h 前（可能失败）"
            elif vmid in backed_up and vmid not in backup_freshness:
                alerts[f"backup-missing-{vmid}"] = f"⚠️ VM{vmid} ({vm['name']}) 在备份任务中但 PBS 无备份记录"

    # 3. 存储检查
    for s in get_storage_status():
        if s["pct"] > t_disk:
            alerts[f"storage-{s['name']}"] = f"⚠️ 存储 {s['name']} 使用率 {s['pct']:.0f}%（阈值 {t_disk}%）"

    # 4. 逐 VM 检查
    for vmid, reg in reg_vms.items():
        vmid_str = str(vmid)
        host = reg.get("host")
        if not host:
            continue

        pve_vm = next((v for v in all_vms if v["vmid"] == vmid_str), None)
        if not pve_vm:
            continue

        # VM 不在运行状态
        if pve_vm["status"] != "running":
            alerts[f"vm-stopped-{vmid}"] = f"🔴 VM{vmid} ({reg['name']}) 已停止"
            continue

        checks = reg.get("checks", [])

        # 系统负载
        if any(c in checks for c in ["load", "memory", "disk"]):
            sysinfo = check_vm_system(host)
            if sysinfo is None:
                alerts[f"ssh-unreachable-{vmid}"] = f"❌ VM{vmid} ({reg['name']}) SSH 不可达"
                continue

            load = sysinfo.get("LOAD", 0)
            if "load" in checks and load > t_load:
                alerts[f"high-load-{vmid}"] = f"⚠️ VM{vmid} ({reg['name']}) 负载 {load}（阈值 {t_load}）"

            mem_total = sysinfo.get("MEM_TOTAL", 0)
            mem_used = sysinfo.get("MEM_USED", 0)
            if "memory" in checks and mem_total > 0:
                mem_pct = mem_used / mem_total * 100
                if mem_pct > t_mem:
                    alerts[f"high-mem-{vmid}"] = f"⚠️ VM{vmid} ({reg['name']}) 内存 {mem_pct:.0f}%（阈值 {t_mem}%）"

            disk_pct = sysinfo.get("DISK_PCT", 0)
            if "disk" in checks and disk_pct > t_disk:
                alerts[f"high-disk-{vmid}"] = f"⚠️ VM{vmid} ({reg['name']}) 磁盘 {disk_pct:.0f}%（阈值 {t_disk}%）"

        # SSL 证书
        for ssl_entry in reg.get("ssl", []):
            ssl_host = ssl_entry.get("host", "")
            ssl_port = ssl_entry.get("port", 443)
            result = check_ssl_cert(ssl_host, ssl_port)
            if result is None:
                alerts[f"ssl-err-{ssl_host}"] = f"⚠️ SSL {ssl_host}:{ssl_port} 无法检查"
            elif result["days_left"] < t_ssl:
                alerts[f"ssl-expire-{ssl_host}"] = f"⚠️ SSL {ssl_host} 将在 {result['days_left']} 天后过期"

        # 服务检查
        for svc in reg.get("services", []):
            svc_name = svc.get("name", "unknown")
            svc_type = svc.get("type", "")
            critical = svc.get("critical", False)
            prefix = "❌" if critical else "⚠️"

            if svc_type == "tcp":
                target = svc.get("target", "")
                if ":" in target:
                    h, p = target.rsplit(":", 1)
                    if not check_service_tcp(h, int(p)):
                        alerts[f"svc-tcp-{vmid}-{svc_name}"] = f"{prefix} VM{vmid} 服务 {svc_name} TCP 不可达"

            elif svc_type == "http":
                url = svc.get("target", "")
                expect = svc.get("expect", 200)
                if not check_service_http(url, expect):
                    alerts[f"svc-http-{vmid}-{svc_name}"] = f"{prefix} VM{vmid} 服务 {svc_name} HTTP 异常"

            elif svc_type == "process":
                proc = svc.get("process", "")
                out = ssh_cmd(host, f"pgrep -x {proc} > /dev/null && echo OK || echo FAIL")
                if out and "FAIL" in out:
                    alerts[f"svc-proc-{vmid}-{svc_name}"] = f"{prefix} VM{vmid} 服务 {svc_name} 进程未运行"

    # 构建系统状态快照
    system_snapshot = {
        "vm_count": len(all_vms),
        "running_count": len(running),
        "backup_schedule": backup_info.get("schedule", "?"),
        "backed_up_count": len(backed_up),
        "storage": [{"name": s["name"], "pct": round(s["pct"], 1)} for s in get_storage_status()],
    }

    return alerts, system_snapshot


def format_changes(new_alerts, resolved_alerts):
    """格式化变化通知（Markdown）"""
    lines = []
    if new_alerts:
        lines.append("**🔴 新增告警：**")
        for key, text in new_alerts.items():
            lines.append(f"- {text}")
    if resolved_alerts:
        lines.append("**✅ 已恢复：**")
        for key, text in resolved_alerts.items():
            lines.append(f"- ~~{text}~~")
    return "\n".join(lines)


def main():
    now = datetime.now(timezone.utc)
    state = load_state()

    # 运行检查
    current_alerts, snapshot = run_checks()

    # 告警退化检测：更新每个告警的触发计数
    alert_counts = state.get("alert_counts", {})
    suppressed = state.get("suppressed_alerts", {})
    now_iso = now.isoformat()
    
    for key in current_alerts:
        alert_counts[key] = alert_counts.get(key, 0) + 1
    
    # 超过阈值的告警移到抑制列表（已知问题，减少噪音）
    SUPPRESS_THRESHOLD = 10
    newly_suppressed = {}
    for key, count in list(alert_counts.items()):
        if count > SUPPRESS_THRESHOLD and key in current_alerts and key not in suppressed:
            suppressed[key] = current_alerts[key]
            newly_suppressed[key] = current_alerts[key]
            del current_alerts[key]
    
    # 被抑制的告警如果恢复，也要通知
    suppressed_resolved = {}
    for key in list(suppressed.keys()):
        if key not in alert_counts or alert_counts[key] == 0:
            suppressed_resolved[key] = suppressed[key]
            del suppressed[key]
            if key in alert_counts:
                del alert_counts[key]
    
    state["alert_counts"] = alert_counts
    state["suppressed_alerts"] = suppressed

    # 计算变化
    prev_alerts = state.get("active_alerts", {})
    new_alerts = {k: v for k, v in current_alerts.items() if k not in prev_alerts}
    resolved_alerts = {k: v for k, v in prev_alerts.items() if k not in current_alerts}

    # 更新状态
    state["active_alerts"] = current_alerts
    state["last_check"] = now_iso

    if new_alerts or resolved_alerts or newly_suppressed or suppressed_resolved:
        state["last_change"] = now_iso
        state["consecutive_same"] = 0

        # 写变化日志
        change_entry = {
            "timestamp": now_iso,
            "new": new_alerts,
            "resolved": resolved_alerts,
            "suppressed": newly_suppressed,
            "suppressed_resolved": suppressed_resolved,
            "total_active": len(current_alerts),
            "total_suppressed": len(suppressed),
        }
        CHANGE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CHANGE_LOG_PATH, "a") as f:
            f.write(json.dumps(change_entry, ensure_ascii=False) + "\n")

        # 发送变化通知
        from alfred_webhook import notify as webhook_notify

        title = f"⚠️ 告警变化：+{len(new_alerts)} -{len(resolved_alerts)}"
        content_md = format_changes(new_alerts, resolved_alerts)
        
        if newly_suppressed:
            content_md += f"\n\n**🔇 已抑制（已知问题）：{len(newly_suppressed)} 项**"
            for k, v in newly_suppressed.items():
                content_md += f"\n- {v}"
        
        if suppressed_resolved:
            content_md += f"\n\n**🔊 抑制解除（状态变化）：{len(suppressed_resolved)} 项**"
            for k, v in suppressed_resolved.items():
                content_md += f"\n- {v}"
        
        if current_alerts:
            content_md += f"\n\n**当前活跃告警：{len(current_alerts)} 项**"
        if suppressed:
            content_md += f"\n**已知问题（已抑制）：{len(suppressed)} 项**"

        webhook_notify(title, content_md)
        state["last_notification"] = now_iso

        print(f"🔄 告警变化：新增 {len(new_alerts)}，恢复 {len(resolved_alerts)}，抑制 {len(newly_suppressed)}，解除 {len(suppressed_resolved)}，当前 {len(current_alerts)} 项")
    else:
        state["consecutive_same"] = state.get("consecutive_same", 0) + 1
        # 每小时输出一次状态（不发通知）
        if state["consecutive_same"] % 12 == 1:
            print(f"✅ 无变化（第 {state['consecutive_same']} 次检查，{len(current_alerts)} 项活跃告警，{len(suppressed)} 项已知问题）")

    # 写全量日志（每小时一次，不再每次 5 分钟都写）
    if state["consecutive_same"] % 12 == 0:  # 每小时
        log_entry = {
            "timestamp": now.isoformat(),
            "alert_count": len(current_alerts),
            "alerts": list(current_alerts.values()),
            "snapshot": snapshot,
        }
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

    # 更新 self-model 的 worries
    try:
        model = json.loads(SELF_MODEL_PATH.read_text())
        model["attention"]["worries"] = list(current_alerts.values())[:5]
        model["current_state"]["last_interaction"] = now.isoformat()
        SELF_MODEL_PATH.write_text(json.dumps(model, indent=2, ensure_ascii=False))
    except:
        pass

    save_state(state)


if __name__ == "__main__":
    main()
