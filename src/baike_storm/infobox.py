"""信息栏(infobox):按词条类型组给建议字段 → LLM 从成文正文抽取 → 校验来源编号。

值只从**正文**里抽(正文每个事实都已带 [n]),保证信息栏与正文口径一致、不引入未核实事实。
产物 infobox.json = [{"k","v","refs"}];不进 markdown,score / entity_recall 不受影响。
"""

import re
from . import tracing as ls

from .entry_types import GROUP_LABELS, type_group

FIELDS = {
    "person": ["中文名", "外文名", "别名", "国籍", "民族", "出生日期", "出生地", "逝世日期", "毕业院校", "职业",
               "现任职务", "所属机构/球队", "主要成就", "代表作品"],
    "org": ["中文名", "外文名", "简称", "性质/类型", "成立时间", "总部地点", "创始人", "现任负责人", "上级/母公司",
            "经营范围", "员工数", "年营业额", "官方网站"],
    "product": ["中文名", "外文名", "品牌", "厂商", "类型/级别", "发布时间", "上市时间", "售价", "动力/核心配置",
                "主要尺寸/规格", "前代产品", "累计销量"],
    "software": ["中文名", "外文名", "开发商", "类型", "发布时间", "最新版本", "运行平台", "底层技术/模型",
                 "用户规模", "授权方式", "官方网站"],
    "work": ["中文名", "外文名", "类型", "作者/导演", "主演/配音", "制作公司", "发行公司", "首发/上映时间", "平台",
             "集数/片长/篇幅", "主要奖项", "票房/销量"],
    "event": ["中文名", "时间", "地点", "主办/发起方", "参与方", "规模", "结果", "伤亡/损失", "影响"],
    "place": ["中文名", "外文名", "所属国家/地区", "地理位置", "面积", "长度/海拔", "人口", "设立/建成时间",
              "行政区类别", "保护级别", "著名景点"],
    "organism": ["中文名", "学名", "别名", "界", "门", "纲", "目", "科", "属", "分布区域", "保护级别"],
    "medical": ["中文名", "英文名", "就诊科室", "多发群体", "常见症状/适应症", "传播途径", "治疗方法", "常用药物"],
    "food": ["中文名", "外文名", "所属菜系/产地", "主要食材", "口味", "烹饪方法", "主要营养成分"],
    "concept": ["中文名", "外文名", "提出者", "提出时间", "所属学科/领域", "适用范围"],
    "space": ["中文名", "外文名", "所属工程", "研制单位", "运载火箭", "发射时间", "发射地点", "着陆/返回时间",
              "探测器质量", "任务目标", "任务结果"],
    "generic": ["中文名", "外文名", "类别", "时间", "地点", "相关方"],
}
MAX_FIELDS = 16
MAX_VALUE_LEN = 60


@ls.traceable(name="build_infobox")
def build_infobox(title: str, dalei: str | None, article: str, llm, parse_json) -> dict:
    """→ {"group","label","fields":[{k,v,refs}]}。llm(prompt)->str;parse_json(text, fallback)。"""
    from . import prompts_storm as P

    group = type_group(title, dalei)
    label = (dalei or GROUP_LABELS[group]) if group != "space" else GROUP_LABELS["space"]
    cited = {int(x) for x in re.findall(r"\[(\d+)\]", article)}
    prompt = P.INFOBOX.format(title=title, type_label=label, fields="、".join(FIELDS[group]),
                              article=article[:60000])
    raw = parse_json(llm(prompt), fallback=[])
    fields, seen = [], set()
    for it in raw if isinstance(raw, list) else []:
        if not isinstance(it, dict):
            continue
        k, v = str(it.get("k") or "").strip(), str(it.get("v") or "").strip()
        if not k or not v or k in seen or re.search(r"未(提及|涉及|知)|不详|暂无|无相关", v):
            continue
        v = re.sub(r"\s*\[\d+\]", "", v)[:MAX_VALUE_LEN]
        refs = []
        for r in it.get("refs") or []:
            try:
                r = int(r)
            except (TypeError, ValueError):
                continue
            if r in cited and r not in refs:
                refs.append(r)
        seen.add(k)
        fields.append({"k": k, "v": v, "refs": refs[:2]})
        if len(fields) >= MAX_FIELDS:
            break
    return {"group": group, "label": label, "fields": fields}
