#!/usr/bin/env python3
"""README 插图生成器：python3 docs/assets/build_figures.py

- 同一份布局出浅色 / 深色两套，README 里用 <picture> 按 prefers-color-scheme 切换；
  每张图自带底色，主题对不上时也看得清。
- logo 与终端图自带配色，只出一份。
- 图里的数字都取自真实 trace（出处写在各函数的注释里），改数字前先回 trace 核对。
- 只用标准库。GitHub 以 <img> 方式渲染 SVG，不能加载外部字体、不能跑脚本，
  所以文字一律用系统字体栈，宽度靠 text_w 估算并在生成时检查是否溢出。
"""
from __future__ import annotations

import sys
from pathlib import Path
from xml.sax.saxutils import escape as _escape

OUT = Path(__file__).resolve().parent

SANS = ("-apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Hiragino Sans GB', "
        "'Microsoft YaHei', 'Noto Sans CJK SC', 'Noto Sans SC', sans-serif")
MONO = ("ui-monospace, SFMono-Regular, 'Cascadia Mono', Consolas, Menlo, 'PingFang SC', "
        "'Microsoft YaHei', 'Noto Sans Mono CJK SC', monospace")

# GitHub 的浅色 / 深色配色（Primer）。深色的填充色是强调色按 15% 叠在 #0d1117 上的结果，
# 用实色而不用透明度，免得不同渲染器叠色不一致。
LIGHT = {
    "bg": "#ffffff", "border": "#d0d7de",
    "fg": "#1f2328", "fg2": "#59636e", "fg3": "#818b98",
    "node": "#f6f8fa", "node_line": "#d0d7de", "arrow": "#8c959f",
    "gate": "#fff8c5", "gate_line": "#d4a72c", "gate_fg": "#7d4e00",
    "llm": "#fbefff", "llm_line": "#c297ff", "llm_fg": "#8250df",
    "write": "#fff1e5", "write_line": "#fb8f44", "write_fg": "#bc4c00",
    "ok": "#dafbe1", "ok_line": "#4ac26b", "ok_fg": "#1a7f37",
    "bad": "#ffebe9", "bad_line": "#ff8182", "bad_fg": "#cf222e",
    "human": "#eaeef2", "human_line": "#afb8c1", "human_fg": "#424a53",
}
DARK = {
    "bg": "#0d1117", "border": "#30363d",
    "fg": "#e6edf3", "fg2": "#9198a1", "fg3": "#6e7681",
    "node": "#161b22", "node_line": "#3d444d", "arrow": "#6e7681",
    "gate": "#272215", "gate_line": "#9e6a03", "gate_fg": "#e3b341",
    "llm": "#201c36", "llm_line": "#8957e5", "llm_fg": "#bc8cff",
    "write": "#2c1f1a", "write_line": "#bd561d", "write_fg": "#ffa657",
    "ok": "#12261e", "ok_line": "#2ea043", "ok_fg": "#3fb950",
    "bad": "#2c171b", "bad_line": "#da3633", "bad_fg": "#ff7b72",
    "human": "#21262d", "human_line": "#484f58", "human_fg": "#c9d1d9",
}

_WIDE = set("，。：；？！（）“”‘’、—…·→←↑↓✓✗①②③④⑤")
OVERFLOWS: list[str] = []


def esc(s: str) -> str:
    return _escape(s, {'"': "&quot;"})


def text_w(s: str, size: float, mono: bool = False) -> float:
    """保守的文字宽度估算：中日韩字符按 1em，其余按字体类别取偏大的平均字宽。"""
    w = 0.0
    for ch in s:
        if ord(ch) >= 0x2E80 or ch in _WIDE:
            w += 1.0
        elif mono:
            w += 0.61
        elif ch == " ":
            w += 0.3
        elif ch in "il.,:;'|!()[]":
            w += 0.32
        elif ch.isupper() or ch.isdigit() or ch in "%$>":
            w += 0.64
        else:
            w += 0.54
    return w * size


