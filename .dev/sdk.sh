#!/usr/bin/env bash
D="$(cd "$(dirname "$0")" && pwd)"
rm -f "$D/sdk.log" "$D/sdk.done"
{
  python3 -m pip install --user -i https://mirrors.aliyun.com/pypi/simple/ \
    claude-agent-sdk pglast networkx 2>&1 | tail -12
  echo "--- import check ---"
  python3 -c "import claude_agent_sdk as s; print('sdk ok:', getattr(s,'__version__','?'))" 2>&1 | tail -2
  python3 -c "import pglast, networkx; print('pglast', pglast.__version__, '| networkx', networkx.__version__)" 2>&1 | tail -2
} > "$D/sdk.log" 2>&1
echo $? > "$D/sdk.done"
