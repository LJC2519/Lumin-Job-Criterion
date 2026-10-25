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
    python3 find.py semantic             # 语义后端自检（探测到就用，没探测到说明怎么开）
    python3 find.py list                  # 列出全部已索引技能

分工：本脚本管「已装的里有没有」（秒回、零网络）；
      已下载未注册的技能单独分区挑明，指出状态并照样介绍功能。
"""
import difflib
import hashlib
import importlib
import importlib.util
import json
import math
import os
import re
import subprocess
import sys
from collections import Counter

SKILLS_DIR = os.environ.get("LJC_SKILLS_DIR") or os.path.expanduser("~/.workbuddy/skills")
INDEX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "index.json")
HISTORY_PATH = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hits_history.jsonl"))
EMBED_CACHE_PATH = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".cache", "embeddings.json"))

# ── 可选语义后端（软依赖：探测得到就用，探测不到就回退词袋）──
# 背景：内置 semantic_scores() 是纯标准库 TF-IDF，属「词袋级」——比较字符重叠，
# 不懂含义。查询「帮我找个能审合同的」与描述「review agreement risk」字符零重叠，
# 词袋打 0 分。真语义需要把文字变成向量（嵌入），那需要第三方库或云端 API。
# 本项目的取舍（用户 2026-10-26 定）：
#   · find.py **永不 import 任何第三方库**——纯净环境 clone 下来就能跑；
#   · 但如果你环境里**已经装了**嵌入能力，就自动用上（真语义召回）；
#   · 没装 → 静默回退 TF-IDF 词袋，行为与今天完全一致，零报错、零依赖。
# 环境变量（全部可选）：
#   LJC_SEMANTIC=auto|off|local|cloud|external  默认 auto
#   LJC_SEMANTIC_MODEL=...                       覆盖默认模型名
#   LJC_SEMANTIC_CMD="..."                       外部嵌入命令：stdin 收 JSON 字符串数组，
#                                                stdout 吐 JSON 二维数组（每行一个向量）
# 支持的三类后端：
#   local    sentence-transformers / fastembed（本机模型，离线可跑）
#   cloud    openai SDK + OPENAI_API_KEY（联网、有隐私代价，按次计费）
#   external 任意可执行命令（你自己接的模型/服务，脚本零依赖接入）

# ── 已下载已安装、但未开放模型调用的技能 ──
# 检测机制（可移植、自维护，不写死名单）：build 时读每个技能 SKILL.md 的
# frontmatter，带 `disable-model-invocation: true` 的即标记
# invocation_disabled=True——作者禁用模型自动调用，Skill 工具加载不到。
# （2026-10-25 实测：mattpocock 系 19 个全部带此标志，已注册对照均无；
#   同日经用户同意开放 to-spec/to-tickets 及 mattpocock 系其余 14 个——
#   仅 setup-ts-deep-modules 仍带（TS 专用、领域不符）；
#   平台内部管线技能（ardot/miora/doc-pipeline 等 23 个）按平台设计保持关闭。）
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
    # ── 阅读与复核（2026-10-25 补：实测「复核 验证 检查清单」查询漏掉
    # verification-loop / verification-before-completion 等验证组技能）──
    "复核": ["verification", "verify", "recheck", "double-check", "review"],
    "阅读": ["reading", "read", "comprehension", "understanding"],
    "长文本": ["long document", "long text", "lengthy document"],
    "检查清单": ["checklist", "check list", "checklist review"],
    "遗漏": ["missing", "omission", "gap", "overlooked"],
    # ── 合同/审查域（2026-10-26 补：实测「审合同」零命中的教训——
    # 同义说法「合同把关 条款风险」命中的全是错技能，根因即缺这组映射）──
    "合同": ["contract", "agreement", "legal review", "clause"],
    "审": ["audit", "review", "inspect", "examine"],
    "审查": ["audit", "review", "inspect"],
    "把关": ["gatekeep", "verify", "quality check", "vet"],
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


def build_index(quiet=False):
    roots = default_roots()
    say = (lambda *a, **k: None) if quiet else print
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
        "built_at": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
        "n_skills": len(entries),
        "n_broken": len(broken),
        "broken": broken,
        "roots": roots,
        "skills": entries,
    }
    out = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "index.json"))
    with open(out, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=1)
    say(f"索引已建：{out}")
    say(f"  已索引技能：{len(entries)} 个（扫描 {len(roots)} 个根）")
    for r in roots:
        say(f"    · {r}")
    if broken:
        say(f"  ⚠️ 失效链接：{len(broken)} 个 → {', '.join(broken)}")
    if collisions:
        shown = ", ".join(f"{n}({a}⊂{b})" for n, a, b in collisions[:6])
        say(f"  ⚠️ 同名去重 {len(collisions)} 个（先到根优先）：{shown}")
    by_source = {}
    for e in entries:
        by_source.setdefault(e["source"], []).append(e["name"])
    say("  按来源：")
    for src, names in sorted(by_source.items(), key=lambda kv: -len(kv[1])):
        say(f"    {src}: {len(names)}")
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


# ── 英文停用词 ──
# 2026-10-26 实测：查「audit an agreement before signing」时，an / before 这类词
# 进了检索词集合，又在每条英文描述里做子串匹配（desc~an 命中几乎所有技能），
# 一次灌出 116 个候选。检索词必须是「有区分度的词」，冠词介词不是。
STOPWORDS_EN = {
    "a", "an", "the", "and", "or", "but", "if", "of", "to", "in", "on", "at",
    "by", "for", "with", "from", "into", "about", "as", "is", "are", "was",
    "be", "do", "does", "did", "it", "its", "this", "that", "these", "those",
    "my", "me", "i", "we", "you", "he", "she", "they", "them", "his", "her",
    "before", "after", "then", "than", "when", "while", "during", "over",
    "under", "again", "very", "can", "could", "should", "would", "will",
    "just", "also", "some", "any", "all", "not", "no", "up", "down", "out",
    "there", "here", "what", "which", "who", "how", "why", "please", "want",
    "need", "help", "find", "get", "make", "use", "using", "something",
}


def strip_flags(text):
    """命令行 flag（--json/--top 及取值）不该被当成查询词。"""
    words = text.split()
    out = []
    skip = 0
    for w in words:
        if skip:
            skip -= 1
            continue
        if w.startswith("--"):
            if w == "--top":  # --top 的取值是数字，一并跳过
                skip = 1
            continue
        out.append(w)
    return " ".join(out).strip()


def expand_query(query, extra_terms=None):
    """把查询扩展成英文检索词集合（中文查映射表，英文词原样保留）。
    中英混排（如「pdf转word」）先在中英边界切开再分词。
    extra_terms：用户自定义映射（None=自动加载 ljc_terms.json，优先于内置）。"""
    q = query.lower().strip()
    # 命令行 flag 不该进查询（--json/--top 等）
    q = " ".join(w for w in q.split() if not w.startswith("--")).strip()
    terms = set()
    # 0) 中英边界切开：pdf转word → pdf 转 word
    q = re.sub(r"([a-z0-9])([\u4e00-\u9fff])", r"\1 \2", q)
    q = re.sub(r"([\u4e00-\u9fff])([a-z0-9])", r"\1 \2", q)
    # 1) 查询本身（去标点）
    cleaned = re.sub(r"[，。！？、；：「」『』（）\s]+", " ", q).strip()
    if cleaned:
        terms.add(cleaned)
        for w in cleaned.split():
            if len(w) >= 2 and w not in STOPWORDS_EN:
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


# ── 语义后端：探测 / 向量 / 融合 ──
# 状态放在模块级字典里，doctor 与 query/suggest 共用一次探测结果（避免重复加载模型）。
SEM_STATE = {"checked": False, "backend": None, "reason": "未探测", "source": None}

# 默认模型选型（2026-10-26 真机 A/B 实测，8 个查询 × 179 技能）：
# bge-m3 是**检索型**多语言模型，paraphrase-multilingual-MiniLM 是**释义型**——
# 后者衡量「两句话像不像」，前者衡量「这段话该不该被这句话检索到」，后者不是为检索设计的。
# 实测差别：画流程图 → bge-m3 给 editorial-diagram-designer（对），
# paraphrase 给 writing-shape（错）；余弦分布 0.36~0.55 → 0.49~0.63，区分度更好。
DEFAULT_MODELS = {
    "sentence-transformers": "BAAI/bge-m3",
    "fastembed": "BAAI/bge-small-zh-v1.5",
    "openai": "text-embedding-3-small",
}


def _spec(name, spec_fn=None):
    """查模块是否存在（只看不 import，不执行第三方代码）。测试可注入。"""
    fn = spec_fn or importlib.util.find_spec
    try:
        return fn(name)
    except Exception:
        return None


def _needs_prefix(model):
    """检索型模型（bge / e5 / gte 系）要求查询带 'query: ' 前缀、文档带
    'passage: ' 前缀——不加前缀效果会明显打折；释义型（paraphrase-*）不需要。"""
    m = (model or "").lower()
    return any(k in m for k in ("bge", "e5", "gte", "multilingual-embedding"))


def _with_prefix(encode_fn, model):
    """把前缀规则包在编码函数外面，用户不必手工配前缀。"""
    if not _needs_prefix(model):
        return encode_fn

    def encode(texts, kind="doc"):
        tag = "query: " if kind == "query" else "passage: "
        return encode_fn([tag + t for t in texts])
    return encode


def _local_backend(kind, model, spec_fn=None):
    """构造本机嵌入后端：延迟 import，只在真的要用时才加载模型。"""
    if kind == "sentence-transformers":
        mod = importlib.import_module("sentence_transformers")
        st_model = mod.SentenceTransformer(model)

        def raw_encode(texts):
            vecs = st_model.encode(list(texts), normalize_embeddings=True)
            return [[float(x) for x in v] for v in vecs]
    else:  # fastembed
        mod = importlib.import_module("fastembed")
        fe_model = mod.TextEmbedding(model_name=model)

        def raw_encode(texts):
            return [[float(x) for x in v] for v in fe_model.embed(list(texts))]
    return {"kind": "local", "name": kind, "model": model,
            "key": f"{kind}:{model}", "encode": _with_prefix(raw_encode, model)}


def _cloud_backend(model, spec_fn=None):
    if not os.environ.get("OPENAI_API_KEY"):
        return None
    mod = importlib.import_module("openai")
    client = mod.OpenAI()

    def raw_encode(texts):
        resp = client.embeddings.create(model=model, input=list(texts))
        out = [None] * len(texts)
        for item in resp.data:
            out[item.index] = [float(x) for x in item.embedding]
        if any(v is None for v in out):
            raise RuntimeError("云端返回向量不完整")
        return out
    return {"kind": "cloud", "name": "openai", "model": model,
            "key": f"openai:{model}", "encode": _with_prefix(raw_encode, model)}


def _external_backend(cmd, spec_fn=None):
    def encode(texts, kind="doc"):
        proc = subprocess.run(cmd, shell=True, input=json.dumps(list(texts)),
                              capture_output=True, text=True, timeout=180)
        if proc.returncode != 0:
            raise RuntimeError(f"外部嵌入命令退出码 {proc.returncode}: "
                               f"{proc.stderr.strip()[:120]}")
        data = json.loads(proc.stdout)
        if isinstance(data, dict):
            data = data.get("vectors") or data.get("embeddings")
        if not isinstance(data, list) or len(data) != len(texts):
            raise RuntimeError(f"外部嵌入命令返回形状不符（期望 {len(texts)} 条）")
        return [[float(x) for x in v] for v in data]
    return {"kind": "external", "name": "external-cmd", "model": cmd,
            "key": f"cmd:{cmd}", "encode": encode}


def detect_semantic_backend(env=None, spec_fn=None):
    """探测可用的语义后端；探测不到返回 None（调用方回退词袋）。
    任何异常都被吞掉——语义是增强项，绝不能让它拖垮检索主流程。"""
    env = os.environ if env is None else env
    mode = (env.get("LJC_SEMANTIC") or "auto").strip().lower()
    if mode in ("off", "0", "no", "false", "never"):
        return None, "已用 LJC_SEMANTIC=off 关闭"
    model_override = (env.get("LJC_SEMANTIC_MODEL") or "").strip() or None
    cmd = (env.get("LJC_SEMANTIC_CMD") or "").strip()
    tried = []

    def want(family):
        return mode in ("auto", family)

    try:
        if want("local"):
            for kind in ("sentence-transformers", "fastembed"):
                tried.append(kind)
                if _spec(kind.replace("-", "_"), spec_fn) is not None:
                    model = model_override or DEFAULT_MODELS[kind]
                    return _local_backend(kind, model, spec_fn), f"{kind}（本机模型）"
        if want("cloud"):
            tried.append("openai")
            if _spec("openai", spec_fn) is not None and env.get("OPENAI_API_KEY"):
                model = model_override or DEFAULT_MODELS["openai"]
                return _cloud_backend(model, spec_fn), "openai（云端嵌入）"
        if want("external") and cmd:
            tried.append("external-cmd")
            return _external_backend(cmd, spec_fn), "外部嵌入命令"
    except Exception as e:  # 加载模型/初始化客户端失败 → 当作没有
        return None, f"后端初始化失败（{e}），已回退词袋"
    if mode != "auto":
        return None, f"LJC_SEMANTIC={mode} 但未找到可用后端（试过 {', '.join(tried) or '—'}）"
    return None, (f"未检测到嵌入库（试过 {', '.join(tried)}）→ 用内置 TF-IDF 词袋；"
                  f"想上真语义：pip install sentence-transformers，或设 LJC_SEMANTIC_CMD")


# ── 跨解释器重启 ──
# 现实问题：脚本可能是被「没装嵌入库的那个 python」跑的（SKILL.md 里写的就是通用
# python3），而按隔离规则包装在托管 venv 里。为了让语义后端真能用上，
# 探测不到时换一个装了嵌入库的解释器重启自己——只在确实能换时才换。
SEM_PY_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache", "semantic_py.txt")


def candidate_interpreters(envs_dir=None):
    """可能装有嵌入库的其它解释器：env 指定的优先，其次 WorkBuddy 托管 venv。
    只走登记过的路径，不扫全盘。"""
    cands = []
    env_py = (os.environ.get("LJC_SEMANTIC_PY") or "").strip()
    if env_py:
        cands.append(env_py)
    d = envs_dir or os.path.expanduser("~/.workbuddy/binaries/python/envs")
    try:
        for name in sorted(os.listdir(d)):
            p = os.path.join(d, name, "bin", "python3")
            if os.path.isfile(p):
                cands.append(p)
    except OSError:
        pass
    out, seen = [], set()
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def interpreter_has_backend(py):
    """问另一个解释器：你那儿有嵌入库吗（只看不 import，很快）。"""
    code = ("import importlib.util as u; print('1' if (u.find_spec('sentence_transformers')"
            " or u.find_spec('fastembed')) else '0')")
    try:
        r = subprocess.run([py, "-c", code], capture_output=True, text=True, timeout=30)
        return r.stdout.strip().endswith("1")
    except Exception:
        return False


def find_semantic_python(cache_path=None, envs_dir=None):
    """已验证装了嵌入库的解释器路径（结果缓存，避免每次 fork）。"""
    path = cache_path or SEM_PY_CACHE
    try:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                cached = f.read().strip()
            if cached and os.path.isfile(cached) and interpreter_has_backend(cached):
                return cached
    except OSError:
        pass
    for py in candidate_interpreters(envs_dir):
        if os.path.abspath(py) == os.path.abspath(sys.executable):
            continue
        if interpreter_has_backend(py):
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(py)
            except OSError:
                pass
            return py
    return None


def maybe_reexec(cmds=("query", "suggest", "semantic"), needed=True):
    """当前解释器没嵌入库、别处有 → 换解释器重启自己。
    三重保险：LJC_NO_REEXEC 防循环、LJC_SEMANTIC=off 尊重关闭、只在检索类命令上换。
    needed=False 表示这一轮压根用不到模型（词袋就答得上来），那就连探测都别做
    ——fork 另一个解释器要零点几秒，不该白花。"""
    if not needed:
        return
    if len(sys.argv) < 2 or sys.argv[1] not in cmds:
        return
    if os.environ.get("LJC_NO_REEXEC") or os.environ.get("LJC_SEMANTIC") == "off":
        return
    try:
        if detect_semantic_backend()[0] is not None:
            return  # 当前解释器就有，不用换
    except Exception:
        return
    py = find_semantic_python()
    if not py:
        return
    env = dict(os.environ, LJC_NO_REEXEC="1")
    try:
        print(f"ℹ️ 语义后端在 {py}（当前解释器无嵌入库），切换解释器重跑…", file=sys.stderr)
        os.execve(py, [py, os.path.abspath(__file__)] + sys.argv[1:], env)
    except Exception:
        pass  # 换不成就用当前的继续跑，绝不影响检索


def resolve_semantic_backend(env=None, force=False):
    """带缓存的探测：一次会话只探测一次（加载模型很贵）。"""
    if SEM_STATE["checked"] and not force:
        return SEM_STATE["backend"]
    backend, reason = detect_semantic_backend(env=env)
    SEM_STATE.update(checked=True, backend=backend, reason=reason)
    return backend


def index_fingerprint(index):
    """索引指纹：技能集合变了/索引重建了，向量缓存就作废重算。"""
    names = sorted(e.get("name", "") for e in index.get("skills", []))
    digest = hashlib.md5("\n".join(names).encode("utf-8")).hexdigest()[:12]
    return f"{index.get('built_at', '')}|{len(names)}|{digest}"


def _entry_doc(entry):
    """送进嵌入模型的文本：名字（拆开连字符）+ 中英双语描述。"""
    name = (entry.get("name", "") or "").replace("-", " ").replace("_", " ")
    return " ".join([name, entry.get("description", "") or "",
                     entry.get("description_zh", "") or ""]).strip()


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def load_vectors(index, backend, cache_path=None):
    """取全部技能的向量：命中缓存直接读，否则算一次并写缓存。
    返回 (dict name→vector, 来源)；失败返回 (None, 原因)。"""
    path = cache_path or EMBED_CACHE_PATH
    fp = index_fingerprint(index)
    try:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if (data.get("fingerprint") == fp and data.get("key") == backend["key"]
                    and len(data.get("names", [])) == len(data.get("vectors", []))):
                return dict(zip(data["names"], data["vectors"])), "缓存"
    except Exception:
        pass
    names = [e.get("name", "") for e in index["skills"]]
    try:
        vecs = backend["encode"]([_entry_doc(e) for e in index["skills"]])
    except Exception as e:
        return None, f"编码失败：{str(e)[:80]}"
    if not vecs or len(vecs) != len(names):
        return None, "向量条数与技能数不符"
    table = {n: [float(x) for x in v] for n, v in zip(names, vecs)}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"fingerprint": fp, "key": backend["key"], "names": names,
                       "vectors": [table[n] for n in names]}, f)
    except OSError:
        pass
    return table, "本次计算"


def vector_scores(index, query_text, backend, cache_path=None):
    """真语义相似度：把查询和每个技能描述各自编码成向量，算余弦。
    返回 0~1 列表（与 index['skills'] 对齐）；不可用时返回 []（调用方回退词袋）。"""
    if backend is None:
        return []
    try:
        qv = backend["encode"]([query_text], kind="query")[0]
    except Exception as e:
        SEM_STATE["reason"] = f"查询编码失败（{str(e)[:80]}），已回退词袋"
        return []
    table, source = load_vectors(index, backend, cache_path)
    if not table:
        SEM_STATE["reason"] = f"语义后端不可用（{source}），已回退词袋"
        return []
    SEM_STATE["source"] = f"{backend['name']} {backend['model']} · {source}"
    return [_cosine(qv, table.get(e.get("name", ""), [])) for e in index["skills"]]


# 余弦基线校准（2026-10-26 实测教训）：嵌入模型对「毫不相干的两段短文本」
# 也常给出 0.3~0.5 的余弦——不加门槛会把无关技能灌满结果。
# VEC_FLOOR：低于此值视为「没有语义信号」，一分不加。
# VEC_RESCUE：关键词零命中时，要捞回一个技能需要的向量分（还需同时 ≥ 本轮最高分的 80%）。
VEC_FLOOR = 0.35
VEC_RESCUE = 0.50
VEC_REL_RATIO = 0.8
VEC_RESCUE_MAX = 10   # 捞回名额上限：召回是补位，不是把半库都搬出来
# 懒加载阈值：关键词命中 ≥ 这个数就不加载模型。
# 实测（2026-10-26）：加载 bge-m3 约 10~15 秒，词袋检索 0.09 秒——
# 「秒回」是本地检索的核心卖点，不能为锦上添花的能力把它赔进去。
# 于是：词袋答得上来就不请模型出山，答不上来（命中太少）才付这份代价。
VEC_LAZY_MIN = 3


def needs_vector(n_keyword_hits, threshold=VEC_LAZY_MIN):
    """纯函数：这一轮值不值得为语义后端付模型加载的时间？
    关键词自己就答得上来 → 不值得（保持秒回）；答不上来 → 值得。"""
    return n_keyword_hits < threshold


def vec_rescue_ok(vec, i, vmax, floor=VEC_RESCUE, ratio=VEC_REL_RATIO):
    """纯函数：关键词零命中时，这个技能的向量分是否强到值得捞回。
    双关：绝对下限（防余弦基线噪声）+ 不低于本轮最高分的 ratio（防「全体都很像」）。"""
    if not vec:
        return False
    return vec[i] >= floor and vec[i] >= ratio * vmax


def rescue_indices(vec, vmax, max_n=VEC_RESCUE_MAX):
    """纯函数：本轮允许被向量捞回的下标集合（按相似度取前 N）。
    理由：语义召回是补位手段，宁可少捞也不错捞——捞太多等于没检索。"""
    if not vec:
        return set()
    picked = set()
    for i in sorted(range(len(vec)), key=lambda k: -vec[k]):
        if not vec_rescue_ok(vec, i, vmax):
            break  # 降序排列，第一个不满足的之后都不满足
        picked.add(i)
        if len(picked) >= max_n:
            break
    return picked


def fusion_points(tfidf_sim, vec_sim, floor=VEC_FLOOR):
    """纯函数：把词袋信号与向量信号融合成加分（可测试）。
    取两者较强者，而非相加——两个引擎都在衡量同一件事，相加会虚高；
    但两者同时给出有效信号（都 ≥5 分）说明不是巧合，额外 +5 表示一致性。
    floor 之下视为噪声（余弦基线问题），一律不计分。"""
    a = int(round(min(max(tfidf_sim or 0.0, 0.0) * 30, 30)))
    raw = max(vec_sim or 0.0, 0.0)
    b = int(round(min(raw * 35, 35))) if raw >= floor else 0
    pts = max(a, b)
    if a >= 5 and b >= 5:
        pts = min(pts + 5, 40)
        return pts, f"tfidf{a}+vec{b}一致+5"
    if b > a and b >= 5:
        return pts, f"vec+{b}"
    if a >= 5:
        return pts, f"tfidf+{a}"
    if b >= 5:
        return pts, f"vec+{b}"
    return pts, None


def semantic_query_text(query, terms=None):
    """送给嵌入模型的查询文本：**只用用户原话**。
    2026-10-26 真模型实测（paraphrase-multilingual-MiniLM-L12-v2，179 技能）：
    把扩展检索词拼进去反而更差——扩展词里含中文整句片段，拼出来重复冗长
    （如「…条款对我方不利 article clause engagement …条款对我方不利 letter」），
    嵌入模型吃干净短句最准。实测 top1 从错的 intro-drafter 变回对的
    contract-review-pro。跨语言由多语言模型自身承担，不必手工拼英文。"""
    return strip_flags(query).strip()


def record_hits(mode, query, scored_names, hidden_names):
    """把一次 query/suggest 的命中追加到本地历史（jsonl，只追加不改写）。
    这是「死技能证据」的数据来源：从未出现在任何记录里的技能，
    就是 suggest 关闭建议的硬证据。写失败不影响主流程。"""
    import time as _t
    rec = {"ts": _t.strftime("%Y-%m-%dT%H:%M:%S"), "mode": mode,
           "q": query, "hits": list(scored_names), "unreg": list(hidden_names)}
    try:
        with open(HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def compute_stats(history_path, index):
    """纯函数：从命中历史算统计（可测试）。"""
    counts = {}
    records = 0
    if os.path.isfile(history_path):
        with open(history_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                records += 1
                for name in rec.get("hits", []):
                    counts[name] = counts.get(name, 0) + 1
    all_names = {e["name"] for e in index["skills"]
                 if not e.get("invocation_disabled")}
    return {"records": records, "counts": counts,
            "never_hit": sorted(all_names - set(counts))}


def cmd_stats(top=10):
    index = load_index()
    index = ensure_fresh_index(index)
    st = compute_stats(HISTORY_PATH, index)
    print(f"LJC 命中统计 —— 历史 {st['records']} 条记录")
    if st["records"] == 0:
        print("  （还没有历史——每次 query/suggest 会自动累积）")
        return
    top_hits = sorted(st["counts"].items(), key=lambda kv: -kv[1])[:top]
    print(f"\n── 命中次数 Top {len(top_hits)} ──")
    for name, c in top_hits:
        print(f"  {name:38s} {c} 次")
    never = st["never_hit"]
    print(f"\n── 从未被任何查询命中（{len(never)} 个）——关闭候选的硬证据 ──")
    for name in never:
        print(f"  · {name}")
    print("\n   关闭需你同意：WorkBuddy 走 skill_management disable，恢复=设置里重开。")


def cmd_query(query, top=10, as_json=False):
    index = load_index()
    index = ensure_fresh_index(index)
    terms = expand_query(query)
    if not terms:
        print("查询为空")
        return
    scored = []
    hidden = []
    sem = semantic_scores(index["skills"], terms)
    # 先只跑词袋（毫秒级）：答得上来就不必请模型出山（加载 10~15 秒）。
    bag = [score_entry(e, terms) for e in index["skills"]]
    n_bag_hits = sum(1 for s, _ in bag if s > 0)
    vec = []
    if needs_vector(n_bag_hits):
        backend = resolve_semantic_backend()
        vec = vector_scores(index, semantic_query_text(query, terms), backend) if backend else []
    vmax = max(vec) if vec else 0.0
    rescued = rescue_indices(vec, vmax)
    for i, e in enumerate(index["skills"]):
        s, hits = bag[i]
        vs = vec[i] if vec else None
        sem_pts, label = fusion_points(sem[i], vs)
        if s <= 0:
            # 关键词零命中但语义信号强 → 低分候选（扩召回）。
            # 过三关：绝对下限 + 不低于本轮最高分 80% + 名额上限（防基线噪声灌入）。
            if sem_pts >= 8 and (not vec or i in rescued):
                # 标注「语义候选」：关键词一个没命中、全靠语义信号捞回来的，
                # 可信度低于关键词命中项，用户应当知道这一点。
                s, hits = sem_pts, [f"语义候选·{label or f'tfidf≈{sem[i]:.2f}'}"]
            else:
                continue
        else:
            s += sem_pts
            if label:
                hits.append(label)
        if e.get("invocation_disabled"):
            hidden.append((s, hits, e))
            continue
        scored.append((s, hits, e))
    scored.sort(key=lambda x: (-x[0], x[2]["name"]))
    hidden.sort(key=lambda x: -x[0])
    record_hits("query", query, [e["name"] for _, _, e in scored],
                [e["name"] for _, _, e in hidden])
    if as_json:
        print(json.dumps({
            "query": query, "terms": sorted(terms),
            "results": [{"name": e["name"], "source": e["source"], "score": s,
                         "hits": hits,
                         "description": (e.get("description") or e.get("description_zh") or "")}
                        for s, hits, e in scored],
            "unregistered": [{"name": e["name"], "source": e["source"], "score": s,
                              "description": (e.get("description") or e.get("description_zh") or "")}
                             for s, hits, e in hidden],
        }, ensure_ascii=False, indent=1))
        return
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


def ensure_fresh_index(index):
    """索引过期（任一扫描根比 built_at 新 5 秒以上）时自动重建——自愈而非提醒。
    built_at 精确到秒、阈值 5 秒：分钟精度曾导致『目录创建于重建后 57 秒』
    这种差值被 60 秒阈值卡死、自愈永不触发的 bug（2026-10-26 实测）。
    177 个技能全量重建 <1 秒，自动重建比 launchd 监听更简单可靠。
    返回（可能已重建的）index。"""
    import time as _t
    try:
        roots = index.get("roots") or [SKILLS_DIR]
        newest = 0
        for root in roots:
            if not os.path.isdir(root):
                continue
            newest = max(newest, max(
                os.path.getmtime(os.path.join(root, n)) for n in os.listdir(root)))
        built = 0
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                built = _t.mktime(_t.strptime(index.get("built_at", ""), fmt))
                break
            except ValueError:
                continue
        if newest - built > 5:
            print("ℹ️ 索引已过期，自动重建…")
            return build_index(quiet=True)
    except Exception:
        pass
    return index


def cmd_suggest(task, top=10, as_json=False):
    """任务视角的技能盘点：相关候选 vs 本次用不到（可暂时关掉的候选）。"""
    index = load_index()
    index = ensure_fresh_index(index)
    terms = expand_query(task)
    if not terms:
        print("任务描述为空")
        return
    useful, unused = [], []
    sem = semantic_scores(index["skills"], terms)
    bag = [score_entry(e, terms) for e in index["skills"]]
    n_bag_hits = sum(1 for e, (s, _) in zip(index["skills"], bag)
                     if s > 0 and not e.get("invocation_disabled"))
    vec = []  # 同 query：词袋答得上来就不加载模型
    if needs_vector(n_bag_hits):
        backend = resolve_semantic_backend()
        vec = vector_scores(index, semantic_query_text(task, terms), backend) if backend else []
    for i, e in enumerate(index["skills"]):
        if e.get("invocation_disabled"):
            continue  # 未注册的本来就不可调用，不参与停用建议
        s, hits = bag[i]
        sem_pts, label = fusion_points(sem[i], vec[i] if vec else None)
        s += sem_pts
        if label:
            hits.append(label)
        if s > 0:
            useful.append((s, hits, e))
        else:
            unused.append(e)
    useful.sort(key=lambda x: (-x[0], x[2]["name"]))
    record_hits("suggest", task, [e["name"] for _, _, e in useful],
                [e["name"] for e in unused])
    if as_json:
        print(json.dumps({
            "task": task, "terms": sorted(terms),
            "useful": [{"name": e["name"], "source": e["source"], "score": s,
                        "description": (e.get("description") or e.get("description_zh") or "")}
                       for s, hits, e in useful],
            "unused": [e["name"] for e in unused],
        }, ensure_ascii=False, indent=1))
        return
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


def cmd_semantic():
    """语义后端自检：探测到什么、用什么模型、缓存状态；没探测到说明怎么开。"""
    index = load_index()
    backend = resolve_semantic_backend(force=True)
    print("LJC 语义后端自检")
    if not backend:
        print(f" 状态：未启用 —— {SEM_STATE['reason']}")
        print("\n 当前走内置 TF-IDF（纯标准库、词袋级：比字符重叠，不懂同义）。")
        print(" 想启用真语义（三选一，都是可选的）：")
        print("   1) 本机模型：pip install sentence-transformers   （离线可跑，首次下载模型约 100~500MB）")
        print("   2) 本机模型：pip install fastembed               （更轻，onnxruntime 推理）")
        print("   3) 外部命令：export LJC_SEMANTIC_CMD='你的嵌入命令'  "
              "（stdin 收 JSON 字符串数组，stdout 吐 JSON 二维数组）")
        print("\n 关闭探测：export LJC_SEMANTIC=off   指定模型：export LJC_SEMANTIC_MODEL=...")
        return
    print(f" 状态：已启用 —— {backend['kind']} / {backend['name']}")
    print(f" 模型：{backend['model']}")
    table, source = load_vectors(index, backend)
    if table:
        dim = len(next(iter(table.values())))
        print(f" 向量：{len(table)} 条 × {dim} 维（{source}）")
        print(f" 缓存：{EMBED_CACHE_PATH}")
    else:
        print(f" ⚠ 编码未成功：{source} —— 本次检索将回退词袋")


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
    try:
        st = compute_stats(HISTORY_PATH, index)
        if st["records"]:
            print(f" ✓ 命中历史：{st['records']} 条记录；"
                  f"从未被命中 {len(st['never_hit'])} 个（find.py stats 看详情）")
        else:
            print(" · 命中历史：空（将随 query/suggest 使用自动积累）")
    except Exception:
        pass
    b = resolve_semantic_backend()
    if b:
        print(f" ✓ 语义后端：{SEM_STATE.get('source') or b['key']}"
              f"（向量召回已启用；缓存 {os.path.basename(EMBED_CACHE_PATH)}）")
    else:
        print(f" · 语义后端未启用：{SEM_STATE['reason']}")
        print("   （不影响使用：已回退内置 TF-IDF 词袋检索）")
    print("\n结论：" + (f"发现 {len(problems)} 项待处理 → {', '.join(problems)}"
                       if problems else "全部正常"))


def reexec_needed(argv):
    """这一轮要不要为语义后端换解释器？先廉价地估一下词袋命中数再决定。
    估不出来（索引坏了等）就保守返回 True——宁可多换一次，不要漏掉语义能力。"""
    if len(argv) < 3:
        return True
    text = strip_flags(" ".join(argv[2:]))
    if not text:
        return True
    try:
        index = load_index()
        terms = expand_query(text)
        if not terms:
            return True
        n = sum(1 for e in index["skills"] if score_entry(e, terms)[0] > 0)
        return needs_vector(n)
    except Exception:
        return True


def main():
    if len(sys.argv) >= 2 and sys.argv[1] in ("query", "suggest"):
        maybe_reexec(needed=reexec_needed(sys.argv))
    elif len(sys.argv) >= 2 and sys.argv[1] == "semantic":
        maybe_reexec()
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == "build":
        build_index()
    elif cmd == "query":
        if len(sys.argv) < 3:
            print("用法: find.py query \"关键词\" [--top N] [--json]")
            return
        top = 10
        as_json = "--json" in sys.argv
        if "--top" in sys.argv:
            i = sys.argv.index("--top")
            top = int(sys.argv[i + 1])
            query = " ".join(sys.argv[2:i])
        else:
            query = " ".join(sys.argv[2:])
        cmd_query(strip_flags(query), top, as_json)
    elif cmd == "suggest":
        if len(sys.argv) < 3:
            print("用法: find.py suggest \"任务描述\" [--top N] [--json]")
            return
        top = 10
        as_json = "--json" in sys.argv
        if "--top" in sys.argv:
            i = sys.argv.index("--top")
            top = int(sys.argv[i + 1])
            task = " ".join(sys.argv[2:i])
        else:
            task = " ".join(sys.argv[2:])
        cmd_suggest(strip_flags(task), top, as_json)
    elif cmd == "stats":
        top = 10
        if "--top" in sys.argv:
            i = sys.argv.index("--top")
            top = int(sys.argv[i + 1])
        cmd_stats(top)
    elif cmd == "doctor":
        cmd_doctor()
    elif cmd == "semantic":
        cmd_semantic()
    elif cmd == "list":
        cmd_list()
    else:
        print(f"未知命令: {cmd}\n")
        print(__doc__)


if __name__ == "__main__":
    main()
