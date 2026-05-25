from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence


GLM_SYSTEM_PROMPT = """你要分析同一个网页流程中的一小段连续截图。

请严格返回 JSON，格式如下：
{
  "session_summary": "简短中文总结",
  "observed_actions": [
    {
      "step_number": 1,
      "action": "click|type|select|wait|scroll|open",
      "target": "页面上真实可见的中文控件文字、字段标签、按钮名、栏目名，或非常具体的页面区域描述",
      "value": "如果有输入内容，填写真实输入值",
      "evidence": "说明截图中哪些可见信息支持这个判断",
      "confidence": 0.0
    }
  ],
  "uncertainties": ["可选的不确定说明"]
}

规则：
- 只能根据截图中真正可见的信息推断，不要脑补不存在的页面元素。
- 输出必须优先使用中文。
- 你的目标不是写摘要，而是为后续“网页回放执行器”提供可执行的原子动作序列。
- 每一步只允许表达一个原子动作；如果一个结果依赖先展开菜单、先切换页签、先等待页面加载，必须拆成多步。
- 如果用户先点击父菜单/导航分组/折叠面板，再点击子项，必须分别输出两步，禁止只保留子项点击。
- 如果点击后明显发生了页面切换、路由切换、列表页加载、详情页打开，应该补出 `wait` 或 `open` 这类过渡动作，而不是直接跳到下一次点击。
- 如果动作发生在列表页中，必须明确是“第一条/当前可见第一条/第 N 行/标题包含某关键词”的哪一项，禁止只写模糊的“某条公告/某个结果”。
- 如果截图显示已经进入详情页，前面的动作不能只写成“点击出让公告”，而应尽量保留“进入列表页”与“打开详情页”两个层次。
- `target` 必须尽量写成页面上真实可见的文字，不要写抽象总结词。
- 禁止输出像“first announcement”“公告保存”“结果项”“入口按钮”“某个链接”这种泛化或抽象目标。
- 如果页面上能看到明确文字，就直接写该文字；如果看不到完整文字，再用具体区域描述补充。
- 如果是列表中的某一项，要写成“第一条公告标题… / 列表中第一条… / 表格第一行的…”这种更具体的描述。
- 如果输入值不清楚，要在 `evidence` 中说明不确定。
- 不要用 markdown 代码块包裹 JSON。
"""


DEEPSEEK_SYSTEM_PROMPT = """你要把截图分析得到的原始动作，整理成可执行的中文网页回放计划。

请严格返回 JSON，格式如下：
{
  "sop": [
    "面向人阅读的中文步骤"
  ],
  "plan": {
    "site_url": "完整站点地址",
    "steps": [
      {
        "step_number": 1,
        "action": "click|type|select|wait|scroll|open",
        "target": "页面上真实可见的中文目标文本或更具体的目标描述",
        "value": "可选值",
        "selector_hint": "可选的 CSS 或更稳定的定位提示",
        "notes": "可选说明"
      }
    ]
  },
  "assumptions": [
    "可选假设"
  ]
}

规则：
- 输出必须优先使用中文。
- 计划既要可执行，也要足够细，宁可多拆一步，也不要把多个动作压缩成一个抽象步骤。
- 如果先点击父菜单、导航分组或折叠面板，再点击其中的子项，必须保留为两个独立步骤，禁止合并成一步。
- 如果一个点击之后才会出现可执行的后续目标，应该在 SOP 中明确写出“等待列表页出现 / 等待详情页出现 / 等待路由切换完成”这类过渡说明。
- 对列表页和详情页要区分层级：进入列表页不是打开详情页，打开详情页也不是下载或保存。
- 如果原始分析只能提供不完整证据，优先保留页面层级关系和状态切换，不要为了简短省略关键过渡步骤。
- `target` 必须尽量贴近真实页面可见文案，禁止只写概念词、总结词、英文抽象词。
- 禁止输出像“公告保存”“first announcement”“search result”“entry button”“target item”这种不贴页面的目标。
- 如果动作发生在列表、表格、搜索结果中，必须明确是“第一条/第二条/表格第一行/搜索结果第一项”等。
- 如果可以从原始分析中看出更稳定的控件含义，要保留足够细节，避免目标过短。
- 只允许使用支持的动作类型。
- 保持步骤顺序。
- 不要用 markdown 代码块包裹 JSON。
"""


def build_glm_user_prompt(
    site_url: str,
    frame_paths: Iterable[Path],
    user_request: str | None = None,
    frame_hints: Sequence[str] | None = None,
) -> str:
    frame_path_list = list(frame_paths)
    frame_names = ", ".join(path.name for path in frame_path_list)
    prompt = (
        f"目标网站：{site_url}\n"
        f"按时间顺序排列的截图：{frame_names}\n"
        "请根据这些连续截图，推断用户在相邻截图之间最可能执行的网页操作。"
    )
    if frame_hints:
        prompt += "\n按时间顺序提供的监听提示："
        for index, frame_path in enumerate(frame_path_list):
            hint = frame_hints[index] if index < len(frame_hints) else ""
            if hint:
                prompt += f"\n- {frame_path.name}: {hint}"
        prompt += "\n这些提示只是辅助线索；如果提示和截图可见内容不一致，请以截图可见内容为准。"
    if user_request:
        prompt += f"\n用户目标或希望完成的结果：{user_request}"
    return prompt


def build_deepseek_user_prompt(site_url: str, raw_analysis_json: str, user_request: str | None = None) -> str:
    prompt = (
        f"目标网站：{site_url}\n"
        "请把下面这份原始网页流程分析结果，整理成中文 SOP 和可执行回放计划。\n"
        f"{raw_analysis_json}"
    )
    if user_request:
        prompt += f"\n在整理步骤时，请优先围绕这个用户目标：{user_request}"
    return prompt
