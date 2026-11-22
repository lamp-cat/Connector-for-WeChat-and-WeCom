"""Common macOS runtime setup. Existing source settings and key files are retained."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import plistlib
import secrets
import subprocess
import sys


def step(name, state, detail):
    print(f"@@step\t{name}\t{state}\t{detail}", flush=True)


def run(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def main():
    if sys.platform != "darwin":
        raise RuntimeError("只支持 macOS")
    os.umask(0o077)
    user_dir = Path(os.environ.get("WD_HOME", Path.home()))
    payload = Path(os.environ.get("WD_PAYLOAD", Path(__file__).resolve().parents[1]))
    managed = user_dir / ".local/share/wechatdesk/runtime"
    candidate = Path(os.environ.get("WD_ROOT", managed))
    root = candidate if (candidate / "wechat-frontend/app/sources/registry.py").is_file() else managed
    step("runtime", "run", "准备个人微信 / 企业微信共用运行时")
    version = (payload / "VERSION").read_text().strip()
    installed = (root / "VERSION").read_text().strip() if (root / "VERSION").exists() else ""
    updated = payload.resolve() != root.resolve() and installed != version
    if payload.resolve() != root.resolve() and (updated or not (root / "setup/wecom-keychain").is_file()):
        root.mkdir(parents=True, exist_ok=True)
        for name in ("wechat-realtime-reader", "wechat-frontend", "setup"):
            (root / name).mkdir(exist_ok=True)
            run(["/usr/bin/rsync", "-a", "--delete", "--exclude=.venv", "--exclude=__pycache__",
                 "--exclude=*.pyc", "--exclude=state", str(payload / name) + "/", str(root / name) + "/"])
        (root / "VERSION").write_text(version + "\n")
    if not (root / "setup/wecom-keychain").is_file():
        run(["/usr/bin/swiftc", "-O", "-target", "arm64-apple-macos13.0", "-framework", "Security", str(root / "setup/wecom-keychain.swift"),
             "-o", str(root / "setup/wecom-keychain")])
    print(f"@@root\t{root}", flush=True)
    step("runtime", "ok", f"共用运行时 {version} 已就绪")
    frontend = root / "wechat-frontend"
    venv = frontend / ".venv"
    python = venv / "bin/python"
    req_hash = hashlib.sha256((frontend / "requirements.txt").read_bytes()).hexdigest()
    marker = venv / ".wd-req"
    step("python", "run", "准备网关与企业快照依赖")
    if not python.is_file():
        run([sys.executable, "-m", "venv", str(venv)])
    if not marker.is_file() or marker.read_text().strip() != req_hash:
        logs = user_dir / "Library/Logs/WeChatDesk"
        logs.mkdir(parents=True, exist_ok=True)
        with (logs / "dependency-install.log").open("w") as log:
            run([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                 "--progress-bar", "off", "-r", str(frontend / "requirements.txt")], stdout=log, stderr=log)
        run([str(python), "-c", "import fastapi, uvicorn, httpx, Crypto, frida"])
        marker.write_text(req_hash + "\n")
    step("python", "ok", "公共环境已就绪；个人微信无需预先登录")
    config = user_dir / ".config/wechat-frontend"
    config.mkdir(mode=0o700, parents=True, exist_ok=True)
    config_path = config / "config.json"
    if not config_path.exists():
        config_path.write_text(json.dumps({"bind_host": "127.0.0.1", "bind_port": 8088,
            "reader_base": "http://127.0.0.1:5678", "cors_origins": []}, indent=2))
    cfg = json.loads(config_path.read_text())
    if cfg.get("bind_host", "127.0.0.1") not in ("127.0.0.1", "localhost", "::1"):
        raise RuntimeError("统一版只支持本机回环监听，请将 bind_host 改为 127.0.0.1")
    token = config / "gateway-token"
    if not token.is_file() or not token.read_text().strip():
        token.write_text(secrets.token_urlsafe(32))
    for path in (config_path, token):
        path.chmod(0o600)
    agents = user_dir / "Library/LaunchAgents"
    logs = user_dir / "Library/Logs/WeChatDesk"
    agents.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    uid = str(os.getuid())

    def service(label, arguments):
        path = agents / (label + ".plist")
        desired = plistlib.dumps({
            "Label": label, "ProgramArguments": arguments, "RunAtLoad": True, "KeepAlive": True,
            "ThrottleInterval": 10, "Umask": 63,
            "StandardOutPath": str(logs / (label + ".log")),
            "StandardErrorPath": str(logs / (label + ".log")),
            "EnvironmentVariables": {"HOME": str(user_dir), "WF_PLATFORM_ROOT": str(root),
                "WECHAT_READER_CONFIG": str(user_dir / ".config/wechat-realtime-reader")}})
        changed = not path.exists() or path.read_bytes() != desired
        if changed and path.exists():
            backup = config / "launchd-backups"
            backup.mkdir(mode=0o700, exist_ok=True)
            (backup / (label + "-" + secrets.token_hex(4) + ".plist")).write_bytes(path.read_bytes())
        if changed:
            path.write_bytes(desired)
        domain = f"gui/{uid}/{label}"
        loaded = subprocess.run(["/bin/launchctl", "print", domain], capture_output=True).returncode == 0
        if loaded and changed:
            subprocess.run(["/bin/launchctl", "bootout", domain], capture_output=True)
            loaded = False
        if not loaded:
            run(["/bin/launchctl", "bootstrap", f"gui/{uid}", str(path)], capture_output=True)
        elif updated:
            run(["/bin/launchctl", "kickstart", "-k", domain], capture_output=True)

    service("com.gly.wechatfrontend", [str(frontend / "scripts/start.sh")])
    step("services", "ok", "公共网关已启动；企业微信读取进程按需启动")
    personal = user_dir / ".config/wechat-realtime-reader"
    reader = root / "wechat-realtime-reader/bin/wechat-reader"
    if reader.is_file() and (personal / "source-config.json").is_file() and (personal / "keys.json").is_file():
        service("com.gly.wechatreader.serve", [str(reader), "serve", "-addr", "127.0.0.1:5678"])
        step("personal", "ok", "已恢复个人微信已有读取配置")
    else:
        step("personal", "skip", "个人微信尚未连接；可在设置页选择个人微信后初始化")
    print("@@done\tok", flush=True)
    if os.environ.get("WD_SETUP_PERSONAL") == "1":
        env = dict(os.environ, WD_ROOT=str(root), WD_SKIP_AGENTS=os.environ.get("WD_SKIP_AGENTS", "1"))
        os.execve("/bin/bash", ["/bin/bash", str(root / "setup/personal-bootstrap.sh")], env)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        step("common", "fail", str(exc) if isinstance(exc, RuntimeError) else
             f"公共初始化失败（{type(exc).__name__}），请检查 Library/Logs/WeChatDesk 日志")
        print("@@done\tfail", flush=True)
        sys.exit(1)
