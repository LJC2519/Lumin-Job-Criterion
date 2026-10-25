#!/usr/bin/env python3
"""lumin-job-criterion（LJC）· 任务技能路由器——本地检索脚本。

LJC = Lumin Job Criterion，触发语句「请ljc为我指点迷津」（大小写均可）。
本脚本只管本地侧：在技能目录里按功能检索。多根扫描（用户技能目录 +
WorkBuddy 内置技能 + 插件缓存最新版，可用环境变量 LJC_SKILLS_DIRS 覆盖）。
市场侧：GitHub 检索用同目录 github_search.py；SkillHub 语义搜索
由 LJC 流程直接 curl。

用法：
    python3 find.py build                 # 扫 ~/.workbuddy/skills/ 建索引
    python3 find.py query "画一个流程图"    # 检索（中英文均可）
    python3 find.py query "法律文书 信息抽取" --top 8
    python3 find.py suggest "把申报书数据合规章节加工成评委友好版"   # 任务盘点：相关 vs 用不到
    python3 find.py doctor               # 一键体检：索引/断链/根目录/通道
    python3 find.py list                  # 列出全部已索引技能

分工：本脚本管「已装的里有没有」（秒回、零网络）；
      已下载未注册的技能单独分区挑明，指出状态并照样介绍功能。
"""
import difflib
import json
import math
import os
import re
import sys
from collections import Counter

SKILLS_DIR = os.environ.get("LJC_SKILLS_DIR") or os.path.expanduser("~/.workbuddy/skills")
INDEX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "index.json")

# ── 已下载已安装、但未开放模型调用的技能 ──
# 检测机制（可移植、自维护，不写死名单）：build 时读每个技能 SKILL.md 的
# frontmatter，带 `disable-model-invocation: true` 的即标记
# invocation_disabled=True——作者禁用模型自动调用，Skill 工具加载不到。
# （2026-10-25 实测：19 个 mattpocock 系技能全部带此标志，已注册对照均无。）
# 查询时这类命中进「下载了，但没有注册」分区：挑明状态 + 照样介绍功能。

