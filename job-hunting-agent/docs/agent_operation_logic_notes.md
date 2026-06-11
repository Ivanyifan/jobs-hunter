
让 agent 主要靠视觉大模型看截图来申请岗位，通常不是最优路线。
更稳的设计应该是：

网页结构 / DOM / Accessibility Tree / 表单 schema 作为主输入
截图 + 视觉模型作为辅助验证和兜底

也就是说，不是“视觉没用”，而是 截图不应该是主控制面。

结论

对于 job application agent，推荐架构是：

Structure-first, Vision-assisted

中文说就是：

先读网页结构，让模型理解页面上有哪些字段、按钮、label、required inputs、error messages；只有在结构信息不完整、页面是复杂自定义组件、或者需要确认视觉状态时，才用截图。

原因很简单：

DOM / Accessibility Tree = 可操作、可定位、可验证
Screenshot = 只能看见，不能稳定操作

Playwright 官方推荐的 locator 本身就是围绕页面语义来的，比如 getByRole()、getByLabel()、getByText()、getByPlaceholder() 等；这些比基于坐标点击截图稳定得多。 WebDriver 这类浏览器自动化标准本质上也是为了让程序能 introspect 和 control 浏览器，而不是只看像素。

为什么纯截图路线很脆弱？

如果你让 VLM 看截图，然后输出：

点击右下角的 Apply 按钮

或者：

在第二个输入框填邮箱

这个在 demo 里可能能跑，但真实 ATS 页面会非常容易坏。

主要问题

第一，坐标不稳定。

不同屏幕尺寸、浏览器 zoom、弹窗、cookie banner、sticky header、移动版布局，都会让同一个按钮位置变化。

第二，截图不包含完整网页状态。

很多重要信息截图里看不到：

input name
aria-label
required attribute
hidden field
select options
disabled state
validation error
iframe 结构
上传控件 accept 类型

但这些东西在 DOM 或 accessibility tree 里是能读到的。

第三，截图很难处理长表单。

申请页面经常是多 step：

Contact Info
Resume Upload
Work Authorization
EEO Questions
Review
Submit

靠截图只能一屏一屏看，容易漏字段。结构化读取可以直接扫描整个页面或当前 step 的 form schema。

第四，视觉模型容易误判 label 和 field 的绑定关系。

截图里看到：

Phone
[________]

但真实 DOM 里可能有多个 phone 字段：

mobile_phone
home_phone
emergency_contact_phone

只看截图，agent 不一定知道该填哪个。

第五，无法稳定验证动作是否成功。

视觉上按钮变了、页面跳了，不等于表单真的通过验证。结构化 agent 可以检查：

URL changed?
network request succeeded?
DOM 出现 success message?
required field error 消失了?
application status changed?
但视觉不是没用

视觉模型适合做这几类事情：

1. 页面结构缺失时兜底
2. 自定义组件识别，比如奇怪的 dropdown、calendar、upload zone
3. 判断弹窗、遮罩、cookie banner、modal 是否挡住页面
4. OCR 一些 DOM 读不到的文字
5. 对最终 review 页面做人工式 sanity check
6. Debug trace：给开发者看 agent 当时看见了什么

所以正确姿势不是：

VLM-only web agent

而是：

DOM / Accessibility 主导
VLM 作为 fallback sensor

Playwright 现在还有 ARIA snapshots，可以把页面 accessibility tree 表示成 YAML，这种结构化页面快照比截图更适合喂给 LLM 理解页面语义。 Chrome DevTools Protocol 也提供 Accessibility domain，可以获取 accessibility tree 相关节点信息；但官方也提醒启用 accessibility domain 可能影响性能，所以适合按需使用。

我建议你的 agent 用这个网页申请算法

可以叫：

Structure-First Application Agent

或者：

SFA: Structure-first Form Agent

核心流程：

Open apply_url
↓
Detect ATS / page type
↓
Extract page structure
↓
Build form schema
↓
Map form fields to candidate memory
↓
LLM decides answers, not coordinates
↓
Browser executor fills fields using stable locators
↓
Validate each step
↓
Escalate ambiguous / risky questions
↓
Submit only after final audit
↓
Record trace + outcome
1. 页面观察层：不要直接给 LLM 截图，先生成 Page State

你不应该把完整 DOM 原样丢给 LLM，也不应该只给截图。
应该由程序先整理一个结构化 PageState。

例如：

