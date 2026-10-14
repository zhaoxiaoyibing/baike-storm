# -*- coding: utf-8 -*-
"""将报告 Markdown 渲染为单文件 HTML。"""

import html
import json
import os
import re
import sys
from . import tracing as ls


def _inline(text: str) -> str:
    """渲染粗体和脚注，并转义 HTML。"""
    s = html.escape(text)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)

    def _cite(m):
        n = m.group(1)
        return f'<sup class="fn"><a id="fnref-{n}" href="#fn-{n}">[{n}]</a></sup>'

    return re.sub(r"\[(\d+)\]", _cite, s)


_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")


def _table_cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _render_blocks(body: str, id_prefix: str | None = None) -> str:
    """渲染段落、标题、列表、表格和引用块。"""
    out, i, k = [], 0, 0
    lines = body.strip("\n").split("\n")
    while i < len(lines):
        ln = lines[i]
        if not ln.strip():
            i += 1
            continue
        hm = re.match(r"^\s*(#{3,6})\s+(.+?)\s*#*\s*$", ln)
        if hm:
            lvl = min(len(hm.group(1)), 4)
            hid = ""
            if lvl == 3 and id_prefix is not None:
                hid = f' id="{id_prefix}-{k}"'
                k += 1
            out.append(f"<h{lvl}{hid}>{_inline(hm.group(2))}</h{lvl}>")
            i += 1
            continue
        if ln.lstrip().startswith("|") and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            head = _table_cells(ln)
            i += 2
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append(_table_cells(lines[i]))
                i += 1
            thead = "".join(f"<th>{_inline(c)}</th>" for c in head)
            tbody = "".join(
                "<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in rows
            )
            out.append(f'<div class="tbl"><table><thead><tr>{thead}</tr></thead>'
                       f"<tbody>{tbody}</tbody></table></div>")
            continue
        if ln.lstrip().startswith(">"):
            buf = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                buf.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            out.append(f"<blockquote>{_inline(' '.join(buf))}</blockquote>")
            continue
        if re.match(r"^\s*[-*]\s+", ln):
            buf = []
            while i < len(lines) and re.match(r"^\s*[-*]\s+", lines[i]):
                buf.append(re.sub(r"^\s*[-*]\s+", "", lines[i]))
                i += 1
            out.append("<ul>" + "".join(f"<li>{_inline(x)}</li>" for x in buf) + "</ul>")
            continue
        if re.match(r"^\s*\d+\.\s+", ln):
            buf = []
            while i < len(lines) and re.match(r"^\s*\d+\.\s+", lines[i]):
                buf.append(re.sub(r"^\s*\d+\.\s+", "", lines[i]))
                i += 1
            out.append("<ol>" + "".join(f"<li>{_inline(x)}</li>" for x in buf) + "</ol>")
            continue
        buf = []
        while i < len(lines) and lines[i].strip() and not re.match(
            r"^\s*([-*]\s+|\d+\.\s+|>|#{3,6}\s|\|)", lines[i]
        ):
            buf.append(lines[i].strip())
            i += 1
        out.append(f"<p>{_inline(' '.join(buf))}</p>")
    return "\n".join(out)


def parse_report(md: str):
    """返回 (title, [(章节标题, 正文)], [(编号, 来源文本, URL)])。

    兼容旧版 ### Sources 和当前的 ## 参考资料。
    """
    src = re.split(r"^(?:###\s+Sources|##\s+参考资料)\s*$", md, maxsplit=1, flags=re.MULTILINE)
    main, sources_raw = src[0], (src[1] if len(src) > 1 else "")
    tm = re.search(r"^#\s+(.+)$", main, flags=re.MULTILINE)
    title = tm.group(1).strip() if tm else "百科词条"
    sections, parts = [], re.split(r"^##\s+(.+)$", main, flags=re.MULTILINE)
    for i in range(1, len(parts), 2):
        sections.append((parts[i].strip(), parts[i + 1] if i + 1 < len(parts) else ""))
    sources = []
    for line in sources_raw.splitlines():
        m = re.match(r"\s*\[(\d+)\]\s*(.*?)\s*(?::|\s-)\s*(https?://\S+)\s*$", line)
        if m:
            sources.append((m.group(1), m.group(2).strip(), m.group(3).strip()))
    return title, sections, sources


def extract_preamble(md: str) -> str:
    """提取标题和首个章节之间的导语。"""
    m = re.search(r"^#\s+.+$", md, flags=re.MULTILINE)
    rest = md[m.end():] if m else md
    rest = re.split(r"^##\s+", rest, maxsplit=1, flags=re.MULTILINE)[0]
    return " ".join(ln.strip() for ln in rest.strip().splitlines() if ln.strip())