# ── 中文 → 英文检索词扩展（覆盖本项目高频场景）──
# 说明：技能的 description 绝大多数是英文；用户用中文提问时，
# 靠这张表把中文查询扩展成可匹配英文描述的检索词。
ZH_TO_EN = {
    "爬虫": ["scraping", "scrape", "crawl", "fetch", "spider"],
    "抓取": ["scraping", "scrape", "fetch", "extract", "download"],
    "抓网页": ["scraping", "fetch", "web"],
    "画图": ["diagram", "chart", "figure", "svg", "visual", "draw", "graph"],
    "画": ["draw", "diagram", "figure", "illustrat"],
    "图表": ["chart", "diagram", "figure", "graph", "visualization"],
    "流程图": ["diagram", "flowchart", "flow"],
    "图解": ["diagram", "figure", "illustration"],
    "插图": ["illustration", "figure", "art"],
    "论文": ["paper", "thesis", "manuscript", "writing", "academic"],
    "文章": ["writing", "article", "draft", "manuscript"],
    "写作": ["writing", "draft", "drafting"],
    "测试": ["test", "testing", "tdd"],
    "调试": ["debug", "debugging", "diagnos"],
    "除虫": ["debug", "debugging"],
    "审查": ["review", "audit"],
    "代码审查": ["review", "code review", "audit"],
    "检索": ["search", "retrieval", "find", "research"],
    "搜索": ["search", "osint", "find"],
    "调研": ["research", "investigation", "osint"],
    "规划": ["plan", "planning", "roadmap"],
    "计划": ["plan", "planning"],
    "文档": ["doc", "document", "docx", "word"],
    "幻灯片": ["ppt", "pptx", "presentation", "slides"],
    "演示": ["presentation", "slides", "demo"],
    "表格": ["sheet", "excel", "xlsx", "spreadsheet", "table"],
    "数据": ["data", "dataset", "dataframe"],
    "清洗": ["cleaning", "clean", "process"],
    "分析": ["analysis", "analytics", "analyze"],
    "知识图谱": ["knowledge graph", "kg", "graph", "graphify"],
    "安全": ["security", "secure", "vulnerab"],
    "漏洞": ["security", "vulnerability", "audit"],
    "正则": ["regex", "pattern", "regular expression"],
    "法律": ["legal", "law", "compliance"],
    "合规": ["compliance", "legal", "customs"],
    "海关": ["customs", "trade"],
    "专利": ["patent", "disclosure"],
    "软著": ["software copyright", "copyright"],
    "答辩": ["presentation", "defense", "interview"],
    "面试": ["interview", "presentation"],
    "安装": ["install", "marketplace", "add"],
    "技能": ["skill", "skills"],
    "重构": ["refactor", "architecture", "redesign"],
    "架构": ["architecture", "design", "structure"],
    "逆向": ["reverse engineer", "rea", "decompile"],
    "研究": ["research", "study"],
    "文献": ["literature", "paper", "review"],
    "引用": ["citation", "cite", "reference"],
    "图表设计": ["figure", "editorial", "design", "diagram"],
    "记忆": ["memory", "context", "handoff"],
    "交接": ["handoff", "context"],
    "定时": ["schedule", "scheduled", "cron", "periodic"],
    "监控": ["monitor", "watch", "track", "watch"],
    "监测": ["monitor", "watch", "track"],
    "更新": ["update", "change", "refresh", "diff"],
    "网站": ["web", "site", "website"],
    "网页": ["web", "page", "html"],
    "邮件": ["mail", "email"],
    "浏览器": ["browser", "chrome", "puppeteer"],
    "自动化": ["automation", "workflow", "rpa"],
    "转换": ["convert", "conversion", "migration", "migrate", "transform"],
    "转": ["convert", "transform", "conversion"],
    "视频": ["video", "frame", "render"],
    "图像": ["image", "photo", "picture"],
    "图片": ["image", "photo"],
    "3d": ["3d", "model", "mesh"],
    "建模": ["model", "modeling", "domain"],
    "git": ["git", "commit", "branch", "pr"],
    "提交": ["commit", "git"],
    "分支": ["branch", "git", "worktree"],
    "学习": ["learn", "study", "exercise", "scaffold"],
    "练习": ["exercise", "scaffold", "practice"],
    "创意": ["idea", "brainstorm", "generation", "critique"],
    "头脑风暴": ["brainstorm", "idea", "ideation"],
    "评审": ["review", "critique", "evaluate"],
    "汇报": ["report", "presentation", "summary"],
    "报告": ["report", "reporting"],
    "排版": ["typesetting", "format", "latex"],
    "幻灯片模板": ["template", "pptx", "presentation"],
    "部署": ["deploy", "publish", "release"],
    "发布": ["publish", "deploy", "release"],
    "模板": ["template", "boilerplate", "scaffold"],
    "原型": ["prototype", "mockup", "prototyp"],
    "界面": ["frontend", "ui", "interface", "design"],
    "前端": ["frontend", "ui", "css", "html"],
    "提示词": ["prompt", "instruction"],
    "智能体": ["agent", "agents"],
    "子代理": ["subagent", "agent", "parallel", "dispatch"],
    "并行": ["parallel", "concurrent", "dispatch"],
    "工作流": ["workflow", "pipeline"],
    "管道": ["pipeline", "workflow"],
    # ── 项目域词汇（2026-10-25 补：东盟出海法律风险预警系统）──
    # 补修前的实测缺口：「法律文书 信息抽取 结构化」查询因这些词零映射，
    # 漏掉了最相关的 regex-vs-llm-structured-text 等技能。
    "文书": ["document", "case", "judgment", "ruling", "legal document"],
    "抽取": ["extract", "extraction", "parse", "information extraction"],
    "提取": ["extract", "extraction"],
    "标注": ["annotation", "label", "labeling", "annotate"],
    "法条": ["statute", "law", "article", "provision", "legal"],
    "法规": ["regulation", "statute", "law", "legal", "act"],
    "义务": ["obligation", "duty", "requirement", "compliance"],
    "变更": ["change", "diff", "modification", "amendment"],
    "比对": ["compare", "comparison", "diff", "alignment"],
    "申报书": ["proposal", "submission", "application", "writing"],
    "申报": ["submission", "declare", "report", "filing"],
    "雷达": ["radar", "monitor", "watch", "alert"],
    "预警": ["alert", "warning", "early warning", "risk"],
    "风险": ["risk", "hazard"],
    "态势": ["situation", "landscape", "analysis", "overview"],
    "出海": ["overseas", "cross-border", "going global", "export"],
    "跨境": ["cross-border", "transborder", "cross border"],
    "案例": ["case", "cases", "case law"],
    "裁判": ["judgment", "ruling", "adjudication", "court"],
    "判决": ["judgment", "ruling", "verdict"],
    "条款": ["clause", "article", "provision", "term"],
    "条文": ["article", "provision", "statute"],
    "条约": ["treaty", "convention", "agreement"],
    "协定": ["agreement", "treaty", "pact"],
    "隐私": ["privacy", "personal data", "pii"],
    "数据保护": ["data protection", "privacy", "personal data"],
    "网络安全": ["cybersecurity", "network security", "security"],
    "人工智能": ["ai", "artificial intelligence", "llm", "model"],
    "模型": ["model", "llm", "machine learning"],
    "评测": ["evaluation", "benchmark", "eval"],
    "情报": ["intelligence", "osint", "information"],
    "结构化": ["structured", "structure", "schema", "extraction"],
    "队友": ["team", "collaboration", "handoff"],
    "分工": ["division", "team", "assignment"],
}


