"""Deprecated Vertex AI prototype orchestrator.

Active execution entrypoints are `mcp_servers/playwright_server.py` for browser
automation and `frontend/scheduler_worker.py` for scheduled application runs.
This module is retained only as an archived prototype and must not be imported
by production execution paths.
"""

import os

DEPRECATED_ORCHESTRATOR = True
ACTIVE_ORCHESTRATOR_ENTRYPOINT = "mcp_servers/playwright_server.py + frontend/scheduler_worker.py"

if os.getenv("ENABLE_DEPRECATED_VERTEX_ORCHESTRATOR") != "1":
    raise RuntimeError(
        "app_orchestrator.py is deprecated. Use mcp_servers/playwright_server.py "
        "and frontend/scheduler_worker.py; final Submit requires explicit human approval."
    )

from typing import Dict, Any, List
# 导入 Google Cloud Agent SDK 核心组件
from vertexai.preview import agents
from vertexai.preview.agents import Agent, Playbook, Tool

# ==========================================
# 🛠️ 第一部分：定义 MCP 工具 (Tools)
# 这些工具将作为外部接口挂载到对应的 Agent 上
# ==========================================

# Resolve local paths for openapi specifications
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
spec_path = lambda filename: os.path.join(BASE_DIR, "openapi_specs", filename)

# 1. MongoDB MCP 工具：负责简历版本管理和投递状态读写
mongodb_mcp_tool = Tool.from_openapi_action(
    name="mongodb_mcp_tool",
    description="用于连接工作网记忆数据库。支持保存简历修改稿(V0->V1)以及更新投递状态(Applied/Interview/Rejected)。",
    openapi_spec=spec_path("mongodb_openapi.yaml")
)

# 2. Elastic Search MCP 工具：负责毫秒级岗位精准检索
elastic_search_tool = Tool.from_openapi_action(
    name="elastic_search_tool",
    description="大模型专属眼睛。输入提取出的技能关键词，在全网数万个最新招聘岗位(JD)中进行向量语义检索。",
    openapi_spec=spec_path("elastic_openapi.yaml")
)

# 3. Arize Phoenix MCP 工具：负责防止大模型瞎编的质检审计
arize_audit_tool = Tool.from_openapi_action(
    name="arize_audit_tool",
    description="调用 Arize Phoenix 运行自动化 LLM-as-a-Judge 评估。比对修改后简历与原始简历，计算事实一致性得分(Faithfulness Score)。",
    openapi_spec=spec_path("arize_openapi.yaml")
)

# 4. Playwright 浏览器自动化工具：负责控制电脑自动填表投递
playwright_execution_tool = Tool.from_openapi_action(
    name="playwright_execution_tool",
    description="自动控制浏览器。接收最终版 PDF 简历和目标岗位 URL，执行自动填表并点击提交投递。",
    openapi_spec=spec_path("playwright_openapi.yaml")
)

# 5. Email MCP 工具：负责获取验证码和同步状态
email_mcp_tool = Tool.from_openapi_action(
    name="email_mcp_tool",
    description="用于连接用户邮箱。支持获取最新邮件验证码(OTP)，以及同步收件箱里的拒信/面试通知邮件状态。",
    openapi_spec=spec_path("email_openapi.yaml")
)


# ==========================================
# 👥 第二部分：声明多智能体团队 (Sub-Agents)
# ==========================================

# 1. 数据策略师 Agent：负责自适应复盘与策略注入
data_strategist_agent = Agent(
    display_name="Data_Strategist_Agent",
    model="gemini-3.5-flash",  # 快速处理结构化历史数据
    instruction="""你是一个求职数据分析专家。
    任务：
    1. 定期读取 MongoDB 里的投递反馈记录。
    2. 分析哪些简历修改策略（高亮了哪些技能）最终拿到了面试（SUCCESS_PATTERN），哪些被拒（FAILURE_PATTERN）。
    3. 在简历美化师（Optimizer）开始工作前，将成功的黄金词汇和避坑指南注入进工作流。""",
    tools=[mongodb_mcp_tool, email_mcp_tool]
)

# 2. 简历美化师 Agent：负责结合岗位特征改写简历
optimizer_agent = Agent(
    display_name="Optimizer_Agent",
    model="gemini-2.5-pro",  # 需要高超的文本包装和理解能力
    instruction="""你是一个王牌简历包装专家。
    任务：
    1. 接收用户的原始简历(V0)以及由 Elastic 检索出的岗位目标描述(JD)。
    2. 参考 Data_Strategist 提供的策略注入包（历史成功经验）。
    3. 产出一份高度契合目标岗位、使用了精漂亮行业黑话的‘简历修改稿(V1)’。
    4. 禁止凭空捏造用户完全没有接触过的公司 and 背景。""",
    tools=[elastic_search_tool]
)

