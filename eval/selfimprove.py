#!/usr/bin/env python3
"""LJC · 回炉自改进循环——移植 Google Harness²（★20.3k Apache-2.0）的
Propose–Probe–Compose 方法论（arXiv:2610.14224），量尺用 LJC 自己的评估集。

  Propose  产出「改进工作表」：评估集失败/退化用例 + 当前术语映射快照，
           交宿主模型（或人）提出候选改动（改术语/停用词/权重前先看尺子）。
  Probe    改动落地后跑评估集 --compare 基线，并做 stash 归因：
           区分「本次改动引入的退化」与「索引扩容等既有欠账」。
  Compose  裁决：无新增退化且改进≥0 → 采纳（git 提交由操作者执行）；
           有新增退化 → 建议回滚（给出命令，不代执行）。

用法：
  python3 eval/selfimprove.py propose    # 改进工作表（改动前跑）
  python3 eval/selfimprove.py probe      # 改动落地后跑：通过/回滚建议
  python3 eval/selfimprove.py compose    # 按 probe 结论给最终裁决与命令

纯标准库；基线固定为 eval/baseline_v1.json。
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BASELINE = os.path.join(HERE, "baseline_v1.json")
TERMS_FILE = os.path.join(ROOT, "scripts", "ljc_terms.json")


def _run_eval(args):
    cmd = [sys.executable, os.path.join(HERE, "run_eval.py")] + args
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True).stdout


def _parse_summary(text):
    """从 run_eval 输出抽 改进/退化 列表。"""
    improved, degraded = [], []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("改进"):
            improved = _nums(s)
        elif s.startswith("退化"):
            degraded = _nums(s)
    return improved, degraded


def _nums(s):
    import re
    return [int(n) for n in re.findall(r"\d+", s.split("：", 1)[-1].split("——")[0])]


def _queries():
    data = json.load(open(os.path.join(HERE, "queries.json")))
    cases = data.get("cases", [])
    return {c["id"]: c for c in cases}


def propose():
    """Propose：失败与退化用例 + 术语快照 + 改动建议方向。"""
    out = _run_eval(["--compare", BASELINE])
    improved, degraded = _parse_summary(out)
    qs = _queries()
    print("═" * 62)
    print("LJC 自改进 · Propose 工作表")
    print("═" * 62)
    print(f"基线: {os.path.relpath(BASELINE, ROOT)}")
    print(f"当前: 改进 {improved} | 退化 {degraded}")
    print()
    print("── 真失败用例（两次都不过=库存缺口，优先处理）──")
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("两次都失败"):
            print("  " + s)
    print()
    print("── 退化用例原文（本次改动前先归因： stash 对照区分新旧账）──")
    for n in degraded:
        q = qs.get(n, {})
        print(f"  #{n}: {q.get('query', '?')}  [accept: {q.get('accept', '?')}]")
    print()
    print("── 当前用户术语表 ──")
    if os.path.isfile(TERMS_FILE):
        print(open(TERMS_FILE).read())
    else:
        print(f"  （{os.path.relpath(TERMS_FILE, ROOT)} 不存在——可创建，格式见 .example.json）")
    print()
    print("改动方向（按杠杆排序）：① 补/收术语映射  ② 停用词  ③ 权重（无底洞，慎动）")
    print("改完跑: python3 eval/selfimprove.py probe")


def probe():
    """Probe：改动后跑对照 + stash 归因。"""
    out = _run_eval(["--compare", BASELINE])
    improved, degraded = _parse_summary(out)
    print("── 本轮（含改动）──")
    print(f"  改进 {improved} | 退化 {degraded}")
    # stash 归因：把 find.py 的未提交改动暂存掉再测一次
    stash = subprocess.run(["git", "stash", "push", "-q", "scripts/find.py"],
                           cwd=ROOT, capture_output=True, text=True)
    if stash.returncode == 0:
        base_out = _run_eval(["--compare", BASELINE])
        subprocess.run(["git", "stash", "pop", "-q"], cwd=ROOT, capture_output=True, text=True)
        b_imp, b_deg = _parse_summary(base_out)
        new_deg = [n for n in degraded if n not in b_deg]
        fixed_deg = [n for n in b_deg if n not in degraded]
        print("── stash 归因（区分本次改动 vs 既有欠账）──")
        print(f"  无改动时: 改进 {b_imp} | 退化 {b_deg}")
        print(f"  本次改动新增退化: {new_deg or '无'}")
        print(f"  本次改动修复的旧退化: {fixed_deg or '无'}")
        new_imp = [n for n in improved if n not in b_imp]
        print(f"  本次改动新增改进: {new_imp or '无'}")
        verdict = decide(b_deg, degraded, improved)
        print()
        print("── 裁决 ──")
        print("  " + verdict)
    else:
        print("  （scripts/find.py 无未提交改动，跳过 stash 归因）")
    print()
    print("下一步: python3 eval/selfimprove.py compose")


def decide(base_deg, degraded, improved):
    """纯函数裁决：Probe 的结论。可单测。"""
    new_deg = [n for n in degraded if n not in base_deg]
    if new_deg:
        return (f"有新增退化 {new_deg}——先收窄改动（宽义术语是常见元凶）再 Probe；"
                f"若属边缘重排（top1 仍在正确领域）需在提交信息里写明理由")
    if len(improved) >= 0 and degraded:
        return (f"无新增退化（既有欠账 {degraded} 为索引扩容旧账）。"
                f"改进 {improved}——可采纳，compose 确认后提交")
    return "无新增退化——可采纳，compose 确认后提交"


def compose():
    """Compose：给最终裁决与执行命令（不代执行 git 提交）。"""
    print("Compose 清单：")
    print("  1. 若 probe 判『可采纳』：git add 改动文件，提交信息必须含评估结论")
    print("     例：术语补X 评估 +2/-3（退化均为扩容旧账，stash 归因）")
    print("  2. 若 probe 判『先收窄』：改完重跑 probe")
    print("  3. 若要回滚术语改动：git checkout -- scripts/find.py")
    print("  4. 经验教训写进 SKILL.md 纪律节（Harness² 的 improvement-harness 层）：")
    print("     一句话格式——『YYYY-MM-DD 学到：X 场景该查 Y』")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "propose":
        propose()
    elif cmd == "probe":
        probe()
    elif cmd == "compose":
        compose()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
