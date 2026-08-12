#!/usr/bin/env bash
# Build the Android shell (ADR-0038).
#
#   app/android/build.sh              debug APK (side-loadable, debug-signed)
#   app/android/build.sh release      release APK (needs instance/android-release.keystore — see
#                                     docs/app/ANDROID.md; key material is NOT in git)
#
# The pinned certificate is refreshed from instance/tls/server.crt on every build, so the APK can never
# be built against a stale trust anchor by accident. tests/test_android_cert_pin.py is the backstop for
# the case where someone commits without building.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
VARIANT="${1:-debug}"

export JAVA_HOME="${JAVA_HOME:-/usr/lib/jvm/java-21-openjdk-amd64}"
export ANDROID_HOME="${ANDROID_HOME:-$HOME/android-sdk}"
export ANDROID_SDK_ROOT="$ANDROID_HOME"

SRC_CRT="$REPO_ROOT/instance/tls/server.crt"
DST_CRT="$HERE/app/src/main/res/raw/ha_server.crt"

if [ -f "$SRC_CRT" ]; then
  if ! cmp -s "$SRC_CRT" "$DST_CRT"; then
    echo "== pinned certificate CHANGED — refreshing from instance/tls/server.crt"
    echo "   NOTE: every already-installed phone must be updated, or it will refuse to connect."
    cp "$SRC_CRT" "$DST_CRT"
  fi
  openssl x509 -in "$DST_CRT" -noout -subject -enddate | sed 's/^/   pin: /'
else
  echo "== WARNING: $SRC_CRT not on this box — building against the committed cert copy."
fi

cd "$HERE"
case "$VARIANT" in
  debug)
    ./gradlew :app:testDebugUnitTest :app:assembleDebug --no-daemon
    APK="app/build/outputs/apk/debug/app-debug.apk"
    ;;
  release)
    if [ ! -f "$REPO_ROOT/instance/android-release.keystore" ]; then
      echo "ERROR: instance/android-release.keystore missing. See docs/app/ANDROID.md (Signing)." >&2
      exit 1
    fi
    ./gradlew :app:testReleaseUnitTest :app:assembleRelease --no-daemon
    APK="app/build/outputs/apk/release/app-release.apk"
    ;;
  *)
    echo "usage: build.sh [debug|release]" >&2; exit 2 ;;
esac

echo
echo "== built: $HERE/$APK"
ls -lh "$APK" | awk '{print "   size: " $5}'
echo "   sha256: $(sha256sum "$APK" | cut -d' ' -f1)"