def _subheads(body: str) -> list[str]:
    """提取小节标题，顺序须与 _render_blocks 的锚点编号一致。"""
    out = []
    for ln in body.splitlines():
        m = re.match(r"^\s*###\s+(.+?)\s*#*\s*$", ln)
        if m:
            out.append(re.sub(r"\*\*|\[\d+\]", "", m.group(1)).strip())
    return out


def _lead(sections) -> str:
    """取第一节正文的首句作为简介。"""
    if not sections:
        return ""
    body = re.sub(r"\[\d+\]", "", sections[0][1]).strip()
    body = re.sub(r"\*\*", "", body)
    first = re.split(r"(?<=。)", body.strip())
    return first[0].strip() if first else body[:80]


CSS = """
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --paper:#f7f6f2;--surface:#fffefb;--ink:#1f1e1b;--ink-2:#46433d;--muted:#7a766d;
  --line:#e6e2d9;--line-2:#d6d0c4;--accent:#2d6a88;--accent-ink:#1f4f67;--accent-soft:#e3ecef;
  --mark:#f3ead2;
  --sans:-apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Noto Sans SC",
    "Microsoft YaHei","Segoe UI",sans-serif;
  --serif:"Noto Serif SC","Source Han Serif SC","Songti SC","STSong","SimSun",Georgia,serif;
  --mono:ui-monospace,"SF Mono","JetBrains Mono",Menlo,Consolas,monospace;
  --shadow:0 1px 2px rgba(64,52,30,.05),0 6px 20px -8px rgba(64,52,30,.12);
}
html{scroll-behavior:smooth}
body{font-family:var(--sans);color:var(--ink);background:var(--paper);
  line-height:1.85;font-size:16.5px;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
a{color:var(--accent);text-decoration:none;text-underline-offset:3px;transition:color .2s}
a:hover{color:var(--accent-ink);text-decoration:underline}
a:focus-visible,button:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:3px}
/* 阅读进度 */
.progress{position:fixed;inset:0 0 auto 0;height:2px;background:var(--accent);
  transform:scaleX(0);transform-origin:0 50%;z-index:10;pointer-events:none}
/* 头部 */
.hero{padding:64px 28px 40px}
.hero-in{max-width:1080px;margin:0 auto}
.kicker{font-size:13px;color:var(--muted);letter-spacing:.08em;display:flex;gap:10px;align-items:center}
.kicker .dot{width:5px;height:5px;background:var(--accent);border-radius:1px}
.hero h1{font-family:var(--serif);font-size:clamp(32px,4.6vw,46px);font-weight:700;color:var(--ink);
  letter-spacing:-.01em;line-height:1.2;margin-top:14px;text-wrap:balance}
.lead{margin-top:20px;max-width:42em;color:var(--ink-2);font-size:17.5px;line-height:1.9;text-wrap:pretty}
.meta{margin-top:24px;display:flex;flex-wrap:wrap;gap:6px 22px;font-size:13px;color:var(--muted);
  padding-top:16px;border-top:1px solid var(--line);max-width:42em}
.meta b{font-family:var(--mono);font-weight:500;color:var(--ink);font-variant-numeric:tabular-nums;margin-right:4px}
/* 布局 */
.layout{max-width:1080px;margin:8px auto 0;padding:0 28px;display:grid;
  grid-template-columns:196px minmax(0,800px);gap:56px;align-items:start}
/* 目录 */
.toc{position:sticky;top:28px;font-size:14px;max-height:calc(100dvh - 56px);overflow:auto;padding-bottom:12px}
.toc h4{font-size:12px;letter-spacing:.12em;color:var(--muted);margin:0 0 10px;font-weight:500}
.toc-list{border-left:1px solid var(--line)}
.toc a{display:flex;gap:10px;color:var(--ink-2);padding:6px 0 6px 14px;margin-left:-1px;line-height:1.5;
  border-left:2px solid transparent;transition:color .2s,border-color .2s}
.toc a .n{font-family:var(--mono);font-size:11.5px;color:var(--muted);padding-top:2px;flex:none;
  font-variant-numeric:tabular-nums}
.toc a:hover{color:var(--accent);text-decoration:none}
.toc a.active{color:var(--accent-ink);font-weight:600;border-left-color:var(--accent)}
.toc a.active .n{color:var(--accent)}
/* 两级目录:小节默认收起,当前大章展开 */
.toc .subs{display:none;padding:0 0 4px}
.toc .grp.open .subs{display:block}
.toc a.sub{font-size:13px;color:var(--muted);padding:3px 0 3px 40px}
.toc a.sub:hover{color:var(--accent)}
.toc a.sub.active{color:var(--accent-ink);font-weight:500;border-left-color:var(--accent-soft)}
/* 正文 */
.content{min-width:0}
.infobox{float:right;width:276px;margin:6px 0 24px 32px;border-radius:14px;
  background:var(--surface);box-shadow:var(--shadow);padding:18px 20px 16px}
.infobox .ih{font-size:12px;letter-spacing:.12em;color:var(--muted);padding-bottom:10px;
  border-bottom:1px solid var(--line)}
.infobox dt{font-size:12px;color:var(--muted);margin-top:12px}
.infobox dd{font-size:14px;color:var(--ink);margin-top:3px;font-weight:500}
.infobox dl.kv{display:grid;grid-template-columns:auto minmax(0,1fr);gap:9px 12px;margin-top:12px}
.infobox dl.kv dt{margin:0;padding-top:1px;white-space:nowrap;letter-spacing:.04em}
.infobox dl.kv dd{margin:0;font-size:13.5px;line-height:1.55;word-break:break-word}
.infobox .ty{color:var(--ink-2);letter-spacing:.04em}
.tags{display:flex;flex-wrap:wrap;gap:5px;margin-top:6px}
.tag{background:var(--paper);color:var(--ink-2);font-size:12px;font-weight:400;padding:2px 8px;
  border-radius:5px;border:1px solid var(--line)}
.stat{display:grid;grid-template-columns:repeat(auto-fit,minmax(56px,1fr));gap:8px;
  border-top:1px solid var(--line);margin-top:16px;padding-top:14px}
.stat b{display:block;font-family:var(--serif);font-size:24px;line-height:1.2;color:var(--ink);
  font-weight:700;font-variant-numeric:tabular-nums}
.stat span{font-size:11.5px;color:var(--muted)}
section.sec{padding-top:12px;scroll-margin-top:24px}
section.sec h2{font-family:var(--serif);font-size:26px;line-height:1.35;color:var(--ink);font-weight:700;
  letter-spacing:-.005em;margin:36px 0 14px;display:flex;align-items:baseline;gap:14px;text-wrap:balance}
section.sec h2 .n{font-family:var(--mono);color:var(--accent);font-weight:500;font-size:13px;
  letter-spacing:.04em;flex:none;transform:translateY(-3px)}
section.sec p{margin:14px 0;text-align:justify;color:var(--ink);text-wrap:pretty}
section.sec h3{font-family:var(--serif);font-size:19px;color:var(--ink);font-weight:700;margin:28px 0 8px}
section.sec h4{font-size:16px;color:var(--ink);font-weight:600;margin:20px 0 6px}
.tbl{overflow-x:auto;margin:18px 0;border-radius:10px;background:var(--surface);box-shadow:var(--shadow)}
.tbl table{border-collapse:collapse;width:100%;font-size:14px;line-height:1.6;font-variant-numeric:tabular-nums}
.tbl th,.tbl td{padding:9px 14px;text-align:left;vertical-align:top;border-bottom:1px solid var(--line)}
.tbl th{color:var(--muted);font-weight:500;font-size:12.5px;white-space:nowrap;background:transparent}
.tbl tr:last-child td{border-bottom:none}
.tbl tbody tr{transition:background .2s}
.tbl tbody tr:hover td{background:var(--paper)}
section.sec ul,section.sec ol{margin:12px 0 12px 24px}
section.sec li{margin:6px 0;padding-left:2px}
section.sec li::marker{color:var(--muted)}
strong{color:var(--ink);font-weight:600;background:linear-gradient(transparent 66%,var(--mark) 66%)}
blockquote{border-left:2px solid var(--accent);padding:4px 0 4px 18px;margin:18px 0;color:var(--ink-2);
  font-family:var(--serif);font-size:17px}
sup.fn{line-height:0}
sup.fn a{color:var(--accent);font-family:var(--mono);font-size:10.5px;font-weight:500;padding:0 1px;
  text-decoration:none}
sup.fn a:hover{color:var(--accent-ink);background:var(--accent-soft);border-radius:3px}
/* 配图 */
.infobox .hero-fig{margin:-18px -20px 14px;border-radius:14px 14px 0 0;overflow:hidden}
.infobox .hero-fig img{display:block;width:100%;aspect-ratio:4/3;object-fit:cover;background:var(--line)}
.infobox .hero-fig figcaption{padding:8px 20px 0;font-size:12px;line-height:1.55;color:var(--muted)}
figure.fig{margin:22px 0;background:var(--surface);border-radius:12px;box-shadow:var(--shadow);overflow:hidden}
figure.fig img{display:block;width:100%;max-height:460px;object-fit:cover;background:var(--line)}
figure.fig.side{float:right;width:300px;margin:6px 0 16px 28px}
figure.fig.side img{max-height:400px}
figure.fig figcaption{padding:9px 14px 11px;font-size:13px;line-height:1.6;color:var(--ink-2)}
.fig-row{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin:22px 0;clear:both}
.fig-row figure.fig{margin:0;display:flex;flex-direction:column}
.fig-row figure.fig img{height:240px;max-height:none}
/* 版面规划:侧栏 s/m/l、左浮、通栏 m/l、三联、竖图组 */
figure.fig.side-left{float:left;width:300px;margin:6px 28px 16px 0}
figure.fig.side-left img{max-height:400px}
figure.fig.side.s,figure.fig.side-left.s{width:220px}
figure.fig.side.m,figure.fig.side-left.m{width:280px}
figure.fig.side.l,figure.fig.side-left.l{width:340px}
figure.fig.side.l img,figure.fig.side-left.l img{max-height:460px}
figure.fig.wide{clear:both;margin:26px auto}
figure.fig.wide.m{width:75%}
figure.fig.wide img{max-height:520px}
.fig-row.trio{grid-template-columns:repeat(3,1fr);gap:12px}
.fig-row.trio figure.fig img{height:190px}
.fig-row.tall figure.fig img{height:340px}
.fig-row.trio.tall figure.fig img{height:300px}
.fno{font-family:var(--mono);font-size:11.5px;font-weight:600;color:var(--accent);margin-right:8px;
  font-variant-numeric:tabular-nums}
section.sec h3.clr{clear:both}
section.sec p[data-fig]{border-radius:4px;transition:background .25s,box-shadow .25s}
section.sec p.hl{background:var(--accent-soft);box-shadow:0 0 0 6px var(--accent-soft)}
figure.fig{transition:box-shadow .25s}
figure.fig.hl{box-shadow:0 0 0 2px var(--accent),var(--shadow)}
.fig .src,.hero-fig .src{color:var(--muted);white-space:nowrap}
.fig .src a,.hero-fig .src a{color:var(--muted)}
.fig .src a:hover,.hero-fig .src a:hover{color:var(--accent)}
section.sec{display:flow-root}
/* 参考/脚注 */
.refs{margin-top:56px;padding-top:28px;border-top:1px solid var(--line-2);clear:both}
.refs h2{font-family:var(--serif);font-size:22px;color:var(--ink);margin-bottom:12px}
.refs ol{list-style:none}
.refs li{position:relative;padding:10px 10px 10px 52px;border-bottom:1px solid var(--line);
  font-size:14px;line-height:1.65;border-radius:6px;transition:background .3s}
.refs li:last-child{border-bottom:none}
.refs li:before{content:attr(data-n);position:absolute;left:10px;top:11px;width:30px;text-align:right;
  color:var(--muted);font-family:var(--mono);font-size:12px;font-variant-numeric:tabular-nums}
.refs li:target{background:var(--mark)}
.refs .u{display:block;color:var(--muted);font-size:12px;word-break:break-all;margin-top:2px}
.refs .u:hover{color:var(--accent)}
.refs .back{margin-left:6px;font-size:12px;color:var(--muted)}
.footer{max-width:1080px;margin:40px auto 0;padding:24px 28px 56px;color:var(--muted);font-size:13px;line-height:1.8}
.footer p{max-width:42em;padding-top:20px;border-top:1px solid var(--line)}
/* 移动端折叠目录 */
.toc-toggle{display:none}
@media(max-width:900px){
  .layout{grid-template-columns:minmax(0,1fr);gap:0}
  .toc{position:static;max-height:none;background:var(--surface);border-radius:12px;box-shadow:var(--shadow);
    padding:6px 14px;margin-bottom:12px}
  .toc h4{display:none}
  .toc-list{border-left:none;padding:4px 0 8px}
  .toc a{padding-left:4px;border-left:none}
  .toc.collapsed .toc-list{display:none}
  .toc-toggle{display:flex;align-items:center;justify-content:space-between;width:100%;
    background:none;border:none;font:inherit;font-weight:600;color:var(--ink);cursor:pointer;padding:8px 0}
  .toc-toggle:after{content:"";width:7px;height:7px;border:solid var(--muted);border-width:0 1.5px 1.5px 0;
    transform:rotate(45deg) translateY(-2px);transition:transform .2s}
  .toc.collapsed .toc-toggle:after{transform:rotate(-45deg)}
  .infobox{float:none;width:auto;margin:0 0 20px}
  figure.fig.side{float:none;width:auto;margin:20px 0}
  figure.fig.side.s,figure.fig.side.m,figure.fig.side.l,figure.fig.side-left{float:none;width:auto;max-width:420px;
    margin:20px auto}
  figure.fig.wide.m{width:auto}
  .fig-row,.fig-row.trio{grid-template-columns:1fr}
  .fig-row.tall figure.fig img,.fig-row.trio figure.fig img{height:auto;max-height:420px}
  .hero{padding:40px 20px 28px}
  .layout{padding:0 20px}
  .footer{padding:20px 20px 40px}
}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}*{transition:none!important}}
@media print{.toc,.progress{display:none}.layout{display:block}body{background:#fff}}
"""

