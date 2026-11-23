"""Check packaged source consistency and generate hashes after app/build.sh."""
import hashlib
import json
import plistlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "app/dist"
APP = DIST / "微信-企微连接器.app"
PAYLOAD = APP / "Contents/Resources/payload"
checked = 0

for base in (ROOT / "wechat-frontend", ROOT / "setup"):
    for path in base.rglob("*"):
        if not path.is_file() or any(x in path.parts for x in (".venv", "__pycache__", ".pytest_cache", "state")):
            continue
        if path.suffix == ".pyc" or path.name == ".DS_Store" or path == ROOT / "setup/wecom-keychain":
            continue
        relative = path.relative_to(ROOT)
        packaged = PAYLOAD / relative
        assert packaged.is_file(), f"Missing packaged source: {relative}"
        assert path.read_bytes() == packaged.read_bytes(), f"Packaged source differs: {relative}"
        checked += 1

assert (ROOT / "VERSION").read_text().strip() == (PAYLOAD / "VERSION").read_text().strip()
version = (ROOT / "VERSION").read_text().strip()
with (APP / "Contents/Info.plist").open("rb") as stream:
    info = plistlib.load(stream)
assert info["CFBundleName"] == info["CFBundleDisplayName"] == "微信/企微连接器"
assert info["CFBundleExecutable"] == "WeChatWeComConnector"
assert info["CFBundleIdentifier"] == "com.gly.wechatdesk"
assert info["CFBundleShortVersionString"] == version
artifacts = [DIST / "微信-企微连接器.dmg", DIST / f"WeChatWeComConnector-v{version}.dmg",
             APP / "Contents/MacOS/WeChatWeComConnector",
             PAYLOAD / "setup/wecom-keychain",
             PAYLOAD / "setup/wecom-runtime",
             PAYLOAD / "wechat-realtime-reader/bin/wechat-reader",
             PAYLOAD / "wechat-realtime-reader/bin/wecom-reader"]
lines = []
for path in artifacts:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    lines.append(digest.hexdigest() + "  " + str(path.relative_to(DIST)))
(DIST / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n")
print(json.dumps({"packaged_source_files_checked": checked,
                  "artifacts": [{"path": str(p.relative_to(DIST)), "bytes": p.stat().st_size} for p in artifacts]},
                 ensure_ascii=False, indent=2))
