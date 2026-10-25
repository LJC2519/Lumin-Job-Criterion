#!/usr/bin/env python3
"""lumin-job-criterion（LJC）· 技能安全检查器。

对「找到的技能」做合规检查——本地技能目录或 GitHub 仓库均可。
纯标准库；检查是启发式的，结论分三档：

  PASS  未发现风险项
  WARN  有需要人工留意的项（缺 license、脚本里有网络调用等）
  BLOCK 发现高危项（管道执行远程脚本、编码载荷、提示词注入、凭证泄漏）

定位（2026-10-25 用户定调）：检查器只负责**把风险说清楚**——
发现疑似不合规时逐条给出说明，不代用户否决；用不用由用户自行决定，
LJC 一律不代为下载、安装或执行。

用法：
  python3 security_check.py <本地目录路径>
  python3 security_check.py <owner/repo>            # GitHub 仓库
  python3 security_check.py <owner/repo> --json

设计说明：
  - 脚本（.py/.js/.ts/.sh）查可执行风险；SKILL.md 查提示词注入与凭证
  - 已知误报类：合法技能讨论安全话题会自带关键词（如审计类技能），
    结论供人判断，机器只负责不漏报
  - 本地已装候选的深度审计可另跑 jason-security-audit 技能的 audit.py（若装着）
"""
import json
import os
import re
import sys

TIMEOUT = 20
UA = "lumin-job-criterion"

CODE_EXTS = (".py", ".js", ".ts", ".mjs", ".cjs", ".sh", ".bash")
KNOWN_HOSTS = ("github.com", "raw.githubusercontent.com", "api.github.com",
               "objects.githubusercontent.com", "lightmake.site",
               "pypi.org", "files.pythonhosted.org", "skillsmp.com",
               "registry.npmjs.org")

BLOCK_PATTERNS = [
    ("管道执行远程脚本", re.compile(r"(curl|wget)[^|\n]{0,200}\|\s*(sudo\s+)?(ba|z|fi)?sh\b"), "script"),
    ("编码载荷进 eval/exec", re.compile(r"\b(eval|exec)\s*\(\s*['\"][A-Za-z0-9+/]{40,}={0,2}"), "script"),
    ("长 base64 块（疑似载荷）", re.compile(r"[A-Za-z0-9+/]{120,}={0,2}"), "script"),
    ("提示词注入：要求忽略先前指令", re.compile(r"ignore\s+(all\s+|any\s+)?(previous|prior|above)\s+instructions?", re.I), "any"),
    ("提示词注入：要求无视自身编程", re.compile(r"disregard\s+(your|all)\s+(instructions?|programming)", re.I), "any"),
    ("提示词注入：身份覆写", re.compile(r"you\s+are\s+now\b(?!\s+a?\s*(skill|agent))", re.I), "any"),
    ("AWS 访问密钥样式", re.compile(r"AKIA[0-9A-Z]{16}"), "any"),
    ("API 密钥样式（sk-）", re.compile(r"sk-[A-Za-z0-9]{20,}"), "any"),
    ("私钥头", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "any"),
    ("硬编码密钥赋值", re.compile(r"(?i)\b(api[_-]?key|secret|token|password)\b\s*[:=]\s*['\"][A-Za-z0-9_\-]{16,}['\"]"), "any"),
]

WARN_PATTERNS = [
    ("脚本含网络调用（curl/wget）", re.compile(r"\b(curl|wget)\s"), "script"),
    ("os.system 调用", re.compile(r"os\.system\s*\("), "script"),
    ("subprocess shell=True", re.compile(r"subprocess\.[A-Za-z]+\([^)]*shell\s*=\s*True"), "script"),
    ("Node child_process.exec", re.compile(r"child_process\.exec"), "script"),
    ("超长单行（>20000 字符，混淆嫌疑）", None, "longline"),
    ("脚本含裸 eval（动态执行）", re.compile(r"(?<![.\w])eval\s*\("), "script"),
    ("引用私钥/凭证文件路径", re.compile(r"(id_rsa|id_ed25519|\.aws[/\\]credentials|\.netrc)"), "any"),
]


def token():
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""


def api_get(url):
    import urllib.error
    import urllib.request
    headers = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
    tok = token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    req = urllib.request.Request(url, headers=headers)
    try:
        import urllib.request as u
        with u.urlopen(req, timeout=TIMEOUT) as resp:
            return True, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, f"网络错误: {e}"