JS = """
// 滚动高亮当前章节(大章高亮并展开其小节;小节单独高亮)
const links=[...document.querySelectorAll('.toc a')];
const map=new Map(links.map(a=>[a.getAttribute('href').slice(1),a]));
const grps=[...document.querySelectorAll('.toc .grp')];
const mark=(sel,a)=>{links.filter(l=>l.matches(sel)).forEach(l=>{l.classList.remove('active');l.removeAttribute('aria-current');});
  if(a){a.classList.add('active');a.setAttribute('aria-current','true');}};
const obs=new IntersectionObserver((es)=>{
  es.forEach(e=>{if(!e.isIntersecting)return;const a=map.get(e.target.id);
    if(e.target.matches('section.sec')){mark('a:not(.sub)',a);mark('a.sub',null);
      grps.forEach(g=>g.classList.toggle('open',!!a&&g.contains(a)));}
    else if(a){mark('a.sub',a);}
  });
},{rootMargin:'-10% 0px -75% 0px',threshold:0});
document.querySelectorAll('section.sec, section.sec h3[id]').forEach(s=>obs.observe(s));
// 阅读进度条(transform,避免重排)
const bar=document.querySelector('.progress');
let ticking=false;
const paint=()=>{const d=document.documentElement,max=d.scrollHeight-d.clientHeight;
  bar.style.transform=`scaleX(${max>0?Math.min(1,d.scrollTop/max):0})`;ticking=false;};
addEventListener('scroll',()=>{if(!ticking){ticking=true;requestAnimationFrame(paint);}},{passive:true});
paint();
// 移动端折叠目录
const MOBILE=900,toc=document.querySelector('.toc'),tg=document.querySelector('.toc-toggle');
const setCollapsed=c=>{toc.classList.toggle('collapsed',c);tg.setAttribute('aria-expanded',String(!c));};
if(tg){tg.addEventListener('click',()=>setCollapsed(!toc.classList.contains('collapsed')));
  setCollapsed(window.innerWidth<=MOBILE);
  toc.querySelector('.toc-list').addEventListener('click',e=>{
    if(e.target.closest('a')&&window.innerWidth<=MOBILE)setCollapsed(true);});}
// 图文联动:悬停图片高亮它配的那一段,悬停该段高亮图片
const figOf=n=>document.getElementById('fig-'+n);
document.querySelectorAll('section.sec p[data-fig]').forEach(p=>{
  const nos=p.dataset.fig.split(' '),figs=nos.map(figOf).filter(Boolean);
  const on=v=>{p.classList.toggle('hl',v);figs.forEach(f=>f.classList.toggle('hl',v));};
  figs.forEach(f=>{f.addEventListener('mouseenter',()=>on(true));f.addEventListener('mouseleave',()=>on(false));});
  p.addEventListener('mouseenter',()=>figs.forEach(f=>f.classList.add('hl')));
  p.addEventListener('mouseleave',()=>figs.forEach(f=>f.classList.remove('hl')));
});
"""