def parse_frontmatter(text):
    """极简 frontmatter 解析：只取 --- 之间的 key: value（不引 yaml 依赖）。
    支持 YAML 块标量：description: >- / | （后续缩进行收集）。"""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    lines = text[3:end].strip().split("\n")
    meta = {}
    i = 0
    while i < len(lines):
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$", lines[i])
        if not m:
            i += 1
            continue
        key, val = m.group(1), m.group(2).strip()
        if val in (">-", ">", "|-", "|"):
            # 块标量：收集后续缩进行
            block = []
            i += 1
            while i < len(lines) and (lines[i].startswith(" ") or lines[i].startswith("\t")):
                block.append(lines[i].strip())
                i += 1
            meta[key] = " ".join(b for b in block if b) if val.startswith(">") else "\n".join(block)
            continue
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        meta[key] = val
        i += 1
    return meta


def guess_source(real_path):
    """从真实路径推断来源仓库（与 MEMORY.md 的五大来源口径对齐）。"""
    if "/skill-sources/" in real_path:
        after = real_path.split("/skill-sources/", 1)[1]
        return after.split("/", 1)[0]
    if "/ecc-study/" in real_path:
        return "ecc-study"
    if "/light-skills/" in real_path:
        return "light-skills"
    if "/supervisor-skills/" in real_path:
        return "supervisor-skills"
    if "/workbuddy-builtin/skills/" in real_path:
        return "workbuddy-builtin"
    if "/plugins/cache/" in real_path:
        return "plugin"
    return "local"


def pick_latest_version(versions, plugin_dir=None):
    """同插件多版本取最新：版本号数字段优先（5.8.3 > 5.8.2），
    无数字段时 mtime 兜底。纯函数（plugin_dir 仅作平级比较用），可测试。"""
    def key(v):
        nums = [int(x) for x in re.findall(r"\d+", v)[:3]]
        mt = os.path.getmtime(os.path.join(plugin_dir, v)) if plugin_dir else 0
        return (nums, mt)
    return max(versions, key=key)


