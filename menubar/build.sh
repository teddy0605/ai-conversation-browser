#!/bin/bash
# Build the Convos menu bar app and install it to ~/Applications/Convos.app.
# The app points at the repo folder that holds this script.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
project="$(dirname "$here")"
app="$HOME/Applications/Convos.app"

pkill -x Convos 2>/dev/null || true
rm -rf "$app"
mkdir -p "$app/Contents/MacOS"

swiftc -O -o "$app/Contents/MacOS/Convos" "$here/Convos.swift"

cat > "$app/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleExecutable</key><string>Convos</string>
	<key>CFBundleIdentifier</key><string>local.ai-conversation-browser.convos</string>
	<key>CFBundleName</key><string>Convos</string>
	<key>CFBundlePackageType</key><string>APPL</string>
	<key>CFBundleShortVersionString</key><string>1.0</string>
	<key>LSMinimumSystemVersion</key><string>13.0</string>
	<key>LSUIElement</key><true/>
	<key>NSAppTransportSecurity</key>
	<dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict>
</plist>
EOF
plutil -insert ACBProjectDir -string "$project" "$app/Contents/Info.plist"

codesign --force --sign - "$app"
open "$app"
echo "Installed $app (project: $project)"