{
  "url": "https://company.greenhouse.io/jobs/123",
  "page_title": "Software Engineer Intern Application",
  "detected_ats": "greenhouse",
  "step": "contact_info",

  "forms": [
    {
      "form_id": "application_form",
      "fields": [
        {
          "field_id": "first_name",
          "label": "First Name",
          "type": "text",
          "required": true,
          "value": "",
          "locator_candidates": [
            "getByLabel('First Name')",
            "input[name='first_name']"
          ]
        },
        {
          "field_id": "email",
          "label": "Email",
          "type": "email",
          "required": true,
          "value": "",
          "locator_candidates": [
            "getByLabel('Email')",
            "input[type='email']"
          ]
        },
        {
          "field_id": "resume",
          "label": "Resume/CV",
          "type": "file",
          "required": true,
          "accept": [".pdf", ".doc", ".docx"]
        }
      ],

      "buttons": [
        {
          "text": "Submit Application",
          "role": "button",
          "enabled": false,
          "locator": "getByRole('button', { name: 'Submit Application' })"
        }
      ],

      "validation_errors": []
    }
  ]
}

LLM 看到的应该是这个，而不是一张图片。

2. LLM 的职责：做语义决策，不做低级点击

LLM 不应该输出：

{
  "action": "click",
  "x": 812,
  "y": 642
}

它应该输出：

{
  "action": "fill_form",
  "fields": [
    {
      "field_id": "first_name",
      "answer_source": "candidate_profile.first_name",
      "value": "Alex"
    },
    {
      "field_id": "email",
      "answer_source": "candidate_profile.email",
      "value": "alex@example.com"
    },
    {
      "field_id": "work_authorization",
      "answer_source": "candidate_profile.work_authorization",
      "value": "Yes"
    }
  ],
  "requires_user_confirmation": [
    {
      "field_id": "sponsorship",
      "reason": "This is legally sensitive and should not be guessed."
    }
  ]
}

也就是说：

LLM = planner / semantic mapper
Playwright = executor
MongoDB = memory
Arize = trace/eval
3. 执行层：用 locator，不用坐标

执行时优先级应该是：

1. getByLabel(label)
2. getByRole(role, name)
3. getByPlaceholder(placeholder)
4. name / id / autocomplete attribute
5. CSS selector
6. XPath
7. 视觉坐标 fallback

Playwright 的 locator 有 auto-waiting 和 retry 能力，能在元素变化时更稳定地找到页面元素。 官方 locator 文档也明确支持按 role、text、label、placeholder、alt text、title、test id 等方式定位元素，这比点击截图坐标更适合真实网页表单。

执行例子：

await page.get_by_label("First Name").fill(candidate.first_name)
await page.get_by_label("Email").fill(candidate.email)
await page.get_by_label("Phone").fill(candidate.phone)
await page.set_input_files("input[type='file']", resume_pdf_path)
await page.get_by_role("button", name="Submit Application").click()

而不是：

click(431, 622)
type("Alex")
4. ATS 页面应该做 adapter，而不是每次都靠 LLM 猜

你会遇到很多外部 ATS：

Greenhouse
Lever
Workday
Ashby
iCIMS
SmartRecruiters
BambooHR
Company custom forms

更好的设计是：

ATS detector
→ ATS adapter
→ generic form agent fallback

例如：

def detect_ats(url, dom):
    if "greenhouse.io" in url or "boards.greenhouse.io" in url:
        return "greenhouse"
    if "lever.co" in url:
        return "lever"
    if "myworkdayjobs.com" in url:
        return "workday"
    return "generic"

然后：

Greenhouse adapter:
- known resume upload structure
- known first_name / last_name / email fields
- known submit button style

Lever adapter:
- known contact fields
- known resume attach field

Generic adapter:
- structure extraction + LLM field mapping

这比每次让视觉模型“看图操作”可靠很多。

5. 表单字段匹配算法

申请表真正难的不是点击，而是理解字段含义。

比如这些都可能表示同一个东西：

Email
Email Address
Contact Email
Preferred Email
E-mail

你的 agent 应该做 field normalization。

Field canonicalization
{
  "raw_label": "Are you legally authorized to work in the United States?",
  "canonical_field": "work_authorization_us",
  "risk_level": "high",
  "answer_source": "candidate_profile.work_authorization_us",
  "auto_fill_allowed": true
}

再比如：

{
  "raw_label": "Will you now or in the future require sponsorship?",
  "canonical_field": "requires_sponsorship",
  "risk_level": "high",
  "answer_source": "candidate_profile.requires_sponsorship",
  "auto_fill_allowed": false,
  "requires_confirmation": true
}

字段可以分风险等级：

字段类型	示例	是否可自动填
low risk	name, email, phone, LinkedIn, GitHub	可以
medium risk	salary expectation, start date, location preference	建议确认
high risk	visa, sponsorship, disability, veteran status, legal authorization	不要猜，确认或用用户预设
irreversible	final submit, certification checkbox	必须 gate
6. 对每一页做 preflight，而不是边看边填