def default_roots():
    """扫描根列表（2026-10-25 扩为多根）：用户技能目录 + WorkBuddy 内置技能
    + 插件缓存（cache/<org>/<plugin>/<version>/skills，每插件只取最新版，
    避免同插件多版本重复索引）。
    环境变量 LJC_SKILLS_DIRS（冒号分隔）可完全覆盖默认列表。"""
    env = os.environ.get("LJC_SKILLS_DIRS")
    if env:
        return [os.path.expanduser(p) for p in env.split(":") if p.strip()]
    roots = [SKILLS_DIR]
    builtin = ("/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/"
               "resources/plugins/workbuddy-builtin/skills")
    if os.path.isdir(builtin):
        roots.append(builtin)
    cache = os.path.expanduser("~/.workbuddy/plugins/cache")
    if os.path.isdir(cache):
        for org in sorted(os.listdir(cache)):
            org_dir = os.path.join(cache, org)
            if not os.path.isdir(org_dir):
                continue
            for plugin in sorted(os.listdir(org_dir)):
                plugin_dir = os.path.join(org_dir, plugin)
                if not os.path.isdir(plugin_dir):
                    continue
                versions = [v for v in os.listdir(plugin_dir)
                            if os.path.isdir(os.path.join(plugin_dir, v, "skills"))]
                if not versions:
                    continue
                latest = pick_latest_version(versions, plugin_dir)
                roots.append(os.path.join(plugin_dir, latest, "skills"))
    return roots


def build_index():
    roots = default_roots()
    entries = []
    broken = []
    seen = {}
    collisions = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            path = os.path.join(root, name)
            real = os.path.realpath(path)
            if not os.path.isdir(real):
                if os.path.islink(path):
                    broken.append(name)
                continue
            skill_md = None
            for cand in (os.path.join(real, "SKILL.md"),):
                if os.path.isfile(cand):
                    skill_md = cand
                    break
            if not skill_md:
                # 个别技能 SKILL.md 不在根（如套件结构），向下找一层
                for root_, dirs, files in os.walk(real):
                    if "SKILL.md" in files:
                        skill_md = os.path.join(root_, "SKILL.md")
                        break
                    dirs[:] = [d for d in dirs if not d.startswith(".") and d != "node_modules"]
                    if root_.count(os.sep) - real.count(os.sep) >= 2:
                        dirs[:] = []
            if not skill_md:
                continue
            try:
                text = open(skill_md, encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            meta = parse_frontmatter(text)
            fm_end = text.find("\n---", 3)
            fm_text = text[:fm_end] if fm_end != -1 else text[:2000]
            invocation_disabled = is_invocation_disabled(fm_text)
            entry = {
                "name": meta.get("name", name),
                "dir": name,
                "path": real,
                "root": root,
                "source": guess_source(real),
                "description": meta.get("description", ""),
                "description_zh": meta.get("description_zh", ""),
                "version": meta.get("version", ""),
                "is_symlink": os.path.islink(path),
                "invocation_disabled": invocation_disabled,
            }
            if entry["name"] in seen:
                # 同名技能去重：先到的根优先（用户目录排在 roots 最前）
                collisions.append((entry["name"], entry["source"], seen[entry["name"]]))
                continue
            seen[entry["name"]] = entry["source"]
            entries.append(entry)
    index = {
        "built_at": __import__("time").strftime("%Y-%m-%d %H:%M"),
        "n_skills": len(entries),
        "n_broken": len(broken),
        "broken": broken,
        "roots": roots,
        "skills": entries,
    }
    out = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "index.json"))
    with open(out, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=1)
    print(f"索引已建：{out}")
    print(f"  已索引技能：{len(entries)} 个（扫描 {len(roots)} 个根）")
    for r in roots:
        print(f"    · {r}")
    if broken:
        print(f"  ⚠️ 失效链接：{len(broken)} 个 → {', '.join(broken)}")
    if collisions:
        shown = ", ".join(f"{n}({a}⊂{b})" for n, a, b in collisions[:6])
        print(f"  ⚠️ 同名去重 {len(collisions)} 个（先到根优先）：{shown}")
    by_source = {}
    for e in entries:
        by_source.setdefault(e["source"], []).append(e["name"])
    print("  按来源：")
    for src, names in sorted(by_source.items(), key=lambda kv: -len(kv[1])):
        print(f"    {src}: {len(names)}")
    return index