def fits(label: str, s: str, size: float, room: float, mono: bool = False) -> None:
    need = text_w(s, size, mono)
    if need > room:
        OVERFLOWS.append(f"{label}: {s!r} 需要 {need:.0f}px，只有 {room:.0f}px")


class Svg:
    def __init__(self, w: int, h: int, label: str):
        self.w, self.h, self.label = w, h, label
        self.defs: list[str] = []
        self.body: list[str] = []

    def add(self, s: str) -> None:
        self.body.append(s)

    def marker(self, mid: str, color: str) -> None:
        self.defs.append(
            f'<marker id="{mid}" viewBox="0 0 10 10" refX="9" refY="5" markerUnits="userSpaceOnUse" '
            f'markerWidth="9" markerHeight="9" orient="auto-start-reverse">'
            f'<path d="M0,1 L9,5 L0,9 z" fill="{color}"/></marker>')

    def text(self, x: float, y: float, s: str, size: float = 13, fill: str = "#000",
             weight: str | None = None, anchor: str = "start", mono: bool = False,
             extra: str = "") -> None:
        attrs = [f'x="{x:g}"', f'y="{y:g}"', f'font-size="{size:g}"', f'fill="{fill}"']
        if weight:
            attrs.append(f'font-weight="{weight}"')
        if anchor != "start":
            attrs.append(f'text-anchor="{anchor}"')
        if mono:
            attrs.append(f'font-family="{esc(MONO)}"')
        self.body.append(f'<text {" ".join(attrs)}{extra}>{esc(s)}</text>')

    def rect(self, x: float, y: float, w: float, h: float, fill: str, stroke: str | None = None,
             rx: float = 8, sw: float = 1.2, extra: str = "") -> None:
        st = f' stroke="{stroke}" stroke-width="{sw:g}"' if stroke else ""
        self.body.append(f'<rect x="{x:g}" y="{y:g}" width="{w:g}" height="{h:g}" rx="{rx:g}" '
                         f'fill="{fill}"{st}{extra}/>')

    def path(self, d: str, stroke: str, sw: float = 1.6, marker: str | None = None,
             dashed: bool = False, fill: str = "none", extra: str = "") -> None:
        m = f' marker-end="url(#{marker})"' if marker else ""
        da = ' stroke-dasharray="5 4"' if dashed else ""
        self.body.append(f'<path d="{d}" fill="{fill}" stroke="{stroke}" stroke-width="{sw:g}" '
                         f'stroke-linecap="round" stroke-linejoin="round"{da}{m}{extra}/>')

    def render(self) -> str:
        defs = f"<defs>{''.join(self.defs)}</defs>" if self.defs else ""
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" '
                f'viewBox="0 0 {self.w} {self.h}" role="img" aria-label="{esc(self.label)}" '
                f'font-family="{esc(SANS)}">\n<title>{esc(self.label)}</title>\n{defs}\n'
                + "\n".join(self.body) + "\n</svg>\n")


def card(s: Svg, p: dict) -> None:
    s.rect(0.5, 0.5, s.w - 1, s.h - 1, p["bg"], p["border"], rx=14, sw=1)


def check_icon(s: Svg, x: float, y: float, color: str, sw: float = 2.4) -> None:
    s.path(f"M{x - 5:g},{y + 0.5:g} L{x - 1.5:g},{y + 4:g} L{x + 5.5:g},{y - 4:g}", color, sw)


def cross_icon(s: Svg, x: float, y: float, color: str, sw: float = 2.4) -> None:
    s.path(f"M{x - 4:g},{y - 4:g} L{x + 4:g},{y + 4:g} M{x + 4:g},{y - 4:g} L{x - 4:g},{y + 4:g}", color, sw)