def scan_text(text, where, findings):
    """对一段文本跑全部模式。where=来源标签，findings=累积列表。"""
    lines = text.splitlines()
    for i, line in enumerate(lines, 1):
        for name, pat, scope in BLOCK_PATTERNS:
            if scope == "script" and not where.endswith(CODE_EXTS):
                continue
            if pat and pat.search(line):
                findings.append(("BLOCK", name, f"{where}:{i}", line.strip()[:100]))
        for name, pat, scope in WARN_PATTERNS:
            if scope == "script" and not where.endswith(CODE_EXTS):
                continue
            if scope == "longline":
                if where.endswith(CODE_EXTS) and len(line) > 20000:
                    findings.append(("WARN", name, f"{where}:{i}", f"该行 {len(line)} 字符"))
            elif pat and pat.search(line):
                findings.append(("WARN", name, f"{where}:{i}", line.strip()[:100]))


def scan_external_urls(text, where, findings):
    import urllib.parse
    for m in re.finditer(r"https?://([A-Za-z0-9.\-]+)", text):
        host = m.group(1).lower()
        if not any(host == k or host.endswith("." + k) for k in KNOWN_HOSTS):
            findings.append(("WARN", f"脚本引用外部主机: {host}", f"{where}", ""))


def check_local(path):
    findings = []
    n_code = n_md = 0
    has_license = any(
        os.path.isfile(os.path.join(path, n))
        for n in ("LICENSE", "LICENSE.md", "LICENSE.txt", "LICENCE")
    )
    has_desc = False
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "__pycache__", ".cache")]
        if root.count(os.sep) - path.count(os.sep) >= 2:
            dirs[:] = []
        for fn in files:
            fp = os.path.join(root, fn)
            rel = os.path.relpath(fp, path)
            if fn == "SKILL.md":
                n_md += 1
                text = open(fp, encoding="utf-8", errors="ignore").read()
                fm = text[:text.find("\n---", 3)] if text.startswith("---") else ""
                has_desc = bool(re.search(r"^description:", fm, re.M))
                scan_text(text, rel, findings)
                if re.search(r"disable-model-invocation\s*:\s*true", fm):
                    findings.append(("INFO", "disable-model-invocation=true（作者禁用模型自动调用）", rel, ""))
                scan_external_urls(text, rel, findings)
            elif fn.endswith(CODE_EXTS):
                n_code += 1
                try:
                    text = open(fp, encoding="utf-8", errors="ignore").read()
                except OSError:
                    continue
                scan_text(text, rel, findings)
                scan_external_urls(text, rel, findings)
    if not has_license:
        findings.append(("WARN", "缺少 LICENSE 文件", "（目录级）", ""))
    if not has_desc:
        findings.append(("WARN", "SKILL.md 缺 description 字段", "（目录级）", ""))
    return findings, n_code, n_md, has_license


def _fetch_text(repo, path, ref):
    """经 contents API 取仓库内文件文本（base64）。失败返回 None。"""
    ok, c = api_get(f"https://api.github.com/repos/{repo}/contents/{path}?ref={ref}")
    if ok and "content" in c:
        import base64
        try:
            return base64.b64decode(c["content"]).decode("utf-8", "ignore")
        except Exception:
            return None
    return None


