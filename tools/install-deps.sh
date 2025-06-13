#!/usr/bin/env bash

if ! grep -qi 'ID_LIKE=.*debian' /etc/os-release && ! grep -qi '^ID=debian' /etc/os-release; then
  echo "installing AppleMusicDecrypt deps"
else
  echo "install-deps.sh do not support Non-Debian distro"
  exit 1
fi
echo "installing build deps"
sudo apt-get update && sudo apt-get install -y build-essential pkg-config git zlib1g-dev
if ! command -v ffmpeg >/dev/null 2>&1
then
  echo "installing ffmpeg"
  sudo apt-get install -y ffmpeg
fi

if ! command -v mp4box >/dev/null 2>&1
then
  echo "installing gpac and mp4box"
  cd /tmp/ || exit 1
  git clone --depth=1 https://github.com/gpac/gpac.git
  cd /tmp/gpac || exit 1 && ./configure --static-bin && make && sudo make install
  sudo ln -s "$(whereis -b MP4Box | sed -e 's/^MP4Box: //')" "$(whereis -b MP4Box | sed -e 's/^MP4Box: //' -e 's/\bMP4Box\b/mp4box/g')"
  rm -rf /tmp/gpac
fi

if ! command -v mp4edit >/dev/null 2>&1
then
  echo "installing Bento4 toolkits"
  cd /tmp/ || exit 1
  git clone --depth=1 https://github.com/axiomatic-systems/Bento4.git
  mkdir /tmp/Bento4/cmakebuild && cd /tmp/Bento4/cmakebuild || exit 1 && cmake -DCMAKE_BUILD_TYPE=Release .. && make && sudo make install
  rm -rf /tmp/Bento4
fi

echo "done"