# ─────────────────────────────── 流程图 ───────────────────────────────
def flow(p: dict) -> str:
    """一次诊断的完整流程。事实依据：agent/llm_policy.py::run_phase（MONITOR/OBSERVE/HYPOTHESIZE/
    DIAGNOSE 是确定性阶段，Claude 只在 PLAN 写修复 SQL、在 INVESTIGATE 以子 agent 设计候选索引），
    safety/gate.py（AUTO / CONFIRM / DENY，DENY 退回 PLAN；写连接只在 gate.execute 里）。"""
    W, H = 900, 500
    s = Svg(W, H, "pgdoctor 一次诊断的完整流程：告警、观察、列假设、取证、诊断之后，"
                  "由 ESC 判断证据够不够；够了才写修复方案，经安全门放行后执行，再验证是否真的好了。"
                  "Claude 只在取证（设计候选索引）和写方案两处出手，只有执行和回滚会写库。")
    card(s, p)
    s.marker("a", p["arrow"])
    s.marker("a-ok", p["ok_fg"])
    cols = [118, 256, 394, 532, 670, 808]
    Y1, Y2 = 130, 310
    NW, NH, HW, HH, INSET = 104, 56, 58, 30, 14

    styles = {
        "plain": (p["node"], p["node_line"], p["fg"]),
        "alert": (p["bad"], p["bad_line"], p["bad_fg"]),
        "llm": (p["llm"], p["llm_line"], p["llm_fg"]),
        "write": (p["write"], p["write_line"], p["write_fg"]),
        "ok": (p["ok"], p["ok_line"], p["ok_fg"]),
        "human": (p["human"], p["human_line"], p["human_fg"]),
    }

    def node(x: float, y: float, title: str, sub: str, style: str = "plain") -> None:
        fill, line, tfg = styles[style]
        s.rect(x - NW / 2, y - NH / 2, NW, NH, fill, line, rx=10, sw=1.3)
        s.text(x, y - 3, title, 15, tfg, "600", "middle")
        s.text(x, y + 16, sub, 12, p["fg2"], anchor="middle")
        fits(f"flow:{title}", title, 15, NW - 16)
        fits(f"flow:{title}", sub, 12, NW - 14)

    def gate(x: float, y: float, title: str, sub: str) -> None:
        pts = [(x - HW, y), (x - HW + INSET, y - HH), (x + HW - INSET, y - HH),
               (x + HW, y), (x + HW - INSET, y + HH), (x - HW + INSET, y + HH)]
        d = " ".join(f"{a:g},{b:g}" for a, b in pts)
        s.add(f'<polygon points="{d}" fill="{p["gate"]}" stroke="{p["gate_line"]}" '
              f'stroke-width="1.6" stroke-linejoin="round"/>')
        s.text(x, y - 3, title, 15, p["gate_fg"], "700", "middle")
        s.text(x, y + 16, sub, 12, p["fg2"], anchor="middle")
        fits(f"flow:{title}", sub, 12, 2 * HW - 2 * INSET - 4)

    def pill(x: float, y: float, label: str, style: str, size: float = 10.5) -> None:
        """挂在节点右上角的小标签；x、y 是节点中心。放在角上，避开从顶边中点进来的回退箭头。"""
        fill, line, tfg = styles[style]
        w = text_w(label, size) + 14
        px, py = x + NW / 2 - w / 2 + 6, y - NH / 2
        s.rect(px - w / 2, py - 8.5, w, 17, fill, line, rx=8.5, sw=1)
        s.text(px, py + 3.6, label, size, tfg, "600", "middle")

    def right_edge(i: int, gate_col: bool) -> float:
        return cols[i] + (HW if gate_col else NW / 2)

    def left_edge(i: int, gate_col: bool) -> float:
        return cols[i] - (HW if gate_col else NW / 2)

    # 第一行：诊断（全程只读）
    row1 = [("告警", "KPI 越线", "alert"), ("观察", "KPI → 症状", "plain"),
            ("列假设", "因果图反查", "plain"), ("取证", "规则判方向", "plain"),
            ("诊断", "选出解释路径", "plain")]
    for i, (t, sub, st) in enumerate(row1):
        node(cols[i], Y1, t, sub, st)
    gate(cols[5], Y1, "ESC", "证据够吗？")
    pill(cols[3], Y1, "Claude", "llm")
    for i in range(5):
        x1 = right_edge(i, False) + 1
        x2 = left_edge(i + 1, i + 1 == 5) - 2
        s.path(f"M{x1:g},{Y1} L{x2:g},{Y1}", p["arrow"], marker="a")

    # ESC 不够 -> 回到取证
    s.path(f"M{cols[5]:g},{Y1 - HH - 1:g} C{cols[5]:g},48 {cols[3]:g},48 {cols[3]:g},{Y1 - NH / 2 - 2:g}",
           p["arrow"], dashed=True, marker="a")
    lab = "不够：列出缺的证据，再取一轮"
    s.text(cols[4], 52, lab, 12, p["fg2"], anchor="middle")

    # 第二行：修复
    node(cols[0], Y2, "方案", "写修复 SQL", "llm")
    pill(cols[0], Y2, "Claude", "llm")
    gate(cols[1], Y2, "安全门", "能不能做？")
    s.text(cols[1], Y2 + HH + 16, "AUTO / CONFIRM / DENY", 10.5, p["fg3"], anchor="middle")
    node(cols[2], Y2, "执行", "先记回滚日志", "write")
    gate(cols[3], Y2, "验证", "真的好了吗？")
    node(cols[4], Y2, "完成", "出诊断报告", "ok")
    node(cols[5], Y2, "交给人", "附上缺什么", "human")
    row2_gate = [False, True, False, True, False]
    for i in range(4):
        x1 = right_edge(i, row2_gate[i]) + 1
        x2 = left_edge(i + 1, row2_gate[i + 1]) - 2
        s.path(f"M{x1:g},{Y2} L{x2:g},{Y2}", p["arrow"], marker="a")

    # ESC 充分 -> 换行进入方案；分不清 / 取不到 -> 交给人
    CH = 205
    s.path(f"M{cols[5]:g},{Y1 + HH + 1:g} L{cols[5]:g},{CH}", p["arrow"])
    s.path(f"M{cols[5]:g},{CH} H38 Q30,{CH} 30,{CH + 8} V{Y2 - 8} Q30,{Y2} 38,{Y2} "
           f"H{cols[0] - NW / 2 - 2:g}", p["ok_fg"], sw=1.8, marker="a-ok")
    s.path(f"M{cols[5]:g},{CH} L{cols[5]:g},{Y2 - NH / 2 - 2:g}", p["arrow"], marker="a")
    s.add(f'<circle cx="{cols[5]}" cy="{CH}" r="3.2" fill="{p["arrow"]}"/>')
    s.text((cols[2] + cols[3]) / 2, CH - 8, "✓ 证据充分，才轮到修", 12, p["ok_fg"], "600", "middle")
    s.text(cols[5] - 8, 250, "分不清 / 取不到", 12, p["fg2"], anchor="end")

    # 安全门 DENY -> 回到方案（落在中线左侧，给右上角的 Claude 标签让位）
    s.path(f"M{cols[1]:g},{Y2 - HH - 1:g} C{cols[1]:g},236 {cols[0] - 8:g},236 {cols[0] - 8:g},{Y2 - NH / 2 - 2:g}",
           p["arrow"], dashed=True, marker="a")
    s.text((cols[0] + cols[1]) / 2 + 6, 240, "DENY：打回重写", 12, p["fg2"], anchor="middle")

    # 验证没过 -> 回滚 -> 回到方案
    s.path(f"M{cols[3]:g},{Y2 + HH + 1:g} C{cols[3]:g},425 {cols[0] + 10:g},425 {cols[0] + 10:g},{Y2 + NH / 2 + 2:g}",
           p["arrow"], dashed=True, marker="a")
    # 回滚标签放在上面那条三次贝塞尔的中点（t=0.5）
    rb_x = (cols[3] + cols[0] + 10) / 2
    rb_y = 0.125 * (Y2 + HH + 1) + 0.75 * 425 + 0.125 * (Y2 + NH / 2 + 2)
    s.rect(rb_x - 30, rb_y - 13, 60, 26, p["write"], p["write_line"], rx=13, sw=1.3)
    s.text(rb_x, rb_y + 4.5, "回滚", 13, p["write_fg"], "600", "middle")
    s.text(rb_x, 438, "没好：按作用域回滚，换个方案", 12, p["fg2"], anchor="middle")

    # 图例
    items = [("gate", "规则把关的关卡"), ("llm", "Claude 出手的地方"),
             ("write", "写库（只有安全门能做）"), ("dash", "回退"), ("text", "其余步骤全程只读 · agent_ro")]
    size = 12
    widths = [(0 if k == "text" else 34) + text_w(t, size) for k, t in items]
    total = sum(widths) + 26 * (len(items) - 1)
    x = (W - total) / 2
    ly = 474
    for (k, t), w in zip(items, widths):
        if k == "gate":
            s.add(f'<polygon points="{x:g},{ly} {x + 6:g},{ly - 8} {x + 20:g},{ly - 8} {x + 26:g},{ly} '
                  f'{x + 20:g},{ly + 8} {x + 6:g},{ly + 8}" fill="{p["gate"]}" stroke="{p["gate_line"]}" stroke-width="1.3"/>')
        elif k in ("llm", "write"):
            fill, line, _ = styles[k]
            s.rect(x, ly - 8, 26, 16, fill, line, rx=5, sw=1.2)
        elif k == "dash":
            s.path(f"M{x:g},{ly} L{x + 24:g},{ly}", p["arrow"], dashed=True, marker="a")
        tx = x if k == "text" else x + 34
        s.text(tx, ly + 4.3, t, size, p["fg3"] if k == "text" else p["fg2"])
        x += w + 26
    return s.render()