def load_index():
    path = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "index.json"))
    if not os.path.isfile(path):
        print("索引不存在，先跑 build", file=sys.stderr)
        sys.exit(2)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_extra_terms(path=None):
    """用户自定义术语映射——发布后别人扩自己领域的词，不用改脚本。
    文件：脚本同目录 ljc_terms.json，格式 {"中文词": ["en1", "en2"], ...}。
    不存在/读失败 → {}（内置映射不受影响）。"""
    p = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "ljc_terms.json")
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {str(k): [str(x) for x in v]
                        for k, v in data.items() if isinstance(v, list)}
        except Exception:
            return {}
    return {}


def expand_query(query, extra_terms=None):
    """把查询扩展成英文检索词集合（中文查映射表，英文词原样保留）。
    中英混排（如「pdf转word」）先在中英边界切开再分词。
    extra_terms：用户自定义映射（None=自动加载 ljc_terms.json，优先于内置）。"""
    q = query.lower().strip()
    terms = set()
    # 0) 中英边界切开：pdf转word → pdf 转 word
    q = re.sub(r"([a-z0-9])([\u4e00-\u9fff])", r"\1 \2", q)
    q = re.sub(r"([\u4e00-\u9fff])([a-z0-9])", r"\1 \2", q)
    # 1) 查询本身（去标点）
    cleaned = re.sub(r"[，。！？、；：「」『』（）\s]+", " ", q).strip()
    if cleaned:
        terms.add(cleaned)
        for w in cleaned.split():
            if len(w) >= 2:
                terms.add(w)
    # 2) 中文关键词 → 英文扩展（长词优先，避免「画图」被「图」抢先）
    mapping = dict(ZH_TO_EN)
    if extra_terms is None:
        extra_terms = load_extra_terms()
    mapping.update(extra_terms)
    for zh, ens in sorted(mapping.items(), key=lambda kv: -len(kv[0])):
        if zh in q:
            terms.update(ens)
    return terms


def is_invocation_disabled(fm_text):
    """frontmatter 带 disable-model-invocation: true → 作者禁用模型自动调用。"""
    return bool(re.search(r"disable-model-invocation\s*:\s*true", fm_text or ""))


def score_entry(entry, terms):
    name = entry["name"].lower()
    desc = (entry.get("description") or "").lower()
    desc_zh = entry.get("description_zh") or ""
    score = 0
    hits = []
    matched_terms = set()
    for t in terms:
        t = t.lower()
        if not t:
            continue
        hit_this = False
        if t == name:
            score += 100
            hits.append(f"name={t}")
            hit_this = True
        elif re.search(r"(?:^|[-_ ])" + re.escape(t) + r"(?:$|[-_ ])", name):
            score += 40
            hits.append(f"name~{t}")
            hit_this = True
        elif t in name:
            score += 15  # 子串命中降权（flow 不该抢 workflow 的分）
            hits.append(f"name⊂{t}")
            hit_this = True
        if t in desc:
            score += 15
            hits.append(f"desc~{t}")
            hit_this = True
        if desc_zh and t in desc_zh:
            score += 20
            hits.append(f"desc_zh~{t}")
            hit_this = True
        if hit_this:
            matched_terms.add(t)
    # 模糊兜底：未命中的长词与技能名分段近似匹配（拼写容错，cutoff 0.8）
    name_segs = [s for s in re.split(r"[-_. ]", name) if len(s) >= 4]
    for t in sorted({str(x).lower() for x in terms
                     if len(str(x)) >= 4 and str(x).lower() not in matched_terms}):
        m = difflib.get_close_matches(t, name_segs, n=1, cutoff=0.8)
        if m:
            score += 10
            hits.append(f"fuzzy~{t}≈{m[0]}")
            matched_terms.add(t)
    # 多词覆盖加成：任务查询是多个词的组合，覆盖的不同检索词越多，
    # 越可能覆盖任务的更多侧面——不应被单个词的强命中（如 name）压过。
    if len(matched_terms) >= 2:
        bonus = 30 * (len(matched_terms) - 1)
        score += bonus
        hits.append(f"覆盖{len(matched_terms)}词+{bonus}")
    return score, hits