def check_github(repo):
    findings = []
    ok, d = api_get(f"https://api.github.com/repos/{repo}")
    if not ok:
        return [("BLOCK", f"仓库元数据抓取失败：{d}", repo, "")], 0, 0, False
    lic = (d.get("license") or {}).get("spdx_id")
    stars = d.get("stargazers_count", 0)
    archived = d.get("archived", False)
    branch = d.get("default_branch", "main")
    if not lic:
        findings.append(("WARN", "仓库无 license 信息", repo, ""))
    if archived:
        findings.append(("WARN", "仓库已归档（停止维护）", repo, ""))
    if stars < 5:
        findings.append(("WARN", f"星数过低（{stars}），社区验证不足", repo, ""))
    n_code = n_md = 0
    root_text = _fetch_text(repo, "SKILL.md", branch)
    if root_text is not None:
        n_md = 1
        fm = root_text[:root_text.find("\n---", 3)] if root_text.startswith("---") else ""
        if not re.search(r"^description:", fm, re.M):
            findings.append(("WARN", "SKILL.md 缺 description 字段", repo, ""))
        scan_text(root_text, "SKILL.md", findings)
        scan_external_urls(root_text, "SKILL.md", findings)
    else:
        # 合集仓库（如 anthropics/skills）：树形列出 SKILL.md 与代码文件，抽检。
        # 封顶 15+5 个文件，防未认证限额（60 次/时）被打爆。
        import fnmatch
        ok3, tree = api_get(f"https://api.github.com/repos/{repo}/git/trees/{branch}?recursive=1")
        if ok3 and "tree" in tree:
            blobs = [t["path"] for t in tree["tree"] if t["type"] == "blob"]
            def clean(paths):
                return sorted(p for p in paths
                              if not any(seg.startswith(".") for seg in p.split("/")[:-1]))
            skill_paths = clean([p for p in blobs if p.endswith("SKILL.md")])[:15]
            code_paths = clean([p for p in blobs if p.endswith(CODE_EXTS)])[:5]
            for p in skill_paths:
                text = _fetch_text(repo, p, branch)
                if text is None:
                    continue
                n_md += 1
                fm = text[:text.find("\n---", 3)] if text.startswith("---") else ""
                if not re.search(r"^description:", fm, re.M):
                    findings.append(("WARN", "SKILL.md 缺 description 字段", p, ""))
                scan_text(text, p, findings)
                scan_external_urls(text, p, findings)
            for p in code_paths:
                text = _fetch_text(repo, p, branch)
                if text is None:
                    continue
                n_code += 1
                scan_text(text, p, findings)
                scan_external_urls(text, p, findings)
            if not skill_paths:
                findings.append(("INFO", "仓库内未发现 SKILL.md（可能不是技能仓库）", repo, ""))
            else:
                findings.append(("INFO",
                                 f"合集仓库：抽检 {n_md} 个 SKILL.md + {n_code} 个代码文件（封顶 15+5）",
                                 repo, ""))
        else:
            findings.append(("INFO", "根目录无 SKILL.md，树形列表也失败（仅覆盖元数据）", repo, ""))
    return findings, n_code, n_md, bool(lic)


def report(target, findings, n_code, n_md, has_license):
    levels = {f[0] for f in findings}
    verdict = "BLOCK" if "BLOCK" in levels else ("WARN" if "WARN" in levels else "PASS")
    print(f"LJC 安全检查：{target}")
    print(f"  代码文件 {n_code} 个 | SKILL.md {n_md} 个 | LICENSE {'有' if has_license else '无'}")
    print(f"  发现 {len(findings)} 项：\n")
    for level, name, where, sample in findings:
        mark = {"BLOCK": "[BLOCK]", "WARN": "[WARN ]", "INFO": "[INFO ]"}[level]
        print(f"  {mark} {name}")
        print(f"          {where}" + (f"  |  {sample}" if sample else ""))
    advice = {
        "PASS": "未发现风险项。可按流程推荐（仍建议读一遍 SKILL.md 原文）。",
        "WARN": "有需人工留意的项。照常告诉用户这个技能存在，推荐时附上以上警告，由用户定夺。",
        "BLOCK": "发现高危项。**不代用户否决**：照常告诉用户有这样一个东西，"
                 "逐条说明以上风险点与可能影响，是否使用由用户自行决定；"
                 "LJC 不代为下载、安装或执行。",
    }[verdict]
    print(f"\n  结论: {verdict} —— {advice}")
    print("  （启发式检查，已知会误报：讨论安全话题的技能自带关键词属正常，最终判断靠人）")
    return verdict


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    as_json = "--json" in sys.argv
    if not args:
        print(__doc__)
        return
    target = args[0]
    if os.path.isdir(target):
        findings, n_code, n_md, has_license = check_local(target)
    elif re.fullmatch(r"[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+", target):
        findings, n_code, n_md, has_license = check_github(target)
    else:
        print(f"目标既不是本地目录也不是 owner/repo: {target}")
        sys.exit(2)
    if as_json:
        print(json.dumps({"target": target, "findings": [
            {"level": l, "name": n, "where": w, "sample": s} for l, n, w, s in findings]},
            ensure_ascii=False, indent=1))
        return
    verdict = report(target, findings, n_code, n_md, has_license)
    sys.exit(2 if verdict == "BLOCK" else 0)


if __name__ == "__main__":
    main()
