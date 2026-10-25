#!/usr/bin/env python3
"""lumin-job-criterion（LJC）· 更新检测器：已装技能哪些有上游新版本。

双轨检测（2026-10-25 实测：11 个技能源里只有 1 个是 git 仓库，其余为
tarball 下载——单轨 git 比对只能覆盖 15/122 个技能，故必须双轨）：

  git 来源（ecc-study 等）   ：fetch + 本地 HEAD vs 上游 tracking → 精确落后数
  tarball 来源（已登记）      ：GitHub API 比对上流 pushed_at 与本地副本时间
                               → 「可能可更新」（日期启发式，如实标注）
  tarball 来源（未登记）      ：如实报告「无法自动检测」，引导登记 ljc_sources.json

用法：
  python3 check_updates.py                    # 全量检测
  python3 check_updates.py code-review pdf    # 只检测指定技能

纪律（继承 LJC）：
  - 只检测、只给路径，不自动 pull/下载——更新是写操作，必须用户点头
  - API 限额/网络失败如实说，不假装查过
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, "..", "index.json")
UA = "lumin-job-criterion"


def git(repo, *args, timeout=90):
    try:
        r = subprocess.run(["git", "-C", repo] + list(args),
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return 99, "", str(e)


def load_index():
    with open(INDEX, encoding="utf-8") as f:
        return json.load(f)


def load_sources():
    """用户登记的 tarball 来源 → owner/repo（git 来源自动识别，无需登记）。"""
    p = os.path.join(HERE, "ljc_sources.json")
    if os.path.isfile(p):
        try:
            d = json.load(open(p, encoding="utf-8"))
            return {k: v for k, v in d.items()
                    if not k.startswith("_") and isinstance(v, str)}
        except Exception:
            return {}
    return {}


def api_get(url):
    headers = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return True, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 403:
            return False, "GitHub API 限额（未认证 60 次/时；设 GITHUB_TOKEN 可解）"
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, f"网络错误: {e}"


def source_of(path):
    """技能真实路径 → 来源目录名；本地自建技能返回 local:<name>。"""
    if "/ecc-study/" in path:
        return "ecc-study"  # ~/ecc-study 整个是一个 git 仓库
    if "/skill-sources/" in path:
        return path.split("/skill-sources/", 1)[1].split("/", 1)[0]
    if path.startswith(os.path.expanduser("~/.workbuddy/skills")):
        return "local:" + os.path.basename(path.rstrip("/"))
    return None


def newest_mtime(path):
    newest = 0.0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "__pycache__", ".cache")]
        for fn in files:
            try:
                newest = max(newest, os.path.getmtime(os.path.join(root, fn)))
            except OSError:
                pass
    return newest


def check_git_source(src_path):
    """git 来源：精确比对。返回状态 dict。"""
    code, top, _ = git(src_path, "rev-parse", "--show-toplevel")
    if code != 0:
        return {"state": "not-git"}
    code, _, err = git(top, "fetch", "--quiet")
    if code != 0:
        coder, remotes, _ = git(top, "remote")
        if coder != 0 or not remotes.strip():
            return {"state": "no-upstream"}  # 本地仓库无 remote，谈不上更新
        return {"state": "fetch-failed", "reason": err[:80]}
    code, head, _ = git(top, "rev-parse", "HEAD")
    code2, up, _ = git(top, "rev-parse", "@{u}")
    if code2 != 0:
        # 分支没设 tracking：退回与 origin/<当前分支> 比
        codeb, branch, _ = git(top, "rev-parse", "--abbrev-ref", "HEAD")
        code2, up, _ = git(top, "rev-parse", f"origin/{branch}")
    if code2 != 0:
        return {"state": "no-upstream"}
    if head == up:
        return {"state": "current"}
    code3, behind, _ = git(top, "rev-list", "--count", f"HEAD..{up}")
    code4, date, _ = git(top, "log", "-1", "--format=%cd", "--date=short", up)
    return {"state": "outdated", "behind": int(behind or 0),
            "date": date, "repo": top, "dirty": bool(git(top, "status", "--porcelain")[1])}


def check_remote_source(src_path, repo_slug):
    """tarball 来源（已登记）：API 日期启发式。"""
    ok, d = api_get(f"https://api.github.com/repos/{repo_slug}")
    if not ok:
        return {"state": "check-failed", "reason": d}
    pushed = (d.get("pushed_at") or "")[:10]
    local = time.strftime("%Y-%m-%d", time.localtime(newest_mtime(src_path)))
    if pushed and pushed > local:
        return {"state": "maybe-outdated", "upstream_date": pushed, "local_date": local}
    return {"state": "current-remote", "upstream_date": pushed, "local_date": local}


def main():
    names = [a for a in sys.argv[1:] if not a.startswith("-")]
    index = load_index()
    entries = [e for e in index["skills"] if not names or e["name"] in names]
    if not entries:
        print("索引里没有对应技能。")
        return
    by_source = {}
    for e in entries:
        src = source_of(e.get("path", ""))
        if src:
            by_source.setdefault(src, []).append(e)
    cfg = load_sources()
    results = []
    for src, es in sorted(by_source.items(), key=lambda kv: -len(kv[1])):
        src_path = os.path.commonpath([e["path"] for e in es])
        r = check_git_source(src_path)
        if r["state"] == "not-git":
            slug = cfg.get(src)
            if slug:
                r = check_remote_source(src_path, slug)
            else:
                r = {"state": "unknown-source"}
        results.append((src, len(es), r))
    # local: 条目拆开：是 git 仓库的参与正常分区；无上游概念的折叠成一行
    local_plain = [(s, n) for s, n, r in results
                   if s.startswith("local:") and r["state"] == "unknown-source"]
    results = [(s, n, r) for s, n, r in results
               if not (s.startswith("local:") and r["state"] == "unknown-source")]
    n_local = len(entries) - sum(n for _, n, _ in results)
    print(f"LJC 更新检测 —— {len(results) + len(local_plain)} 个来源 / {len(entries)} 个技能\n")

    def show(pred, title, note=""):
        rows = [(s, n, r) for s, n, r in results if pred(r)]
        if not rows:
            return
        print(f"── {title} ──")
        if note:
            print(f"   {note}")
        for s, n, r in rows:
            skills = ", ".join(e["name"] for e in by_source[s][:6])
            if len(by_source[s]) > 6:
                skills += f" 等 {len(by_source[s])} 个"
            print(f" · {s}（{n} 个技能）：{skills}")
        print()

    show(lambda r: r["state"] == "current", "已是最新（git 上游对齐）")
    show(lambda r: r["state"] == "current-remote", "已是最新（API 日期比对）")
    show(lambda r: r["state"] == "outdated", "可更新（git 精确比对）",
         "更新=git pull，需用户同意；有本地改动会标 dirty")
    show(lambda r: r["state"] == "maybe-outdated", "可能可更新（API 日期启发式，仅供参考）")
    show(lambda r: r["state"] in ("fetch-failed", "check-failed"), "检测失败（网络/限额）")
    show(lambda r: r["state"] in ("no-upstream", "unknown-source"), "无法自动检测",
         "非 git 且未登记来源——在 scripts/ljc_sources.json 登记 owner/repo 后即可查")
    if local_plain:
        print(f"── 本地自建/单文件技能（{sum(n for _, n in local_plain)} 个）──")
        print("   无上游概念（手动创建或市场直装），跳过更新检测\n")

    print("详细：")
    for s, n, r in results:
        if r["state"] == "outdated":
            print(f" · {s}：落后 {r['behind']} 个 commit（上游最后更新 {r['date']}）"
                  + ("，本地有未提交改动，pull 前先处理" if r.get("dirty") else "")
                  + f"\n   更新命令：git -C {r['repo']} pull")
        elif r["state"] == "maybe-outdated":
            print(f" · {s}：上游最后推送 {r['upstream_date']}，本地副本 {r['local_date']}"
                  f"\n   登记仓库：{cfg.get(s)}——更新=重新下载 tarball，可让 LJC 协助")
        elif r["state"] == "check-failed":
            print(f" · {s}：{r['reason']}")
        elif r["state"] == "fetch-failed":
            print(f" · {s}：{r['reason']}")
    print("\n纪律：本检测只读；真要更新必须你点头——git 来源执行上面的 pull 命令，"
          "tarball 来源我帮你重新下载。")


if __name__ == "__main__":
    main()
