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
  python3 security_check.py <本地目录> --deep       # 追加 NVIDIA SkillSpector 深扫（可选层）

设计说明：
  - 脚本（.py/.js/.ts/.sh）查可执行风险；SKILL.md 查提示词注入与凭证
  - 已知误报类：合法技能讨论安全话题会自带关键词（如审计类技能），
    结论供人判断，机器只负责不漏报
  - 本地已装候选的深度审计可另跑 jason-security-audit 技能的 audit.py（若装着）
  - --deep：调用 NVIDIA SkillSpector（★22.5k，71 漏洞模式/17 类，2026-10-27 接入）。
    检测到 skillspector 可执行文件才启用，否则提示并回退内置正则——
    软依赖纪律（同 LJC_SEMANTIC）：不装不影响默认行为，装了就升级到 0-100 风险分。
    仅支持本地目录（GitHub 目标需先 clone，不代做）。注意：macOS 上 SkillSpector
    v2.12.0 有平台 bug（os.open 不在 supports_dir_fd 导致全部文件被拒），
    本机安装副本已打运行时探测补丁，上游修复后此备注可删。
"""
import json
import os
import re
import shutil
import subprocess
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
    # —— 2026-10-27 对标 NVIDIA SkillSpector 71 模式/17 类补缺（原 16 条 → 覆盖反拒绝、
    #    数据外渗、提示词泄漏、供应链安装钩子、记忆投毒五类）——
    ("反拒绝：试图解除模型安全限制", re.compile(
        r"(?i)(do\s+not\s+refuse|never\s+refuse|bypass\s+(the\s+)?(safety|content|guard)"
        r"|ignore\s+your\s+safety)"), "any"),
    ("数据外渗：读环境凭证并外传", re.compile(
        r"(?i)(environ|os\.getenv|api[_-]?key|token).{0,120}(https?://|curl\s|wget\s|"
        r"requests\.(get|post)|urlopen)"), "script"),
    ("提示词泄漏：诱导复述系统提示", re.compile(
        r"(?i)(reveal|show|print|output|repeat|verbatim)\s+(me\s+)?(your|the)\s+"
        r"(system\s+)?(prompt|instructions)"), "any"),
    ("供应链：安装钩子执行远程脚本", re.compile(
        r"(?i)[\"'](preinstall|postinstall|prepare)[\"']\s*:\s*[\"'].*(curl|wget|npx\b)"), "any"),
    ("记忆投毒：写入 agent 配置/记忆文件", re.compile(
        r"(?i)(inject|overwrite|append|write\w*)\s+(to\s+|into\s+)?"
        r"(CLAUDE\.md|AGENTS\.md|\.cursorrules|MEMORY\.md)"), "any"),
]

WARN_PATTERNS = [
    ("脚本含网络调用（curl/wget）", re.compile(r"\b(curl|wget)\s"), "script"),
    ("os.system 调用", re.compile(r"os\.system\s*\("), "script"),
    ("subprocess shell=True", re.compile(r"subprocess\.[A-Za-z]+\([^)]*shell\s*=\s*True"), "script"),
    ("Node child_process.exec", re.compile(r"child_process\.exec"), "script"),
    ("超长单行（>20000 字符，混淆嫌疑）", None, "longline"),
    ("脚本含裸 eval（动态执行）", re.compile(r"(?<![.\w])eval\s*\("), "script"),
    ("引用私钥/凭证文件路径", re.compile(r"(id_rsa|id_ed25519|\.aws[/\\]credentials|\.netrc)"), "any"),
    # —— 对标补缺（WARN 档）——
    ("尝试访问 agent 配置/凭据目录", re.compile(
        r"\.claude[/\\]|\.cursor[/\\]|\.workbuddy[/\\]|\.codex[/\\]"), "any"),
    ("请求提权（sudo）", re.compile(r"(?m)^\s*sudo\s|(?<![a-z])sudo\s+(rm|mv|cp|chown|chmod)"), "script"),
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


DEEP_SKIP_PREFIXES = (".git/", "__pycache__/", "scripts/.cache/", ".cache/")


def run_deep_skillspector(path):
    """--deep 层：调用 NVIDIA SkillSpector（若装有 CLI）。

    返回 (findings, meta)——findings 追加格式与内置一致；meta 给报告页。
    任何失败（未安装/超时/macOS 平台 bug 未打补丁）都只降级不崩溃：
    返回 (None, 原因)，调用方回退内置结果。
    """
    exe = shutil.which("skillspector")
    if not exe:
        return None, "未检测到 skillspector（uv tool install NVIDIA/skillspector 可装）"
    try:
        proc = subprocess.run(
            [exe, "scan", ".", "--no-llm", "--format", "json"],
            cwd=path, capture_output=True, text=True, timeout=240)
    except subprocess.TimeoutExpired:
        return None, "skillspector 扫描超时（240s），回退内置结果"
    # 注意：扫描器惯例——发现问题时退出码非 0。只要 stdout 是合法 JSON 就算成功。
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None, f"skillspector 退出码 {proc.returncode} 且无 JSON 输出，回退内置结果"
    ra = data.get("risk_assessment") or {}
    meta = {"score": ra.get("score"), "raw_issues": 0, "kept": 0}
    findings = []
    issues = (data.get("issues") or data.get("findings") or [])
    for it in issues:
        meta["raw_issues"] += 1
        loc = str(it.get("location") or it.get("path") or "")
        if any(loc.startswith(p) or f"/{p}" in loc for p in DEEP_SKIP_PREFIXES):
            continue  # 扫描范围噪音：.git / 缓存 / 第三方内容不记入
        meta["kept"] += 1
        sev = str(it.get("severity") or it.get("level") or "").upper()
        name = f"[SkillSpector:{it.get('rule_id', it.get('id', '?'))}] {it.get('message', '')[:80]}"
        where = loc or "(deep)"
        if sev in ("CRITICAL",):
            findings.append(("BLOCK", name, where, ""))
        elif sev in ("HIGH",):
            findings.append(("WARN", name, where, ""))
        else:
            findings.append(("INFO", name, where, ""))
    score = meta.get("score")
    if isinstance(score, (int, float)) and score >= 50:
        # 风险分 0-100，≥50 属 SkillSpector 的高危带（其建议即 DO NOT INSTALL）
        findings.append(("BLOCK", f"[SkillSpector] 总风险分 {score}/100（≥50 高危带）", "(deep)", ""))
    elif isinstance(score, (int, float)) and score >= 25 and not any(f[0] == "WARN" for f in findings):
        findings.append(("WARN", f"[SkillSpector] 总风险分 {score}/100", "(deep)", ""))
    return findings, meta


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
    deep = "--deep" in sys.argv
    if not args:
        print(__doc__)
        return
    target = args[0]
    deep_note = None
    if os.path.isdir(target):
        findings, n_code, n_md, has_license = check_local(target)
        if deep:
            deep_findings, deep_note = run_deep_skillspector(target)
            if deep_findings is not None:
                findings.extend(deep_findings)
    elif re.fullmatch(r"[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+", target):
        findings, n_code, n_md, has_license = check_github(target)
        if deep:
            deep_note = "--deep 仅支持本地目录（GitHub 目标需先 clone，LJC 不代做）"
    else:
        print(f"目标既不是本地目录也不是 owner/repo: {target}")
        sys.exit(2)
    if as_json:
        print(json.dumps({"target": target, "findings": [
            {"level": l, "name": n, "where": w, "sample": s} for l, n, w, s in findings]},
            ensure_ascii=False, indent=1))
        return
    verdict = report(target, findings, n_code, n_md, has_license)
    if deep:
        if isinstance(deep_note, dict):
            print(f"  [deep] SkillSpector 深扫完成：风险分 {deep_note.get('score')}，"
                  f"保留 {deep_note.get('kept')}/{deep_note.get('raw_issues')} 条"
                  f"（已滤除 .git/.cache/缓存噪音）")
        elif deep_note:
            print(f"  [deep] {deep_note}")
    sys.exit(2 if verdict == "BLOCK" else 0)


if __name__ == "__main__":
    main()
