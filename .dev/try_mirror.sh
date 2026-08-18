set -e
sed -i -e 's|deb.debian.org|mirrors.aliyun.com|g' -e 's|security.debian.org|mirrors.aliyun.com|g' \
  /etc/apt/sources.list.d/debian.sources 2>/dev/null || true
sed -i -e 's|https\?://apt.postgresql.org/pub/repos/apt|http://mirrors.aliyun.com/postgresql/repos/apt|g' \
  /etc/apt/sources.list.d/pgdg.list
echo "--- pgdg.list now ---"; cat /etc/apt/sources.list.d/pgdg.list
echo "--- apt-get update ---"
apt-get update 2>&1 | grep -E 'pgdg|Err|W:' | head -6
echo "--- hypopg availability ---"
apt-cache policy postgresql-16-hypopg 2>&1 | head -4