def _fig_source(fig, ref_by_url) -> str:
    """生成图注来源链接。"""
    n = ref_by_url.get(fig.get("page", ""))
    if n:
        return f'<span class="src"> · 来源 <a href="#fn-{n}">[{n}]</a></span>'
    site = html.escape(fig.get("site") or "原网页")
    return (f'<span class="src"> · 图源 <a href="{html.escape(fig.get("page", ""))}" target="_blank" '
            f'rel="noopener noreferrer">{site}</a></span>')


def _figure(fig, ref_by_url, side: bool) -> str:
    cap = html.escape(fig.get("caption", ""))
    w, h = fig.get("w") or 0, fig.get("h") or 0
    size = f' width="{w}" height="{h}"' if w and h else ""
    return (f'<figure class="fig{" side" if side else ""}"><img src="{html.escape(fig["src"])}" alt="{cap}"'
            f' loading="lazy" decoding="async" referrerpolicy="no-referrer"{size}'
            f' onerror="this.closest(\'figure\').remove()">'
            f'<figcaption>{cap}{_fig_source(fig, ref_by_url)}</figcaption></figure>')


_P_RE = re.compile(r"<p>(.*?)</p>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_ANCHOR_MIN = 0.08  # 匹配过低时使用首段。


def _bigrams(s: str) -> set[str]:
    s = re.sub(r"[\s\W_]+", "", s or "")
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _anchor_para(body_html: str, anchor: str):
    """返回与锚点最匹配的段落。"""
    paras = list(_P_RE.finditer(body_html))
    if not paras:
        return None
    a = _bigrams(anchor)
    if len(a) < 2:
        return paras[0]
    ov = [len(a & _bigrams(_TAG_RE.sub("", m.group(1)))) / len(a) for m in paras]
    k = max(range(len(paras)), key=lambda i: ov[i])
    return paras[k] if ov[k] >= _ANCHOR_MIN else paras[0]


def _with_figure(body_html: str, fig, ref_by_url) -> str:
    """将章节图片插入与图注最相关的段落旁。"""
    if not fig:
        return body_html
    figs = fig if isinstance(fig, list) else [fig]
    anchor = " ".join(f.get("anchor") or f.get("caption", "") for f in figs)
    if len(figs) > 1:
        tag, after = ('<div class="fig-row">' + "".join(_figure(f, ref_by_url, False) for f in figs)
                      + "</div>"), True
    else:
        w, h = figs[0].get("w") or 4, figs[0].get("h") or 3
        side = w / h < 1.3
        tag, after = _figure(figs[0], ref_by_url, side), not side
    m = _anchor_para(body_html, anchor)
    if m is None:
        return tag + body_html
    i = m.end() if after else m.start()
    return body_html[:i] + tag + body_html[i:]


def _fig_el(fig, ref_by_url, cls: str, no: int) -> str:
    """渲染版面规划中的单张图片。"""
    cap = html.escape(fig.get("caption", ""))
    w, h = fig.get("w") or 0, fig.get("h") or 0
    size = f' width="{w}" height="{h}"' if w and h else ""
    klass = ("fig " + cls).strip()
    return (f'<figure class="{klass}" id="fig-{no}" data-no="{no}">'
            f'<img src="{html.escape(fig["src"])}" alt="{cap}" loading="lazy" decoding="async"'
            f' referrerpolicy="no-referrer"{size} onerror="this.closest(\'figure\').remove()">'
            f'<figcaption><span class="fno">图{no}</span>{cap}{_fig_source(fig, ref_by_url)}</figcaption></figure>')


def _with_blocks(body_html: str, blocks: list[dict], ref_by_url, counter: list[int]) -> str:
    """按版面规划将图片插入指定段落。"""
    paras = list(_P_RE.finditer(body_html))
    before, after, marks = {}, {}, {}
    for b in sorted(blocks, key=lambda b: b.get("para", 0)):
        figs = b.get("figs") or []
        if not figs:
            continue
        nos = list(range(counter[0] + 1, counter[0] + 1 + len(figs)))
        counter[0] += len(figs)
        mode, size = b.get("mode", "side"), b.get("size", "m")
        if mode in ("pair", "trio"):
            tall = all((f.get("w") or 4) / max(f.get("h") or 3, 1) < 0.85 for f in figs)
            tag = (f'<div class="fig-row {mode}{" tall" if tall else ""}">'
                   + "".join(_fig_el(f, ref_by_url, "", n) for f, n in zip(figs, nos)) + "</div>")
        else:
            tag = _fig_el(figs[0], ref_by_url, f"{mode} {size}", nos[0])
        k = min(max(int(b.get("para", 0)), 0), len(paras) - 1) if paras else -1
        (before if mode in ("side", "side-left") else after).setdefault(k, []).append((tag, mode))
        marks.setdefault(k, []).extend(nos)
    if not paras:
        return "".join(t for t, _ in before.get(-1, []) + after.get(-1, [])) + body_html

    out, last, pending_clear = [], 0, False
    for idx, m in enumerate(paras):
        gap = body_html[last:m.start()]
        if pending_clear and "<h3" in gap:
            gap, pending_clear = gap.replace("<h3", '<h3 class="clr"', 1), False
        out.append(gap)
        for tag, _ in before.get(idx, []):
            out.append(tag)
            pending_clear = True
        p = m.group(0)
        if idx in marks:
            p = f'<p data-fig="{" ".join(map(str, marks[idx]))}">' + p[3:]
        out.append(p)
        out.extend(tag for tag, _ in after.get(idx, []))
        last = m.end()
    tail = body_html[last:]
    if pending_clear and "<h3" in tail:
        tail = tail.replace("<h3", '<h3 class="clr"', 1)
    out.append(tail)
    return "".join(out)


def _hero_style(hero) -> str:
    """根据原图比例选择主图裁切框。"""
    r = (hero.get("w") or 4) / max(hero.get("h") or 3, 1)
    if r < 0.85:
        return "aspect-ratio:3/4;object-position:50% 18%"
    if r < 1.15:
        return "aspect-ratio:1/1;object-position:50% 30%"
    return "aspect-ratio:4/3" if r < 1.55 else "aspect-ratio:16/10"


def _infobox(title, dalei, sections, score, hero=None, ref_by_url=None, info=None) -> str:
    """渲染右侧信息栏，没有字段时显示类型和关键词。"""
    stat = ""
    if score and score.get("total") is not None:
        stat = f'<div class="stat"><div><b>{score["total"]:g}</b><span>重建评分</span></div></div>'
    fields = (info or {}).get("fields") or []
    valid_refs = set((ref_by_url or {}).values())
    if fields:
        rows = []
        for f in fields:
            refs = "".join(
                f'<sup class="fn"><a href="#fn-{r}">[{r}]</a></sup>'
                for r in f.get("refs") or [] if str(r) in valid_refs
            )
            rows.append(f'<dt>{html.escape(f["k"])}</dt><dd>{html.escape(f["v"])}{refs}</dd>')
        label = (info or {}).get("label") or dalei or "综合"
        body = (f'<div class="ih">词条信息 · <span class="ty">{html.escape(label)}</span></div>'
                f'<dl class="kv">{"".join(rows)}</dl>')
    else:
        tags = "".join(f'<span class="tag">{html.escape(t)}</span>'
                       for t, _ in sections[1:-1][:6]) or f'<span class="tag">{html.escape(title)}</span>'
        body = ('<div class="ih">词条信息</div><dl>'
                f'<dt>词条类型</dt><dd>{html.escape(dalei or "综合")}</dd>'
                f'<dt>关键词</dt><dd><div class="tags">{tags}</div></dd></dl>')
    return (
        '<aside class="infobox" aria-label="词条信息">'
        + (f'<figure class="hero-fig"><img src="{html.escape(hero["src"])}" alt="{html.escape(hero.get("caption", ""))}"'
           f' style="{_hero_style(hero)}" referrerpolicy="no-referrer" onerror="this.closest(\'figure\').remove()">'
           f'<figcaption>{html.escape(hero.get("caption", ""))}{_fig_source(hero, ref_by_url or {})}</figcaption></figure>'
           if hero else "")
        + body + stat + '</aside>'
    )


def _char_count(sections) -> int:
    """统计正文可见字数。"""
    text = "".join(b for _, b in sections)
    text = re.sub(r"\[\d+\]|[#*|>\-:`]|\s", "", text)
    return len(text)


def build_html(title, sections, sources, dalei="", score=None, lead=None, figures=None, infobox=None) -> str:
    """构建完整 HTML；缺少 lead 时使用首节首句。

    figures 支持 {hero, sections, blocks}，blocks 优先；infobox 使用 {label, fields:[{k,v,refs}]}。
    """
    figures = figures or {}
    sec_figs = figures.get("sections") or {}
    ref_by_url = {url: n for n, _, url in sources}
    lead_html = _inline(lead) if lead else _inline(_lead(sections))

    def _toc_group(i, t, b):
        subs = "".join(f'<a class="sub" href="#sec-{i}-{k}">{html.escape(s)}</a>'
                       for k, s in enumerate(_subheads(b)))
        return (f'<div class="grp"><a href="#sec-{i}"><span class="n">{i + 1:02d}</span>'
                f'<span>{html.escape(t)}</span></a>' + (f'<div class="subs">{subs}</div>' if subs else "") + "</div>")

    toc_items = "".join(_toc_group(i, t, b) for i, (t, b) in enumerate(sections))
    blocks_by_ch = {}
    for blk in figures.get("blocks") or []:
        blocks_by_ch.setdefault(blk.get("chapter"), []).append(blk)
    counter = [0]

    def _sec_body(i, t, b):
        body = _render_blocks(b, id_prefix=f"sec-{i}")
        if blocks_by_ch:
            return _with_blocks(body, blocks_by_ch.get(t, []), ref_by_url, counter)
        return _with_figure(body, sec_figs.get(t), ref_by_url)

    secs = "".join(
        f'<section class="sec" id="sec-{i}" aria-labelledby="h-{i}">'
        f'<h2 id="h-{i}"><span class="n" aria-hidden="true">{i + 1:02d}</span>'
        f'<span>{html.escape(t)}</span></h2>'
        f'{_sec_body(i, t, b)}</section>'
        for i, (t, b) in enumerate(sections)
    )
    refs = "".join(
        f'<li id="fn-{n}" data-n="{n}"><span>{html.escape(txt)}</span>'
        f'<a class="u" href="{html.escape(url)}" target="_blank" rel="noopener noreferrer">{html.escape(url)}</a>'
        f'<a class="back" href="#fnref-{n}" aria-label="返回正文引用处 {n}">↩</a></li>'
        for n, txt, url in sources
    )
    kicker = "百科词条" + (f" · {html.escape(dalei)}" if dalei else "")
    meta = (
        f'<span><b>{len(sections)}</b>章节</span>'
        f'<span><b>{len(sources)}</b>参考来源</span>'
        f'<span><b>{_char_count(sections):,}</b>字</span>'
    )
    desc = html.escape(re.sub(r"\[\d+\]|\*\*", "", lead or _lead(sections))[:120], quote=True)
    favicon = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
               "%3Crect width='32' height='32' rx='8' fill='%232d6a88'/%3E%3Cpath d='M10 9h9a3 3 0 0 1 3 3v11"
               "H13a3 3 0 0 1-3-3z' fill='none' stroke='%23fffefb' stroke-width='2'/%3E%3C/svg%3E")
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} · 百科词条</title>
<meta name="description" content="{desc}">
<meta property="og:title" content="{html.escape(title, quote=True)}">
<meta property="og:description" content="{desc}">
<link rel="icon" href="{favicon}">
<style>{CSS}</style></head><body>
<div class="progress" aria-hidden="true"></div>
<header class="hero"><div class="hero-in">
  <div class="kicker"><span class="dot" aria-hidden="true"></span>{kicker}</div>
  <h1>{html.escape(title)}</h1>
  <p class="lead">{lead_html}</p>
  <div class="meta">{meta}</div>
