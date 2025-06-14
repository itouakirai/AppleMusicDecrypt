#!/usr/bin/env bash
set -euo pipefail

PREFIX=""
if [ "$EUID" -ne 0 ]; then
  PREFIX="sudo "
fi

if ! command -v apt >/dev/null 2>&1; then
  echo "install-deps.sh does not support Non-Debian distros"
  exit 1
fi

echo "Installing build dependencies..."
$PREFIX apt-get update
$PREFIX apt-get install -y build-essential pkg-config git zlib1g-dev

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "Installing ffmpeg..."
  $PREFIX apt-get install -y ffmpeg
fi

if ! command -v mp4box >/dev/null 2>&1; then
  echo "Installing gpac and MP4Box..."
  cd /tmp/ || exit 1
  git clone --depth=1 https://github.com/gpac/gpac.git
  cd gpac || exit 1
  ./configure --static-bin
  make
  $PREFIX make install

  MP4BOX_PATH=$(command -v MP4Box)
  if [ -n "$MP4BOX_PATH" ]; then
    $PREFIX ln -sf "$MP4BOX_PATH" "$(dirname "$MP4BOX_PATH")/mp4box"
  fi
  rm -rf /tmp/gpac
fi

if ! command -v mp4edit >/dev/null 2>&1; then
  echo "Installing Bento4 toolkits..."
  cd /tmp/ || exit 1
  git clone --depth=1 https://github.com/axiomatic-systems/Bento4.git
  mkdir -p Bento4/cmakebuild
  cd Bento4/cmakebuild || exit 1
  cmake -DCMAKE_BUILD_TYPE=Release ..
  make
  $PREFIX make install
  rm -rf /tmp/Bento4
fi

echo "Done."