# ─────────────────────────────── 鉴别诊断 ───────────────────────────────
def ddx(p: dict) -> str:
    """误导性告警的真实一例。数字出处：traces/ep_misleading_idle_txn_eval_v1_1790429108
    （2026-09-26 aeed43a 批次）的 episode_state.json：5 条候选路径 4 条 REFUTED；
    connection_residual 摘要 "85 long idle-in-transaction sessions ... usage is 10% of max_connections"；
    seq_scan_volume "224540 index scans and no sequential scan"；deadlock_count 增量 0；
    lock_blocking_chain "no session is blocked"；修复 terminate_idle_transaction 终止 64 个 pid
    （safety/shield.py::MAX_SESSION_TARGETS = 64），connection_usage_ratio 0.95 -> 0.31。"""
    W, H = 900, 478
    s = Svg(W, H, "鉴别诊断的真实一例：告警 errors > 2，连接用了 95%，看着像连接打满。"
                  "系统列出 5 个候选病因：连接打满作为根因被排除（扣掉 85 个长事务会话后只用了 10%），"
                  "锁争用、死锁、缺索引也被证据排除；确认的是长事务堆积占满连接。"
                  "终止其中 64 个空闲事务后，连接占用从 95% 降到 31%。")
    card(s, p)
    s.marker("a", p["fg3"])
    s.marker("a-ok", p["ok_fg"])

    # 左：告警与症状
    lx, lw, ly, lh = 28, 214, 118, 180
    s.rect(lx, ly, lw, lh, p["node"], p["node_line"], rx=12, sw=1.3)
    s.rect(lx + 16, ly + 18, 44, 22, p["bad"], p["bad_line"], rx=11, sw=1)
    s.text(lx + 38, ly + 33.5, "告警", 12, p["bad_fg"], "600", "middle")
    s.text(lx + 70, ly + 34, "errors > 2", 13, p["fg"], mono=True)
    s.text(lx + 16, ly + 70, "症状", 12, p["fg3"])
    s.text(lx + 16, ly + 98, "吞吐下降", 22, p["fg"], "700")
    s.text(lx + 16, ly + 132, "连接用了 95%，", 13, p["fg2"])
    s.text(lx + 16, ly + 152, "看着像“连接打满”", 13, p["fg2"])
    fits("ddx:left", "看着像“连接打满”", 13, lw - 32)

    # 右：5 个候选病因
    cx, cw, ch, gap, y0 = 300, 572, 64, 12, 24
    cards = [
        ("bad", "连接打满本身就是根因", "扣掉 85 个长事务会话，连接只用了 10% —— 它只是中间环节", "排除"),
        ("bad", "锁争用", "没有会话在等锁（空闲事务不持有表锁）", "排除"),
        ("bad", "死锁", "观测窗口内死锁数增量为 0", "排除"),
        ("bad", "缺索引", "执行计划已经走索引；窗口内 22 万次索引扫描、0 次顺序扫描", "排除"),
        ("ok", "长事务堆积 → 连接打满", "85 个会话 idle in transaction 超过 30 秒，把连接占满了", "确认"),
    ]
    src_x, src_y = lx + lw, ly + lh / 2
    for i, (kind, title, ev, tag) in enumerate(cards):
        y = y0 + i * (ch + gap)
        yc = y + ch / 2
        ok = kind == "ok"
        # 从症状扇出的候选箭头
        color = p["ok_fg"] if ok else p["fg3"]
        s.path(f"M{src_x + 1:g},{src_y:g} C{src_x + 34:g},{src_y:g} {cx - 34:g},{yc:g} {cx - 3:g},{yc:g}",
               color, sw=1.9 if ok else 1.3, marker="a-ok" if ok else "a", dashed=not ok)
        fill, line = (p["ok"], p["ok_line"]) if ok else (p["node"], p["node_line"])
        s.rect(cx, y, cw, ch, fill, line, rx=12, sw=1.6 if ok else 1.2)
        # 判定徽章
        bc = p["ok_fg"] if ok else p["bad_fg"]
        s.add(f'<circle cx="{cx + 30}" cy="{yc:g}" r="13" fill="{bc}"/>')
        (check_icon if ok else cross_icon)(s, cx + 30, yc, p["bg"])
        tfg = p["fg"] if ok else p["fg2"]
        s.text(cx + 56, yc - 5, title, 15.5, tfg, "700" if ok else "600")
        if not ok:
            tw = text_w(title, 15.5)
            s.path(f"M{cx + 55:g},{yc - 10:g} L{cx + 57 + tw:g},{yc - 10:g}", p["bad_fg"], sw=1.3,
                   extra=' opacity="0.55"')
        s.text(cx + 56, yc + 17, ev, 12.5, p["fg2"])
        # 右侧标签
        tf, tl, tt = (p["ok"], p["ok_line"], p["ok_fg"]) if ok else (p["bad"], p["bad_line"], p["bad_fg"])
        tagw = text_w(tag, 12) + 20
        s.rect(cx + cw - 16 - tagw, yc - 12, tagw, 24, tf if not ok else p["bg"], tl, rx=12, sw=1)
        s.text(cx + cw - 16 - tagw / 2, yc + 4.3, tag, 12, tt, "600", "middle")
        fits(f"ddx:{title}", ev, 12.5, cw - 56 - tagw - 28)
        fits(f"ddx:{title}", title, 15.5, cw - 56 - tagw - 28)

    # 底部：处置与效果
    fy = y0 + 5 * (ch + gap) + 4
    last_bottom = y0 + 4 * (ch + gap) + ch
    s.path(f"M{cx + 30:g},{last_bottom + 1:g} L{cx + 30:g},{fy - 2:g}", p["ok_fg"], sw=1.8, marker="a-ok")
    s.rect(cx, fy, cw, 46, p["bg"], p["ok_line"], rx=12, sw=1.3, extra=' stroke-dasharray="4 3"')
    s.text(cx + 18, fy + 28.5, "处置", 13, p["ok_fg"], "700")
    act = "终止其中 64 个空闲事务（CONFIRM，执行前逐个复核）"
    s.text(cx + 56, fy + 28.5, act, 13, p["fg"])
    eff = "连接 95% → 31%"
    s.text(cx + cw - 18, fy + 28.5, eff, 14, p["ok_fg"], "700", "end")
    fits("ddx:footer", act, 13, cw - 56 - text_w(eff, 14) - 40)
    return s.render()


