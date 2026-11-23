#!/bin/bash
# Build 微信-企微连接器.app (+ .dmg). Needs Xcode Command Line Tools (swiftc).
# Embeds wechat-realtime-reader + wechat-frontend + setup/ into Contents/Resources/payload
# so a downloaded .app is self-contained (zero-config on first launch).
set -euo pipefail
cd "$(dirname "$0")"
APP_NAME="微信-企微连接器"
DISPLAY_NAME="微信/企微连接器"
EXECUTABLE="WeChatWeComConnector"
BUNDLE_ID="com.gly.wechatdesk"
VERSION="${WD_VERSION:-$(tr -d '\n' < ../VERSION)}"
BUILD="build"; DIST="dist"
APP="$DIST/$APP_NAME.app"
REPO="$(cd .. && pwd)"
PAYLOAD_SRC="$REPO"
rm -rf "$BUILD" "$APP" "$DIST/$APP_NAME.dmg" "$DIST/WeChatWeComConnector-v${VERSION}.dmg"
mkdir -p "$BUILD" "$DIST"

echo "==> compile"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
if command -v go >/dev/null 2>&1; then
  (cd "$REPO/wechat-realtime-reader" && go build -o bin/wechat-reader ./cmd/wechat-reader &&
    go build -o bin/wecom-reader ./cmd/wecom-reader)
fi
[[ -x "$REPO/wechat-realtime-reader/bin/wechat-reader" ]] || { echo "缺少读取器，请安装 Go 并构建"; exit 1; }
[[ -x "$REPO/wechat-realtime-reader/bin/wecom-reader" ]] || { echo "缺少企业微信读取器，请安装 Go 并构建"; exit 1; }
swiftc -O -target arm64-apple-macos13.0 -framework Cocoa -framework WebKit src/main.swift -o "$BUILD/$EXECUTABLE"
swiftc -O -target arm64-apple-macos13.0 -framework Security "$REPO/setup/wecom-keychain.swift" -o "$REPO/setup/wecom-keychain"
swiftc -O -target arm64-apple-macos13.0 "$REPO/setup/wecom-runtime.swift" -o "$REPO/setup/wecom-runtime"

echo "==> icon"
swiftc -O src/makeicon.swift -o "$BUILD/makeicon"
"$BUILD/makeicon" "$BUILD/AppIcon.iconset"
iconutil -c icns "$BUILD/AppIcon.iconset" -o "$BUILD/AppIcon.icns"

echo "==> bundle skeleton"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BUILD/$EXECUTABLE" "$APP/Contents/MacOS/$EXECUTABLE"
cp "$BUILD/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>$DISPLAY_NAME</string>
  <key>CFBundleDisplayName</key><string>$DISPLAY_NAME</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleExecutable</key><string>$EXECUTABLE</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$(date +%Y%m%d%H%M)</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSPrincipalClass</key><string>NSApplication</string>
  <key>NSHumanReadableCopyright</key><string>微信/企微连接器 · 本地只读消息分析</string>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
PLIST

echo "==> frida-inject (official 17.25.2, sha256-verified; bundled so key capture needs no pip/network)"
FRIDA_VENDOR="$PAYLOAD_SRC/wechat-realtime-reader/skills/wechat-db-keys/vendor/frida"
if [[ -n "${WD_FRIDA_XZ_DIR:-}" ]]; then
  python3 "$FRIDA_VENDOR/fetch.py" --arch all --from "$WD_FRIDA_XZ_DIR"
else
  python3 "$FRIDA_VENDOR/fetch.py" --arch all
fi
for a in arm64 x86_64; do [[ -x "$FRIDA_VENDOR/frida-inject-$a" ]] || { echo "missing frida-inject-$a"; exit 1; }; done

echo "==> embed payload (reader + frontend + setup)"
P="$APP/Contents/Resources/payload"
mkdir -p "$P"
echo "$VERSION" > "$P/VERSION"
# reader: binary + skills (for key capture) + go.mod (for module path awareness)
mkdir -p "$P/wechat-realtime-reader/bin" "$P/wechat-realtime-reader/skills"
cp "$PAYLOAD_SRC/wechat-realtime-reader/bin/wechat-reader" "$P/wechat-realtime-reader/bin/"
cp "$PAYLOAD_SRC/wechat-realtime-reader/bin/wecom-reader" "$P/wechat-realtime-reader/bin/"
chmod +x "$P/wechat-realtime-reader/bin/wechat-reader"
chmod +x "$P/wechat-realtime-reader/bin/wecom-reader"
rsync -a --delete --exclude '__pycache__' --exclude '*.part' --exclude '*.tmp' "$PAYLOAD_SRC/wechat-realtime-reader/skills/" "$P/wechat-realtime-reader/skills/"
xattr -cr "$P/wechat-realtime-reader/skills/wechat-db-keys/vendor/frida" 2>/dev/null || true
# frontend: python sources + scripts + requirements (venv is created on first launch)
mkdir -p "$P/wechat-frontend"
rsync -a --delete \
  --exclude '.venv' --exclude '__pycache__' --exclude '*.pyc' --exclude 'state' \
  "$PAYLOAD_SRC/wechat-frontend/" "$P/wechat-frontend/"
chmod +x "$P/wechat-frontend/scripts/"*.sh
# setup scripts
mkdir -p "$P/setup"
cp "$PAYLOAD_SRC/setup/"*.sh "$P/setup/"
cp "$PAYLOAD_SRC/setup/"*.py "$PAYLOAD_SRC/setup/wecom-keychain" "$PAYLOAD_SRC/setup/wecom-keychain.swift" "$P/setup/"
cp "$PAYLOAD_SRC/setup/wecom-runtime" "$PAYLOAD_SRC/setup/wecom-runtime.swift" "$P/setup/"
chmod +x "$P/setup/"*.sh

echo "==> sign (ad-hoc)"
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP"

echo "==> dmg"
STAGE="$BUILD/dmg"; mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"; ln -s /Applications "$STAGE/Applications"
hdiutil create -quiet -volname "$APP_NAME" -srcfolder "$STAGE" -ov -format UDZO "$DIST/$APP_NAME.dmg"
# ASCII-named release artifact
cp "$DIST/$APP_NAME.dmg" "$DIST/WeChatWeComConnector-v${VERSION}.dmg"
echo "==> done: $APP"
echo "    $DIST/$APP_NAME.dmg"
echo "    $DIST/WeChatWeComConnector-v${VERSION}.dmg"