我建议每个 step 都先做一次 preflight：

1. 扫描当前页面所有 required fields
2. 判断哪些能从 MongoDB memory 自动回答
3. 判断哪些需要用户确认
4. 判断是否有风险字段
5. 判断是否有文件上传
6. 判断是否有验证码 / 登录 / 反自动化阻断
7. 生成 fill plan

输出：

{
  "page_status": "fillable",
  "required_fields_count": 8,
  "auto_fillable_count": 6,
  "needs_user_count": 2,
  "blocking_issues": [
    {
      "type": "missing_answer",
      "field": "requires_sponsorship",
      "reason": "High-risk legal/work authorization field"
    }
  ],
  "fill_plan": [
    {
      "field_id": "first_name",
      "value_source": "candidate.first_name",
      "confidence": 0.99
    },
    {
      "field_id": "resume_upload",
      "value_source": "resume_versions.rv_789.file_uri",
      "confidence": 1.0
    }
  ]
}

这样 agent 不会乱填。

7. 关键：每一步都要验证

结构化网页 agent 必须是一个 loop：

observe → plan → act → verify → continue

不是：

看一眼 → 填完 → 提交
每次 fill 后验证

比如填 email：

await page.get_by_label("Email").fill(candidate.email)

actual = await page.get_by_label("Email").input_value()
assert actual == candidate.email

上传简历后：

检查页面是否出现：
- uploaded file name
- remove file button
- resume.pdf
- hidden attachment id

点击 next 后：

检查：
- URL 是否变化
- step heading 是否变化
- required error 是否出现
- network request 是否失败

最终提交前：

review_page_audit

确认：

{
  "resume_version_id": "rv_789",
  "company": "Acme",
  "role": "Software Engineer Intern",
  "email": "alex@example.com",
  "required_fields_completed": true,
  "high_risk_fields_confirmed": true,
  "submit_allowed": true
}
8. 什么时候用截图 / VLM？

你可以把截图放在这些节点：

A. DOM 解析失败

比如页面大量 shadow DOM、自定义组件、canvas、奇怪 iframe。

DOM confidence < 0.6 → request screenshot reasoning
B. 页面遮挡
button exists in DOM but click failed
→ screenshot detects cookie banner / modal / sticky overlay
C. Dropdown / date picker

有些 custom select DOM 很复杂。
可以用视觉帮助判断哪个选项被打开了，但最终还是尽量用 keyboard / locator 操作。

D. 最终 review sanity check

提交前截一张 review 页面，让 VLM 判断：

是否明显填错？
是否上传了正确简历？
是否有红色 error？
是否还停留在未完成页面？
E. Debug / observability

把截图挂到 Arize trace 里，方便 demo：

这里是 agent 看到的页面
这里是 DOM schema
这里是它选择的 action
这里是 action 结果

但不要让视觉模型成为主执行器。

9. 推荐的多层感知架构
Browser Page
   ↓
Layer 1: URL / ATS detection
   ↓
Layer 2: DOM extraction
   ↓
Layer 3: Accessibility tree extraction
   ↓
Layer 4: Form schema extraction
   ↓
Layer 5: Screenshot / VLM fallback
   ↓
Structured PageState
   ↓
LLM Planner
   ↓
Deterministic Executor
   ↓
Verifier
   ↓
Trace + Memory

可以记成：

DOM first
AX tree second
Vision third
Human escalation last

AX tree 就是 accessibility tree。它往往比 raw DOM 更适合给 LLM，因为它更接近用户实际理解的页面语义：

button: Submit Application
textbox: Email
checkbox: I certify that the information is accurate
combobox: Work authorization
10. Agent action schema 设计

不要让 LLM 输出自由文本动作。
让它输出严格 JSON。

{
  "action_type": "fill_field",
  "target": {
    "field_id": "email",
    "canonical_field": "candidate_email",
    "locator_strategy": "label",
    "locator": "Email"
  },
  "value": "alex@example.com",
  "confidence": 0.99,
  "risk_level": "low",
  "reason": "Email field maps directly to candidate profile email"
}

上传简历：

{
  "action_type": "upload_file",
  "target": {
    "field_id": "resume_upload",
    "canonical_field": "resume_file"
  },
  "file_uri": "s3://resumes/rv_789.pdf",
  "resume_version_id": "rv_789",
  "confidence": 1.0,
  "risk_level": "medium"
}

提交：

{
  "action_type": "submit_application",
  "target": {
    "button_text": "Submit Application"
  },
  "preconditions": {
    "all_required_fields_complete": true,
    "high_risk_fields_confirmed": true,
    "resume_audit_verdict": "approve",
    "form_audit_verdict": "approve"
  },
  "confidence": 0.95,
  "risk_level": "irreversible"
}
11. 申请 agent 的完整状态机

