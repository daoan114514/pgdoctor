for u in \
  http://mirrors.aliyun.com/postgresql/repos/apt/dists/trixie-pgdg/InRelease \
  http://mirrors.cloud.tencent.com/postgresql/repos/apt/dists/trixie-pgdg/InRelease \
  http://mirror.nju.edu.cn/postgresql/repos/apt/dists/trixie-pgdg/InRelease
do
  code=$(wget -q -S -O /dev/null --timeout=12 "$u" 2>&1 | grep -m1 'HTTP/' | awk '{print $2}')
  echo "$code  $u"
done