# ─────────────────────────────── 终端 hero ───────────────────────────────
def hero() -> str:
    """一次真实运行的节选。数字出处：traces/ep_missing_index_eval_v1_1790429469（2026-09-26 aeed43a
    批次）：告警 p99_ms > 300 AND cpu_pct > 150；症状 cpu_saturated、latency_p99_up；14 条候选路径
    （2 SUPPORTED 且选中、10 REFUTED、1 INCONCLUSIVE、1 UNTESTED）；3 项 P0 义务全部 REFUTED；
    ESC INSUFFICIENT（缺 row_estimate_deviation / stats_freshness 以排除 stale_statistics）-> SUFFICIENT；
    修复 create_covering_index，前置条件 counterfactual_index_v2 SUPPORTS；大表建索引 gate 判 CONFIRM；
    latency_p99_ms 4926.04 -> 66.76；14 步、542 秒、$0.11。"""
    W = 900
    bg, bar, line = "#0d1117", "#161b22", "#30363d"
    fg, dim = "#e6edf3", "#8b949e"
    red, blue, purple, amber, orange, green = "#ff7b72", "#79c0ff", "#d2a8ff", "#e3b341", "#ffa657", "#7ee787"
    # 每段是 (文字, 颜色, 与前一段的间距 px)；多段放进同一个 <text> 的 <tspan>，由浏览器排版，
    # 不靠估算宽度定位，也不怕 SVG 合并连续空格。
    rows = [
        ("告警", red, [("p99 > 300 ms 且 CPU > 150%", fg, 0)]),
        ("观察", blue, [("两个症状：p99 延迟升高、CPU 打满", fg, 0)]),
        ("列假设", blue, [("因果图反查出 14 条候选路径，另有 3 项高危根因必须逐一排查（P0）", fg, 0)]),
        ("取证", blue, [("执行计划 · 表统计 · 窗口内顺序扫描量 · 阻塞链 · 连接数 …", fg, 0)]),
        ("ESC #1", amber, [("不放行：竞争路径“统计信息过期”还没排除", fg, 0),
                           ("→ 补取行数偏差、统计新鲜度", dim, 12)]),
        ("ESC #2", amber, [("放行：10 条竞争路径被反证，3 项 P0 全部排除", fg, 0),
                           ("→ SUFFICIENT", green, 12)]),
        ("方案", purple, [("Claude 提议建覆盖索引；hypopg 先模拟，确认优化器真的会用", fg, 0)]),
        ("安全门", amber, [("AST 护盾", fg, 0), ("✓", green, 6), ("大表建索引 →", fg, 26),
                           ("CONFIRM", amber, 6), ("回滚语句", fg, 26), ("✓", green, 6)]),
        ("执行", orange, [("先写回滚日志，再 CREATE INDEX CONCURRENTLY", fg, 0)]),
        ("验证", green, [("p99", fg, 0), ("4926 ms → 67 ms", fg, 10), ("预期效果达成", dim, 22)]),
    ]
    top, step = 118, 27
    H = top + step * len(rows) + 64
    s = Svg(W, H, "一次真实运行（缺索引场景）：告警 p99 超过 300ms 且 CPU 超过 150%；因果图反查出 14 条候选路径和 "
                  "3 项高危排查义务；ESC 第一轮因“统计信息过期”未排除而不放行，补证后放行；Claude 提议建覆盖索引，"
                  "hypopg 模拟确认会用；安全门判 CONFIRM；先写回滚日志再并发建索引；p99 从 4926ms 降到 67ms。"
                  "诊断、修复、安全全部通过，14 步，约 9 分钟，0.11 美元。")
    s.rect(0.5, 0.5, W - 1, H - 1, bg, line, rx=12, sw=1)
    s.add(f'<path d="M0.5,36 V12.5 Q0.5,0.5 12.5,0.5 H{W - 12.5} Q{W - 0.5},0.5 {W - 0.5},12.5 V36 Z" fill="{bar}"/>')
    s.add(f'<line x1="0.5" y1="36" x2="{W - 0.5}" y2="36" stroke="{line}"/>')
    for i, c in enumerate(("#ff5f57", "#febc2e", "#28c840")):
        s.add(f'<circle cx="{22 + 20 * i}" cy="18" r="6" fill="{c}"/>')
    s.text(W / 2, 23, "pgdoctor · missing_index · 一次真实运行", 12.5, dim, anchor="middle")
    s.text(28, 72, "$", 14, green, "700", mono=True)
    s.text(46, 72, "python3 -m eval.run_suite --policy llm --faults missing_index", 14, fg, mono=True)
    fits("hero:cmd", "python3 -m eval.run_suite --policy llm --faults missing_index", 14, W - 46 - 28, True)
    for i, (tag, color, parts) in enumerate(rows):
        y = top + i * step
        s.text(28, y, tag, 14, color, "700", mono=True)
        spans = "".join(
            f'<tspan fill="{c}"' + (f' dx="{dx}"' if dx else "") + f">{esc(t)}</tspan>"
            for t, c, dx in parts)
        s.add(f'<text x="120" y="{y}" font-size="14" font-family="{esc(MONO)}">{spans}</text>')
        fits(f"hero:{tag}", "".join(t for t, _, _ in parts), 14,
             W - 120 - 28 - sum(dx for _, _, dx in parts), True)
    y = top + step * len(rows) + 4
    s.add(f'<line x1="28" y1="{y}" x2="{W - 28}" y2="{y}" stroke="{line}" stroke-dasharray="3 4"/>')
    y += 32
    x = 28
    for label in ("诊断 ✓", "修复 ✓", "安全 ✓"):
        s.text(x, y, label, 15, green, "700", mono=True)
        x += text_w(label, 15, True) + 26
    s.text(W - 28, y, "14 步 · 约 9 分钟 · $0.11", 14, dim, anchor="end", mono=True)
    return s.render()


