# Job Hunter Agent: Project Story

## Inspiration

The inspiration for Job Hunter Agent came from a simple frustration: applying for jobs is not only repetitive, it is also easy to do badly at scale. A strong candidate can still lose opportunities if every application uses the same generic resume, if an ATS form is filled incorrectly, or if previous application outcomes are never used as feedback.

I wanted to build an agent that treats job hunting as a learning system, not as a blind form-filling script. The central idea is that each application should become a memory. If a previous resume rewrite worked for a similar backend role, the agent should learn from it. If a rewrite added unsupported skills and failed, the agent should remember that too.

The project is built around one principle:

> A job application agent should be evidence-based, structure-first, and human-gated at risky moments.

That means the agent should not simply look at screenshots and click around. It should read the page structure, understand the form fields, map them to verified candidate data, and stop before sensitive or irreversible actions.

## What I Learned

The biggest lesson was that automation for job applications is not mostly about clicking buttons. The hard part is deciding what is safe, truthful, and reusable.

I learned that resume matching should be measured per application, not globally. For a specific job, the useful comparison is:

$$
\text{Lift}_{job} = \text{Match}(V_1, JD_{job}) - \text{Match}(V_0, JD_{job})
$$

where:

- \(V_0\) is the original resume.
- \(V_1\) is the tailored resume for that specific job.
- \(JD_{job}\) is the job description for that specific company and role.

This matters because a resume that improves fit for one backend engineering role may not improve fit for a data science role. The score only makes sense when it is tied to one concrete application.

I also learned that visual AI is useful, but it should not be the main control layer. Screenshots are good for fallback reasoning, detecting blocked pages, and debugging. But DOM, accessibility trees, labels, required fields, validation errors, and stable locators are much more reliable for real application forms.

The final operating model became:

$$
\text{Agent Reliability} = f(\text{DOM}, \text{Accessibility}, \text{Verification}, \text{Human Gates})
$$

not:

$$
\text{Agent Reliability} = f(\text{Screenshot Coordinates})
$$

## How I Built It

I built the system as a collection of services that each own a clear part of the workflow.

The Streamlit frontend is the control cockpit. It shows candidate data, application records, resume versions, per-job resume-to-JD fit, external apply prechecks, and audit results.

MongoDB acts as the source of truth. It stores applications, events, artifacts, resume versions, form discovery results, and outcomes. Each application becomes a durable memory rather than a temporary browser session.

Elastic/SOMA handles retrieval. SOMA stands for Stack-Outcome Matching Algorithm. Its purpose is to retrieve similar historical application episodes, rank them by technical stack similarity, outcome quality, faithfulness, and resume evidence compatibility, then extract reusable rewrite patterns.

The simplified ranking formula is:

$$
\text{case\_score}
= 0.45S_{stack}
+ 0.20S_{task}
+ 0.15A_{resume}
+ 0.10O
+ 0.10F
- 0.30H
$$

where:

- \(S_{stack}\) is stack similarity.
- \(S_{task}\) is task similarity.
- \(A_{resume}\) is whether the current resume has evidence to reuse the pattern.
- \(O\) is historical outcome score.
- \(F\) is faithfulness score.
- \(H\) is hallucination risk.

The resume matching layer scores \(V_0\) and \(V_1\) against the current JD and stores the result on that application record. This lets each job card show its own fit:

$$
\text{V1 Lift} = \text{JD Fit}_{V1} - \text{JD Fit}_{V0}
$$

The Playwright service handles browser automation, but it now follows a structure-first design. It builds a `PageState` from the live page:

- URL
- detected ATS
- form fields
- labels
- required status
- locator candidates
- buttons
- validation errors
- risk levels

Then it builds a preflight plan:

- which fields can be safely autofilled
- which fields need user confirmation
- whether a resume upload is available
- whether final submit is visible
- whether a captcha or human checkpoint blocks progress

Only after this structure is understood does the agent act. The loop is:

$$
\text{Observe} \rightarrow \text{Plan} \rightarrow \text{Act} \rightarrow \text{Verify} \rightarrow \text{Stop or Continue}
$$

Arize/Phoenix is used for observability and evaluation. The goal is to make the agent explainable: what did it retrieve, what did it rewrite, what did it block, and why?

Gmail/Email integration provides outcome signals such as interview, recruiter reply, online assessment, rejection, or no response. Those outcomes can flow back into the memory system so future decisions improve.

Cloud Run is the deployment target. Each tool server can run as a separate service:

- `jobs-mongo`
- `jobs-elastic`
- `jobs-arize`
- `jobs-email`
- `jobs-playwright`
- `jobs-frontend`

This made the project closer to a real multi-service agent architecture rather than a single local demo script.

## Challenges

The first challenge was making the automation reliable on real ATS pages. Workday, BrassRing, Greenhouse, Lever, and other systems all behave differently. A screenshot-only agent can easily click the wrong place or close the wrong modal. The solution was to move toward DOM and accessibility-first extraction, with vision only as a fallback.

The second challenge was safety. Job applications contain sensitive fields: work authorization, sponsorship, disability, veteran status, legal certifications, salary expectations, and final submission controls. The agent must not guess these fields. It needs explicit user data or human confirmation.

The third challenge was keeping resume rewrites faithful. It is easy for an LLM to make a resume sound better by adding unsupported claims. The system therefore scores faithfulness and hallucination risk, and blocks or flags risky outputs.

The fourth challenge was making the UI understandable. A global score is misleading. The important score is per job:

$$
\text{Application Fit}_{i} = \text{Match}(V_{1,i}, JD_i)
$$

and the improvement must compare the tailored resume to the original resume for that same job:

$$
\Delta_i = \text{Match}(V_{1,i}, JD_i) - \text{Match}(V_{0,i}, JD_i)
$$

That is why the application board now shows resume-to-JD fit by application instead of only showing a fixed demo metric.

## What Makes It Different

This project is not just an auto-apply bot. It is a memory-backed application assistant.

It learns from historical outcomes, checks whether rewrite patterns are actually supported by resume evidence, audits generated resumes, reads structured form state before acting, and stops at risky moments.

The goal is not to remove the human from the process. The goal is to remove repetitive work while preserving judgment, truthfulness, and control.

In short:

$$
\text{Good Automation} = \text{Speed} + \text{Evidence} + \text{Safety} + \text{Feedback}
$$

Job Hunter Agent is my attempt to build that kind of automation for the job search process.