def _entry_tokens(entry):
    """技能的词袋：name + 双语 description，小写英文数字词。"""
    text = " ".join([entry.get("name", ""), entry.get("description", ""),
                     entry.get("description_zh", "")]).lower()
    return re.findall(r"[a-z0-9]{2,}", text)


def semantic_scores(entries, terms):
    """纯标准库 TF-IDF 余弦相似度——关键词匹配之外的语义补充信号。
    词袋级（非向量嵌入），但能捕捉「查询词不在描述里、近义词在」的情况。
    返回与 entries 等长的 0~1 相似度列表。"""
    if not entries or not terms:
        return [0.0] * len(entries)
    docs = [Counter(_entry_tokens(e)) for e in entries]
    n = len(docs)
    df = Counter()
    for doc in docs:
        for t in doc:
            df[t] += 1
    idf = {t: math.log(n / c) + 1.0 for t, c in df.items()}
    q = Counter(t for t in terms)
    qnorm = math.sqrt(sum((c * idf.get(t, 1.0)) ** 2 for t, c in q.items())) or 1.0
    scores = []
    for doc in docs:
        dot = sum(qc * idf.get(t, 1.0) * dc * idf.get(t, 1.0)
                  for t, qc in q.items() if (dc := doc.get(t)))
        dnorm = math.sqrt(sum((dc * idf.get(t, 1.0)) ** 2 for t, dc in doc.items())) or 1.0
        scores.append(dot / (qnorm * dnorm))
    return scores


def cmd_query(query, top=10):
    index = load_index()
    warn_if_stale(index)
    terms = expand_query(query)
    if not terms:
        print("查询为空")
        return
    scored = []
    hidden = []
    sem = semantic_scores(index["skills"], terms)
    for i, e in enumerate(index["skills"]):
        s, hits = score_entry(e, terms)
        sem_pts = int(round(min(sem[i] * 30, 30)))
        if s <= 0:
            if sem_pts >= 8:  # 关键词零命中但语义信号强 → 低分候选（扩召回）
                s, hits = sem_pts, [f"tfidf≈{sem[i]:.2f}"]
            else:
                continue
        else:
            s += sem_pts
            if sem_pts >= 5:
                hits.append(f"tfidf+{sem_pts}")
        if e.get("invocation_disabled"):
            hidden.append((s, hits, e))
            continue
        scored.append((s, hits, e))
    scored.sort(key=lambda x: (-x[0], x[2]["name"]))
    hidden.sort(key=lambda x: -x[0])
    n_total = index.get("n_skills", len(index["skills"]))

    def print_hidden():
        """已下载未注册的命中：挑明状态 + 照样介绍功能（用户明确要求）。"""
        if not hidden:
            return
        print("── 下载了，但没有注册（作者禁用了模型自动调用，本会话 Skill 工具加载不到）──")
        print("   功能介绍如下；确有用处可找用户确认后开放（移除其 SKILL.md 的 disable-model-invocation 标志）。")
        for i, (s, hits, e) in enumerate(hidden[:top], 1):
            desc = (e.get("description") or e.get("description_zh") or "（无描述）")
            if len(desc) > 150:
                desc = desc[:150] + "…"
            print(f" U{i}. [{e['name']}]  (来源:{e['source']}  得分:{s})")
            print(f"    {desc}")
            if hits:
                print(f"    命中: {', '.join(hits[:6])}")
            print()
        if len(hidden) > top:
            print(f" （另有 {len(hidden) - top} 个未注册命中未展开）\n")

    if not scored:
        print(f"本地 {n_total} 个已安装技能中，没有匹配「{query}」的可调用技能。")
        print("→ 下一步：走官方 find-skills 技能搜市场（SkillHub），")
        print("  或让我直接用通用能力帮你做。\n")
        print_hidden()
        return
    print(f"查询「{query}」→ 扩展检索词 {len(terms)} 个：{len(scored)} 个可调用 + {len(hidden)} 个未注册命中。\n")
    for i, (s, hits, e) in enumerate(scored[:top], 1):
        src = e["source"]
        desc = (e.get("description") or e.get("description_zh") or "（无描述）")
        if len(desc) > 150:
            desc = desc[:150] + "…"
        print(f"{i:2d}. [{e['name']}]  (来源:{src}  得分:{s})")
        print(f"    {desc}")
        if hits:
            print(f"    命中: {', '.join(hits[:6])}")
        print()
    print_hidden()


