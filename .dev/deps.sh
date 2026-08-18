#!/usr/bin/env bash
D="$(cd "$(dirname "$0")" && pwd)"; cd "$D/.." || exit 9
rm -f "$D/deps.log" "$D/deps.done"
{
  M="https://mirrors.aliyun.com/pypi/simple/"
  if python3 -m venv .venv 2>/dev/null; then
    echo "venv created"
    ./.venv/bin/pip install -q --upgrade pip -i "$M" 2>&1 | tail -2
    ./.venv/bin/pip install -i "$M" 'psycopg[binary]>=3.1' 'PyYAML>=6.0' 'pglast>=5.0' 'networkx>=3.0' 'numpy>=1.24' 2>&1 | tail -6
    ./.venv/bin/python -c "import psycopg, yaml, pglast, networkx, numpy; print('imports OK, psycopg', psycopg.__version__)"
  else
    echo "venv unavailable -> pip --user"
    python3 -m pip install --user -i "$M" 'psycopg[binary]>=3.1' 'PyYAML>=6.0' 2>&1 | tail -4
  fi
} > "$D/deps.log" 2>&1
echo $? > "$D/deps.done"