</div></header>
<div class="layout">
  <nav class="toc" aria-label="本页目录">
    <button class="toc-toggle" type="button" aria-expanded="true">目录</button>
    <div class="toc-list"><h4>本页目录</h4>{toc_items}</div>
  </nav>
  <main class="content" id="content">
    {_infobox(title, dalei, sections, score, figures.get("hero"), ref_by_url, infobox)}
    {secs}
    <section class="refs" aria-labelledby="refs-h"><h2 id="refs-h">参考资料</h2><ol>{refs}</ol></section>
  </main>
</div>
<footer class="footer"><p>
  本词条由百科重建 Agent 依据公开资料自动调研与撰写,所有事实均附行内脚注来源,仅供效果展示,不代表官方表述。
</p></footer>
<script>{JS}</script>
</body></html>"""


@ls.traceable(name="render_markdown")
def render_markdown(md: str, dalei: str = "", score=None, figures=None, infobox=None) -> str:
    """将 Markdown 报告转换为 HTML。"""
    title, sections, sources = parse_report(md)
    return build_html(title, sections, sources, dalei=dalei, score=score, lead=extract_preamble(md),
                      figures=figures, infobox=infobox)


def render(run_dir: str) -> str:
    with open(os.path.join(run_dir, "final_report.md"), encoding="utf-8") as f:
        md = f.read()
    dalei, score = "", None
    sp = os.path.join(run_dir, "score.json")
    if os.path.exists(sp):
        score = json.load(open(sp, encoding="utf-8"))
    rp = os.path.join(run_dir, "route.txt")
    route_txt = open(rp, encoding="utf-8").read() if os.path.exists(rp) else (
        score.get("route", "") if score else "")
    m = re.search(r"大类[「\"]?([^」\"()\n]+)", route_txt)
    if m:
        dalei = m.group(1).strip()
    title, sections, sources = parse_report(md)
    figures = None
    fp = os.path.join(run_dir, "figures.json")
    if os.path.exists(fp):
        with open(fp, encoding="utf-8") as f:
            figures = json.load(f)
    infobox = None
    ip = os.path.join(run_dir, "infobox.json")
    if os.path.exists(ip):
        with open(ip, encoding="utf-8") as f:
            infobox = json.load(f) or None
    out = render_markdown(md, dalei=dalei, score=score, figures=figures, infobox=infobox)
    out_path = os.path.join(run_dir, "index.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(out)
    sec_figs = (figures or {}).get("sections") or {}
    blocks = (figures or {}).get("blocks") or []
    n_sec = (sum(len(b.get("figs") or []) for b in blocks) if blocks
             else sum(len(v) if isinstance(v, list) else 1 for v in sec_figs.values()))
    n_fig = n_sec + bool((figures or {}).get("hero"))
    lay = ((figures or {}).get("planner") or {}).get("status") or ("rule" if figures else "-")
    print(f"✅ {out_path}\n   {title} | {len(sections)} 节 | {len(sources)} 来源 | 配图 {n_fig}"
          f"(版面 {lay}) | 类型={dalei or '-'}")
    return out_path


if __name__ == "__main__":
    d = sys.argv[1] if len(sys.argv) > 1 else "runs/问界M9_dynoutline"
    if not os.path.isabs(d):
        d = os.path.join(os.path.dirname(os.path.abspath(__file__)), d)
    render(d)