我会这样设计：

DISCOVER_JOB
↓
OPEN_APPLY_PAGE
↓
DETECT_ATS
↓
EXTRACT_FORM_SCHEMA
↓
PRECHECK_REQUIRED_FIELDS
↓
MAP_ANSWERS
↓
FILL_PAGE
↓
VERIFY_PAGE
↓
NEXT_STEP_OR_REVIEW
↓
FINAL_AUDIT
↓
SUBMIT_OR_ESCALATE
↓
POST_SUBMIT_CAPTURE
↓
GMAIL_OUTCOME_TRACKING

每个状态都应该有：

{
  "state": "FILL_PAGE",
  "input_page_state": "...",
  "planned_actions": [],
  "executed_actions": [],
  "verification_result": {},
  "errors": [],
  "next_state": "VERIFY_PAGE"
}

这非常适合放进 Arize/Phoenix trace。

12. MongoDB 里怎么存网页结构

你可以新增一个 collection：

application_form_snapshots
{
  "_id": "form_snap_001",
  "application_id": "app_123",
  "job_id": "job_456",
  "apply_url": "https://...",

  "detected_ats": "greenhouse",
  "page_url": "https://...",
  "step_name": "contact_info",

  "page_state": {
    "fields": [
      {
        "field_id": "f_001",
        "raw_label": "First Name",
        "canonical_field": "first_name",
        "input_type": "text",
        "required": true,
        "visible": true,
        "enabled": true,
        "locator_candidates": [
          {
            "strategy": "label",
            "value": "First Name",
            "confidence": 0.96
          },
          {
            "strategy": "css",
            "value": "input[name='first_name']",
            "confidence": 0.82
          }
        ]
      }
    ],

    "buttons": [
      {
        "text": "Submit Application",
        "role": "button",
        "enabled": false,
        "locator": "getByRole('button', { name: 'Submit Application' })"
      }
    ],

    "validation_errors": []
  },

  "screenshot_uri": "s3://screenshots/app_123_step_1.png",
  "created_at": "2026-06-10T12:00:00Z"
}
13. Arize trace 里应该怎么体现这个设计

你可以加这些 spans：

application_agent_trace
├── open_apply_url
├── detect_ats
├── extract_page_structure
├── build_form_schema
├── map_fields_to_candidate_profile
├── fill_form_step
├── verify_form_step
├── visual_fallback_if_needed
├── final_application_audit
└── submit_decision

其中最关键的是：

extract_page_structure
{
  "span_name": "extract_page_structure",
  "span_kind": "TOOL",
  "input": {
    "url": "https://..."
  },
  "output": {
    "detected_ats": "greenhouse",
    "fields_count": 12,
    "required_fields_count": 8,
    "buttons_count": 3,
    "used_dom": true,
    "used_accessibility_tree": true,
    "used_screenshot": false
  }
}
map_fields_to_candidate_profile
{
  "span_name": "map_fields_to_candidate_profile",
  "span_kind": "LLM",
  "input": {
    "fields": [
      "First Name",
      "Email",
      "Are you legally authorized to work in the United States?"
    ],
    "candidate_profile_keys": [
      "first_name",
      "email",
      "work_authorization_us"
    ]
  },
  "output": {
    "mapped_fields": 3,
    "auto_fillable": 2,
    "requires_confirmation": 1
  }
}
visual_fallback_if_needed
{
  "span_name": "visual_fallback_if_needed",
  "span_kind": "LLM",
  "input": {
    "reason": "DOM click failed because button was covered by modal"
  },
  "output": {
    "visual_observation": "Cookie banner blocking bottom of page",
    "recommended_action": "close_cookie_banner"
  }
}

这能很好展示：

我们不是 blind screenshot clicking。我们有 structure extraction、field mapping、verification、fallback vision 和 traceability。

14. 容错逻辑

真实申请页面会很乱。你的 agent 要有 fail-safe。

情况 A：字段识别置信度低
field_mapping_confidence < 0.75
→ 不自动填
→ ask user / mark pending
情况 B：高风险字段
visa / sponsorship / disability / veteran / criminal history / legal certification
→ 不猜
→ 只用用户预设答案
→ 没有预设就 pause
情况 C：验证码或反自动化
captcha / human verification / suspicious traffic
→ 不绕过
→ escalate to user

这点很重要。不要把项目讲成“绕过 ATS 或反 bot”。应该讲成：

The agent respects human checkpoints and escalates when manual confirmation is required.

情况 D：最终提交
Submit 是 irreversible action
→ 必须 final audit
→ 必须记录 resume_version_id
→ 必须确认高风险字段