def warn_if_stale(index):
    """索引新鲜度自检：任一扫描根在索引建成后有变动就提醒。"""
    try:
        import time as _t
        roots = index.get("roots") or [SKILLS_DIR]
        newest = 0
        for root in roots:
            if not os.path.isdir(root):
                continue
            newest = max(newest, max(
                os.path.getmtime(os.path.join(root, n)) for n in os.listdir(root)))
        built = _t.mktime(_t.strptime(index.get("built_at", ""), "%Y-%m-%d %H:%M"))
        if newest - built > 60:
            print(f"⚠️ 索引建于 {index.get('built_at')}，技能目录此后有变动——建议先跑一次 build。\n")
    except Exception:
        pass


def cmd_suggest(task, top=10):
    """任务视角的技能盘点：相关候选 vs 本次用不到（可暂时关掉的候选）。"""
    index = load_index()
    warn_if_stale(index)
    terms = expand_query(task)
    if not terms:
        print("任务描述为空")
        return
    useful, unused = [], []
    sem = semantic_scores(index["skills"], terms)
    for i, e in enumerate(index["skills"]):
        if e.get("invocation_disabled"):
            continue  # 未注册的本来就不可调用，不参与停用建议
        s, hits = score_entry(e, terms)
        s += int(round(min(sem[i] * 30, 30)))
        if s > 0:
            useful.append((s, hits, e))
        else:
            unused.append(e)
    useful.sort(key=lambda x: (-x[0], x[2]["name"]))
    print(f"任务「{task}」→ 扩展检索词 {len(terms)} 个：{len(useful)} 个相关候选，{len(unused)} 个本次用不到。\n")
    print(f"── 任务相关候选（{len(useful)} 个，按相关度排序，仍需读原文核实）──\n")
    for i, (s, hits, e) in enumerate(useful[:top], 1):
        desc = (e.get("description") or e.get("description_zh") or "（无描述）")
        if len(desc) > 120:
            desc = desc[:120] + "…"
        print(f"{i:2d}. [{e['name']}]  (来源:{e['source']}  得分:{s})")
        print(f"    {desc}")
        print(f"    命中: {', '.join(hits[:5])}\n")
    if len(useful) > top:
        print(f" （另有 {len(useful) - top} 个相关候选未展开）\n")
    print(f"── 本次任务用不到（{len(unused)} 个）——「可暂时关掉」的候选 ──")
    print("   注意：用不到≠没用，只是这个任务不需要；关哪些、关不关由用户定。\n")
    by_source = {}
    for e in unused:
        by_source.setdefault(e["source"], []).append(e["name"])
    for src, names in sorted(by_source.items(), key=lambda kv: -len(kv[1])):
        shown = ", ".join(names[:12]) + ("……" if len(names) > 12 else "")
        print(f"   {src} ({len(names)}): {shown}")
    print("\n   停用路径：用户同意后走 skill_management disable（WorkBuddy）；")
    print("   恢复=用户去技能设置里重新打开。本脚本只读，永不自作主张停用。")