# 3. 毒舌审计员 Agent：负责互相监督、揪出幻觉
auditor_agent = Agent(
    display_name="Auditor_Agent",
    model="gemini-2.5-pro",  # 负责严格审查与严密逻辑判定
    instruction="""你是一个严苛、充满怀疑精神的简历合规质检员。你的工作是跟【简历美化师】进行针锋相对的互相监督。
    任务：
    1. 拿到原始简历(V0)和修改稿(V1)。
    2. 调用 Arize Phoenix 工具运行事实一致性审计。
    3. 如果 Arize 返回的 Faithfulness 评分低于 0.85（说明美化师在严重造假），你必须无情地驳回该任务，命令【简历美化师】重新修改，并指出造假位置。
    4. 只有评分通过，你才可以将最终版简历放行给投递执行官。""",
    tools=[arize_audit_tool]
)

# 4. 投递执行官 Agent：负责真人级自动化填表投递
execution_agent = Agent(
    display_name="Execution_Agent",
    model="gemini-3.5-flash",  # 负责精确遵循步骤去调用工具
    instruction="""你是一个绝对服从命令的投递自动化机器人。
    任务：
    1. 接收【毒舌审计员】放行的最终版简历 PDF 路径以及目标岗位 URL。
    2. 调用 Playwright 工具打开目标网站，解析表单字段，将用户的结构化数据自动填入。
    3. 如果工具返回 BLOCKED_ON_QUESTIONS，必须停止当前 application，并把 grouped question blockers 返回给用户审批。
    4. 禁止猜测签证、工作授权、残障、退伍军人、犯罪记录、性别、种族、利益冲突、电子签名、薪资等敏感答案。
    5. 只有 approved answer 已存在且工具明确返回 READY_TO_RESUME / READY_TO_SUBMIT 时，才允许进入下一阶段。
    6. 禁止自动点击真实最终 Submit；本地流程必须停在人工确认前。""",
    tools=[playwright_execution_tool, mongodb_mcp_tool, email_mcp_tool]
)

# 5. 模拟面试官 Agent：负责在投递成功后，为面试者准备防拷打指南
interviewer_agent = Agent(
    display_name="Interviewer_Agent",
    model="gemini-2.5-pro",  # 模拟人类的高难度刁钻心理
    instruction="""你是一个极其老练且刁钻的硅谷大厂面试官。
    任务：
    1. 对比用户的原始简历(V0)和最终投递出的修改稿(V1)，精准找出那些‘美化程度最高、最容易被深挖穿帮’的改动点。
    2. 针对这些高危踩雷点，拟定 3 道极具杀伤力的追问问题。
    3. 基于用户真实的底子(V0)，利用 STAR 法则（情景、任务、行动、结果）为用户量身定制一套既表现专业、又绝对不会穿帮的防拷打答题话术卡。""",
    tools=[]
)


# ==========================================
# 🎯 第三部分：定义主编排剧本 (Orchestrator Playbook)
# ==========================================

# 首席规划官剧本：管理多智能体之间的任务流转顺序
orchestrator_playbook = Playbook(
    display_name="Job_Hunter_Orchestrator_Playbook",
    goal="""引导求职者上传简历，并统筹旗下的数据分析、简历美化、防幻觉审计、自动投递和面试准备等一系列多智能体协作流，帮助求职者无缝、安全地拿到面试机会。""",
    steps=[
        "1. 欢迎用户，引导用户上传其原始 PDF 简历 (V0)，并询问其意向的求职岗位类型、城市和薪资范围。",
        "2. 唤醒 [Data_Strategist_Agent]，对该用户所选的岗位类型进行历史投递复盘，获取当前的成功和失败特征包。",
        "3. 将特征包与原始简历(V0)共同移交给 [Optimizer_Agent]，命令其调用 Elastic Search 匹配岗位，并生成针对性的定制修改稿(V1)。",
        "4. 将修改稿(V1)与原始简历(V0)无缝对接给 [Auditor_Agent]。如果 Auditor 报告幻觉严重并触发 Rejected，则强制工作流倒流回第 3 步让 Optimizer 重写。直到 Auditor 给出 Approved 放行。",
        "5. 审核放行后，指派 [Execution_Agent] 启动浏览器自动化工具，在远端完成真实的表单填写和简历投递，并将结果持久化在数据库中。",
        "6. 投递完成后，呼叫 [Interviewer_Agent] 登场，为求职者生成个性化的‘防拷打面试准备小抄’，并优雅地结束本次全自动求职生命周期流程。"
    ],
    # 将刚才声明的 5 个子 Agent 注册到这个主编排中
    sub_agents=[
        data_strategist_agent,
        optimizer_agent,
        auditor_agent,
        execution_agent,
        interviewer_agent
    ]
)

# ==========================================
# 🚀 第四部分：打包初始化 Agent Platform Studio
# ==========================================

# 创建最终的顶级 Agent Platform 控制实例
job_hunting_platform_agent = agents.AgentPlatform(
    project_id=os.environ.get("GOOGLE_CLOUD_PROJECT", "my-first-project"),
    location="us-central1",  # 或者是亚洲/欧洲可用区
    display_name="Self_Learning_Career_Agent_Platform",
    main_playbook=orchestrator_playbook
)

if __name__ == "__main__":
    print("🎉 恭喜！多智能体协作防拷打求职系统 ADK 代码架构已成功在 Google Cloud 平台中声明完毕。现在可以点击部署或调用接口进行内嵌！")