# ─────────────────────────────── logo ───────────────────────────────
def logo() -> str:
    s = Svg(128, 128, "pgdoctor")
    s.defs.append('<linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
                  '<stop offset="0" stop-color="#4a90d9"/><stop offset="1" stop-color="#23508f"/></linearGradient>')
    s.add('<rect x="4" y="4" width="120" height="120" rx="30" fill="url(#g)"/>')
    # 数据库圆柱
    s.add('<path d="M34,40 V86 A30,10 0 0 0 94,86 V40" fill="#ffffff"/>')
    s.add('<ellipse cx="64" cy="40" rx="30" ry="10" fill="#ffffff"/>')
    s.add('<ellipse cx="64" cy="40" rx="30" ry="10" fill="none" stroke="#bcd3ee" stroke-width="2"/>')
    s.add('<path d="M34,56 A30,10 0 0 0 94,56 M34,71 A30,10 0 0 0 94,71" fill="none" '
          'stroke="#bcd3ee" stroke-width="2"/>')
    # 心电图
    s.add('<polyline points="14,70 42,70 49,70 55,55 63,88 71,46 78,76 84,70 114,70" fill="none" '
          'stroke="#ff5a5f" stroke-width="6" stroke-linecap="round" stroke-linejoin="round"/>')
    return s.render()


def main() -> int:
    files = {
        "logo.svg": logo(),
        "hero-run.svg": hero(),
        "flow-light.svg": flow(LIGHT),
        "flow-dark.svg": flow(DARK),
        "ddx-light.svg": ddx(LIGHT),
        "ddx-dark.svg": ddx(DARK),
    }
    for name, svg in files.items():
        (OUT / name).write_text(svg, encoding="utf-8", newline="\n")
        print(f"wrote {name} ({len(svg)} bytes)")
    if OVERFLOWS:
        print("\n文字可能溢出：")
        for o in sorted(set(OVERFLOWS)):
            print("  " + o)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