def cmd_list():
    index = load_index()
    by_source = {}
    for e in index["skills"]:
        by_source.setdefault(e["source"], []).append(e)
    for src in sorted(by_source, key=lambda s: -len(by_source[s])):
        print(f"\n== {src} ({len(by_source[src])}) ==")
        for e in by_source[src]:
            d = (e.get("description_zh") or e.get("description") or "")[:60]
            print(f"  {e['name']:38s} {d}")


def cmd_doctor():
    """一键体检：索引新鲜度 / 失效链接 / 扫描根 / GitHub 连通 / 目录缓存。"""
    import time as _t
    import urllib.request
    print("LJC doctor —— 自检")
    problems = []
    idx_file = os.path.abspath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "index.json"))
    if not os.path.isfile(idx_file):
        print(" ✗ 索引不存在 → python3 find.py build")
        print("\n结论：1 项待处理 → index-missing")
        return
    index = load_index()
    try:
        built = _t.mktime(_t.strptime(index.get("built_at", ""), "%Y-%m-%d %H:%M"))
        age = (_t.time() - built) / 60
        mark = "✓" if age < 120 else "⚠"
        print(f" {mark} 索引：{index.get('n_skills')} 个技能，"
              f"建于 {index.get('built_at')}（{age:.0f} 分钟前）")
        if age >= 120:
            problems.append("index-stale")
    except Exception:
        problems.append("index-unreadable")
        print(" ⚠ 索引 built_at 无法解析")
    if index.get("n_broken"):
        print(f" ✗ 失效符号链接 {index['n_broken']} 个："
              f"{', '.join(index.get('broken', []))}")
        problems.append("broken-links")
    for root in index.get("roots", []):
        if not os.path.isdir(root):
            print(f" ✗ 扫描根不存在：{root}")
            problems.append("root-missing")
    try:
        urllib.request.urlopen(
            urllib.request.Request("https://github.com", method="HEAD"), timeout=6)
        print(" ✓ GitHub 可达（市场通道可用）")
    except Exception as e:
        print(f" ⚠ GitHub 暂不可达（{e}）——市场通道受影响，本地检索不受影响")
    cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
    if os.path.isdir(cache_dir):
        n = len([f for f in os.listdir(cache_dir)
                 if f.endswith(".md") or f.endswith(".json")])
        print(f" ✓ 技能目录缓存：{n} 个文件（7 天 TTL，--refresh 强刷）")
    else:
        print(" · 技能目录缓存：空（首次 --catalog 会抓取）")
    print("\n结论：" + (f"发现 {len(problems)} 项待处理 → {', '.join(problems)}"
                       if problems else "全部正常"))


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == "build":
        build_index()
    elif cmd == "query":
        if len(sys.argv) < 3:
            print("用法: find.py query \"关键词\" [--top N]")
            return
        top = 10
        if "--top" in sys.argv:
            i = sys.argv.index("--top")
            top = int(sys.argv[i + 1])
            query = " ".join(sys.argv[2:i])
        else:
            query = " ".join(sys.argv[2:])
        cmd_query(query, top)
    elif cmd == "suggest":
        if len(sys.argv) < 3:
            print("用法: find.py suggest \"任务描述\" [--top N]")
            return
        top = 10
        if "--top" in sys.argv:
            i = sys.argv.index("--top")
            top = int(sys.argv[i + 1])
            task = " ".join(sys.argv[2:i])
        else:
            task = " ".join(sys.argv[2:])
        cmd_suggest(task, top)
    elif cmd == "doctor":
        cmd_doctor()
    elif cmd == "list":
        cmd_list()
    else:
        print(f"未知命令: {cmd}\n")
        print(__doc__)


if __name__ == "__main__":
    main()
