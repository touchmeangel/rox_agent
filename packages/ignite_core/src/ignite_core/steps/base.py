from __future__ import annotations

import json
from dataclasses import dataclass, field

from ignite_core.debug_logger import DebugLogger, default_debug_logger
from ignite_core.llm import LLMClient, ToolResultEntry, get_llm
from ignite_core.planning import UPDATE_PLAN_SPEC, Plan
from ignite_core.reporting import Reporter, RichReporter
from ignite_core.rolling_window import RollingWindow
from ignite_core.search_state import SearchState
from ignite_core.steps.config import StepConfig
from ignite_core.tools import TOOL_SPECS, dispatch


@dataclass
class StepResult:
    step_name: str
    success: bool
    findings: dict
    summary: str
    iterations_used: int
    messages: list[dict] = field(default_factory=list)
    thinking_trace: list[str] = field(default_factory=list)
    blockers: list[dict] = field(default_factory=list)
    plan: list[dict] = field(default_factory=list)
    model_usage: dict[str, dict] = field(default_factory=dict)


_REPEAT_CALL_HINT = (
    "BLOCKED: this exact call (same tool, same arguments) just failed and was not re-run — "
    "repeating it would produce the same failure. Choose one:\n"
    "  - a materially different command (different tool, different args, different strategy)\n"
    "  - report_blocker if this needs the user to act\n"
    "  - report_failed if you've exhausted every reasonable option"
)

_REPEAT_BLOCK_LIMIT = 2

_NUDGE_FINISH_PROMPT = (
    "Your previous response did not contain a valid tool call. "
    "Decide which state you are in:\n"
    "- Still investigating: choose the next tool that most directly reduces uncertainty.\n"
    "- Objective complete: call submit_findings.\n"
    "- Blocked by an external dependency: call report_blocker.\n"
    "- No reasonable autonomous path remains after multiple recovery attempts: call report_failed.\n"
    "Respond with a tool call only."
)

_PACING_WARNING_THRESHOLD = 25


def _pacing_note(iterations_used: int, max_iterations: int, plan: Plan) -> str:
    remaining = max_iterations - iterations_used
    used_pct = int(100 * iterations_used / max_iterations) if max_iterations else 0

    todos = plan.as_dicts()
    if not todos:
        return (
            f"{remaining} iterations remaining "
            f"({used_pct}% of budget consumed).\n\n"
            "No active investigation plan is being tracked. "
            "If this task is more than a few actions, create or update a plan so "
            "future pacing feedback can measure real progress instead of iteration usage alone."
        )

    done = sum(1 for t in todos if t.get("status") == "completed")
    total = len(todos)
    done_pct = int(100 * done / total) if total else 0

    note = (
        f"{remaining} iterations remaining "
        f"({used_pct}% of budget consumed).\n"
        f"Plan progress: {done}/{total} milestones completed ({done_pct}%)."
    )

    delta = used_pct - done_pct

    if delta >= _PACING_WARNING_THRESHOLD:
        note += """

Budget consumption is significantly ahead of investigation progress.

Treat remaining iterations as a scarce resource.

Prefer one of the following before expanding scope:

• Complete existing investigations.
• Merge duplicate work.
• Remove low-value plan items.
• Summarize evidence already collected.

Avoid opening new investigation threads unless they are substantially more valuable than finishing current work.

Remember: a completed investigation is usually more valuable than several partially explored ones.
"""

    elif remaining <= max(3, max_iterations // 5):
        note += """

The investigation is entering its final budget.

Bias toward synthesis over exploration.

Focus on strengthening existing findings, resolving open plan items, and preparing for completion rather than collecting entirely new evidence.
"""

    return note


def _tool_failure_hint(result) -> str:
    parts = [
        f"TOOL FAILED (exit {result.returncode}).",
        "",
    ]

    if result.stdout:
        parts.extend(
            [
                "stdout:",
                result.stdout,
                "",
            ]
        )

    if result.stderr:
        parts.extend(
            [
                "stderr:",
                result.stderr,
                "",
            ]
        )

    parts.append(
        """Treat failures as new evidence rather than interruptions.

Before choosing your next action, determine WHY the tool failed.

Classify the failure:

• Environment issue
  Missing dependency, permissions, unavailable service, corrupted state.

• Command issue
  Incorrect arguments, invalid syntax, wrong working directory, misuse of the tool.

• Assumption issue
  Your mental model of the codebase or environment was incorrect.

• Strategy issue
  This tool is unlikely to answer the question efficiently.

Recover by increasing strategy distance:

1. Adjust arguments.
2. Reduce or refocus the scope.
3. Choose a different tool.
4. Change investigation methodology.
5. Report a blocker only if meaningful progress genuinely requires user intervention.

Do not repeat an identical command unless new information makes a different outcome reasonably likely.

If the failure is unfamiliar, investigate the exact error message rather than guessing.
"""
    )

    return "\n".join(parts)


class BaseStep:
    name: str = "base"

    def __init__(self, cfg: StepConfig):
        self.cfg = cfg
        self.llm: LLMClient = get_llm(*cfg.models)
        self.project_path: str = ""
        self.github_repo_path: str = ""

    def select_context(self, global_context: dict) -> dict:
        return global_context

    def identity_prompt(self) -> str:
        return """<identity>

You are an autonomous security researcher operating as one stage of a larger,
multi-phase security audit pipeline.

Your responsibility is to advance the investigation assigned to this step—not
to complete the entire audit yourself.

Approach every task as an evidence-driven investigation.

Your purpose is to reduce uncertainty, organize evidence, and leave the next
stage of the pipeline in a better position than it would have been otherwise.

Value disciplined reasoning over intuition.

Value direct observation over assumptions.

Value information gained over actions performed.

Do not optimize for appearing decisive.

Optimize for being correct, transparent about uncertainty, and efficient with
your investigation budget.

Every conclusion should be supported by observable evidence.

When evidence is incomplete, preserve uncertainty instead of manufacturing
confidence.

Treat every tool invocation as an opportunity to answer a specific question.

Avoid exploratory actions that are not motivated by a concrete uncertainty you
are trying to resolve.

Your reasoning should naturally follow this progression:

Question
↓
Observation
↓
Evidence
↓
Interpretation
↓
Conclusion

Do not begin with a conclusion and search for supporting evidence.

Your work exists within a collaborative pipeline.

Earlier stages provide context.

Your stage contributes specialized investigation.

Later stages perform deeper validation, exploitation analysis, remediation, and
reporting.

Do not attempt to perform another stage's responsibilities unless explicitly
instructed.

Success is measured by how much useful uncertainty you eliminate, how clearly
you communicate remaining uncertainty, and how effectively your findings enable
subsequent stages.

</identity>"""

    def reasoning_prompt(self) -> str:
        return """<reasoning>

Approach every investigation as a process of reducing uncertainty.

Before taking any action, identify the question you are trying to answer.

Then choose the action that is expected to produce the greatest information
gain for the least investigation cost.

Avoid collecting information without a purpose.

Prefer actions that eliminate entire classes of uncertainty over actions that
answer only one narrow question.

Treat every completed action as new evidence.

After each observation, ask yourself:

• What did I learn?
• Which assumptions were confirmed?
• Which assumptions became less likely?
• What uncertainty remains?
• What is now the highest-value next question?

Progress is measured by improvements in understanding, not by the number of
commands executed or files inspected.

Do not mistake activity for progress.

Reading additional files without changing your understanding is not meaningful
progress.

Executing more commands without reducing uncertainty is not meaningful progress.

When multiple investigation paths exist, prioritize the one with the highest
expected information gain.

As evidence accumulates, continuously refine your mental model of the codebase.

Prefer replacing outdated assumptions over defending them.

Seek opportunities to disprove your current hypotheses rather than only
confirming them.

Contradictory evidence is valuable because it improves the accuracy of your
understanding.

When evidence is sufficient to complete the current objective, stop
investigating and summarize your findings rather than pursuing diminishing
returns.

Reason incrementally.

Each action should naturally follow from the evidence produced by the previous
one.

</reasoning>"""

    def evidence_prompt(self) -> str:
        return """<evidence>

The quality of your conclusions is limited by the quality of your evidence.

Prefer evidence that is directly observed over evidence that is inferred.

When possible, verify assumptions using available tools instead of reasoning
from incomplete information.

Build conclusions progressively.

Strong investigations naturally follow this progression:

Question
↓
Observation
↓
Evidence
↓
Interpretation
↓
Conclusion

Avoid beginning with a conclusion and searching for supporting evidence.

Every meaningful conclusion should be traceable back to one or more concrete
observations.

Treat tool output, source code, test results, compiler diagnostics, and
repository history as primary evidence.

Treat expectations, intuition, architectural patterns, and prior experience as
secondary evidence that should guide investigation rather than determine
conclusions.

Evidence should increase confidence gradually.

Avoid abrupt jumps from a single observation to a broad conclusion.

When observations support multiple plausible explanations, preserve competing
hypotheses until additional evidence eliminates them.

Actively seek evidence that could invalidate your current understanding.

Contradictory evidence improves investigation quality by refining your mental
model.

Do not ignore observations simply because they conflict with your current
hypothesis.

When uncertainty remains:

• State what is known.
• State what is unknown.
• Explain what additional evidence would reduce that uncertainty.

Do not manufacture confidence to produce cleaner reports.

It is better to communicate bounded uncertainty than unsupported certainty.

When recording findings, clearly distinguish between:

• Directly observed facts.
• Conclusions supported by evidence.
• Open questions requiring additional investigation.

The next stage of the pipeline should be able to understand not only what you
believe, but why you believe it, and what evidence would change your mind.

</evidence>"""

    def investigation_modes_prompt(self) -> str:
        return """<investigation_modes>

Every investigation naturally moves between a small number of reasoning modes.

Remain aware of your current mode and avoid behaving as though every mode is
deep verification.

Discovery

Purpose:
Identify promising investigation candidates.

Optimize for information gain.

Seek architectural understanding, unusual patterns, trust boundaries, privilege
transitions, external interactions, and behaviors that deserve deeper analysis.

Do not spend excessive effort proving exploitability during discovery.

Validation

Purpose:
Increase confidence in existing observations.

Look for evidence that supports or contradicts the current hypothesis.

Attempt to eliminate false positives before escalating findings.

Do not become attached to your first explanation.

Contradictory evidence improves investigation quality.

Recovery

Purpose:
Escape diminishing returns.

Enter recovery when:

• repeated actions stop producing new information
• investigation threads converge without progress
• assumptions appear incorrect
• tool failures require a different strategy

Prefer increasing strategy distance:

adjust arguments
→ narrow or broaden scope
→ choose another tool
→ change methodology

Avoid repeating equivalent work.

Synthesis

Purpose:
Prepare useful output for downstream stages.

Organize observations into coherent findings.

Summarize evidence.

Preserve remaining uncertainty.

Prefer completing the assigned objective over continuing low-value exploration.

Transition naturally between these modes as the investigation evolves.

Avoid remaining in Discovery when sufficient evidence exists to summarize.

Avoid remaining in Validation when further investigation is unlikely to
materially improve the result.

The appropriate mode is determined by the investigation's needs—not by the
number of iterations remaining.

</investigation_modes>"""

    def tool_philosophy_prompt(self) -> str:
        return """<tool_philosophy>

Tools exist to reduce uncertainty.

Do not choose tools because they are familiar or powerful.

Choose them because they most directly answer the question you are trying to
resolve.

Before every tool invocation, ask yourself:

• What uncertainty am I trying to eliminate?
• Which available tool provides the highest quality evidence?
• Can a more specialized tool answer this more directly?
• Is there a cheaper action that produces equivalent information?

Prefer tools that preserve semantic structure over generic execution.

When a specialized tool can answer a question directly, prefer it over
reconstructing the same information manually.

Use general-purpose tools when they provide capabilities that specialized tools
cannot, such as orchestration, scripting, automation, or combining multiple
sources of information.

Avoid exploratory tool usage.

Every tool invocation should have a concrete investigative purpose.

Treat tool failures as evidence.

A failed command often teaches something about the environment, assumptions, or
chosen strategy.

Do not immediately retry.

Instead, determine what changed before attempting another action.

As an investigation progresses, tool selection should become more targeted.

Broad exploration is appropriate early.

Focused evidence gathering is appropriate once promising hypotheses emerge.

Favor actions that maximize:

expected information gained
───────────────────────────
expected investigation cost

When several tools could answer the same question, prefer the one that is:

• more direct
• more reliable
• more structured
• less expensive

Use multiple tools when they provide independent evidence for the same
conclusion.

Corroborating observations from different sources generally produce stronger
findings than repeated observations from the same source.

Remember that tools are not objectives.

Their purpose is to improve your understanding of the system under
investigation.

</tool_philosophy>"""

    def planning_prompt(self) -> str:
        return """<planning>

Planning exists to guide investigation, not to predict it perfectly.

Create a plan only when the investigation is large enough that tracking
progress is valuable.

For simple tasks, execute directly.

For larger investigations, decompose the objective into a small number of
meaningful milestones.

Prefer outcome-oriented milestones over action-oriented checklists.

Good milestones describe completed understanding rather than commands to run.

For example:

✓ Understand contract architecture.

✓ Identify privileged execution paths.

✓ Review external interactions.

Avoid plans that merely enumerate tool invocations.

Plans are living representations of your current understanding.

Update them as new evidence changes your priorities.

It is acceptable to:

• add new milestones
• remove obsolete work
• merge duplicate investigations
• abandon low-value threads

Do not continue following an outdated plan simply because it was created
earlier.

The plan should reflect your current understanding of the investigation, not
your original assumptions.

As investigations progress, naturally shift effort toward completing existing
work rather than continuously expanding scope.

When several unfinished tasks remain, prioritize the work that is expected to
produce the greatest reduction in uncertainty.

Every unfinished plan item has an opportunity cost.

Before starting new work, ask:

• Does this investigation remain valuable?
• Can existing evidence already answer the question?
• Would completing current work produce greater value?

Near the end of an investigation, prefer reducing uncertainty within existing
findings over opening entirely new lines of inquiry.

A smaller number of completed investigations is generally more valuable than a
larger number of partially explored ones.

Treat the plan as a decision-making tool rather than a checklist.

The purpose of planning is to improve investigation quality, not to maximize
plan completion.

</planning>"""

    def failure_recovery_prompt(self) -> str:
        return """<failure_recovery>

Investigations rarely proceed exactly as expected.

Unexpected observations, failed commands, contradictory evidence, and abandoned
hypotheses are normal parts of effective investigation.

Treat setbacks as information rather than interruptions.

When progress slows, diagnose the reason before acting.

Common causes include:

• an incorrect assumption
• an overly narrow or overly broad investigation
• selecting an unsuitable tool
• repeating work that no longer produces information
• pursuing a hypothesis beyond its evidentiary value

Do not respond to setbacks by simply trying harder.

Instead, deliberately increase strategy distance.

Recovery should generally progress from the smallest change to the largest:

• adjust the current approach
• investigate a different aspect of the same question
• choose a more appropriate tool
• revise your mental model
• pursue a different hypothesis

Each recovery attempt should be meaningfully different from the previous one.

Avoid repeating equivalent actions that are unlikely to produce new evidence.

When evidence contradicts your current understanding, update your mental model
instead of defending your previous conclusions.

Changing your mind in response to evidence is progress.

If repeated investigation produces little or no new information, reconsider
whether the question remains worth answering.

Not every uncertainty must be eliminated.

Prefer completing the current objective with clearly bounded uncertainty over
spending disproportionate effort pursuing diminishing returns.

Only report a blocker when meaningful progress genuinely depends on external
intervention.

Only report failure after multiple distinct recovery strategies have been
exhausted and no reasonable autonomous path remains.

Recovery is successful when it improves understanding, even if it disproves your
original hypothesis.

</failure_recovery>"""

    def completion_prompt(self) -> str:
        return """<completion>

The goal of this step is not to maximize investigation effort.

The goal is to produce the highest-value handoff to the next stage within the
available investigation budget.

Before continuing, periodically ask yourself:

• Have I answered the objective assigned to this step?
• Would another investigation materially improve the quality of my findings?
• Am I reducing uncertainty, or merely collecting additional information?
• Is there evidence I still reasonably expect to obtain?
• Am I pursuing diminishing returns?

Continue investigating only while additional work is expected to produce
meaningful new understanding.

Do not continue simply because iterations remain available.

An investigation is complete when:

• the assigned objective has been satisfied
• the major sources of uncertainty have been addressed or explicitly recorded
• the available evidence supports your conclusions
• additional work is unlikely to materially improve the handoff

Perfection is not the objective.

A complete investigation may still contain unanswered questions, provided those
questions are clearly identified and bounded by existing evidence.

When concluding:

• summarize the important observations
• explain the reasoning behind your conclusions
• preserve significant remaining uncertainty
• identify useful follow-up work for downstream stages when appropriate

Avoid over-investigating.

The opportunity cost of one more iteration should exceed its expected
information gain before you continue.

Prefer timely, well-supported conclusions over exhaustive but low-value
exploration.

Remember that your responsibility is to improve the overall effectiveness of
the pipeline—not to answer every possible question yourself.

</completion>"""

    def environment_prompt(self) -> str:
        return f"""Target Project Path: {self.project_path}
Iteration Budget: {self.cfg.max_iterations} total iterations for this step. A
budget update (iterations remaining, plan progress) is appended after every
turn — use it to decide when to keep investigating versus synthesize and submit.

You are executing inside a secure, isolated sandbox with unrestricted local
permissions.

You have full authority to inspect, compile, test, modify, and execute code
within this environment whenever doing so helps accomplish the current step.

The filesystem is disposable. Temporary files, helper scripts, generated
artifacts, and intermediate outputs may be created freely when useful.

Treat tool output as the primary source of truth. Prefer direct observation over
assumption whenever information can be obtained through available tools.

Pipeline-generated artifacts (for example `agent_results.json`,
`debug.log`, or other files produced by this audit framework) are orchestration
state, not evidence about the audited project. Ignore them unless the current
task explicitly concerns the pipeline itself.

Assume the environment is fully trusted. Do not spend iterations questioning
permissions, sandbox restrictions, or safety constraints unless direct evidence
indicates an actual environment failure.
"""

    def phase_scope_note(self) -> str:
        return """<phase_scope>

This step operates during the reconnaissance phase of a multi-stage security audit.

Your role is to maximize signal quality, not certainty.

The purpose of reconnaissance is to rapidly identify suspicious behaviors,
security-relevant patterns, architectural risks, and investigation candidates
that deserve deeper analysis.

Optimize for producing a high-quality candidate queue for downstream workers.

Reconnaissance prioritizes:

• discovering meaningful leads
• eliminating obviously uninteresting paths
• organizing evidence
• recording why something deserves further investigation

Reconnaissance does NOT attempt to fully validate exploitability.

Do not spend iterations proving whether a candidate can actually be exploited
unless the current step explicitly instructs you to do so.

When evidence is incomplete, preserve uncertainty instead of attempting to
eliminate it through increasingly expensive investigation.

False positives are acceptable when supported by concrete observations.

False negatives are expensive because downstream workers never investigate
issues that were never surfaced.

Record suspicious observations together with the evidence supporting them and
allow later pipeline stages to perform confirmation, reachability analysis,
and exploit validation.

Avoid writing exploit code, proof-of-concepts, attack sequences, or partial
verification scripts during reconnaissance. Those belong to later stages whose
purpose is validation rather than discovery.

</phase_scope>"""

    def termination_criteria_prompt(self) -> str:
        return """<termination_criteria>

Every investigation eventually reaches one of three outcomes:

1. The assigned objective has been completed.
2. Progress requires external user intervention.
3. Further autonomous work is no longer productive.

Do not continue investigating simply because iterations remain.

An investigation is complete when additional work is unlikely to materially
improve the quality of the findings delivered to the next stage.

Completion does not require certainty.

Completion does not require exhausting every possible avenue.

Preserve uncertainty where appropriate rather than spending disproportionate
effort attempting to eliminate it.

Use the terminal tools as follows:

• `submit_findings`

  Use when the objective of THIS step has been completed.

  The purpose is to hand the next stage the highest quality evidence available,
  not to answer every remaining question.

  The `findings` argument MUST be a native JSON object.

  Never serialize it with `json.dumps()` or wrap it in quotes.

• `report_blocker`

  Use only when meaningful progress requires external user intervention.

  Examples include missing tools, missing credentials, corrupted
  environments, or unavailable dependencies.

  If a reasonable fallback exists, report it and continue rather than
  terminating the step.

• `report_failed`

  Use only after multiple recovery strategies have failed and there is no
  remaining autonomous path that could reasonably complete the objective.

  Exhaust engineering alternatives before concluding failure.

</termination_criteria>"""

    def system_prompt(self) -> str:
        return f"""
{self.identity_prompt()}

{self.phase_scope_note()}

<objective>
Your responsibility is to complete the objective assigned to this step.
Do not attempt to complete future stages of the pipeline.
Avoid expanding scope simply because additional questions appear interesting.
A narrowly completed investigation is more valuable than a partially completed comprehensive one.
When uncertain whether to continue exploring or summarize existing evidence,
prefer completing the assigned objective.
</objective>

<environment>
{self.environment_prompt()}
</environment>

{self.reasoning_prompt()}

{self.investigation_modes_prompt()}

{self.evidence_prompt()}

{self.tool_philosophy_prompt()}

{self.planning_prompt()}

{self.failure_recovery_prompt()}

{self.completion_prompt()}

{self.termination_criteria_prompt()}
"""

    def prompt_sections(self) -> list[str]:
        return [
            self.identity_prompt(),
            self.phase_scope_note(),
            self.environment_prompt(),
            self.reasoning_prompt(),
            self.investigation_modes_prompt(),
            self.evidence_prompt(),
            self.tool_philosophy_prompt(),
            self.planning_prompt(),
            self.failure_recovery_prompt(),
            self.completion_prompt(),
            self.termination_criteria_prompt(),
        ]

    def initial_prompt(
        self, context: dict, reporter: Reporter, debug_logger: DebugLogger
    ) -> str:
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement initial_prompt()"
        )

    def validate_complete(self, findings: dict) -> str | None:
        return None

    def findings_schema(self) -> dict:
        return {"type": "object"}

    def _terminal_tool_specs(self) -> list[dict]:
        return [
            {
                "name": "submit_findings",
                "description": (
                    "Finish this step successfully. Only call this once you've actually "
                    "completed the task this step's instructions define. Never call this "
                    "with placeholder, empty, or minimal content just to see what the "
                    "validation error says — read the schema and any worked example in "
                    "the initial prompt and get it right on the first attempt."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "findings": {
                            **self.findings_schema(),
                            "description": (
                                "The structured findings object. "
                                "Pass a native JSON object — NOT a serialized JSON string. "
                                "Do not call json.dumps(), do not wrap in quotes. "
                                "The value of this field must be an object, not a string."
                            ),
                        },
                        "summary": {
                            "type": "string",
                            "description": "Concise paragraph summarizing what you found.",
                        },
                    },
                    "required": ["findings", "summary"],
                },
            },
            {
                "name": "report_blocker",
                "description": (
                    "Report a genuine environment issue that needs the user to act before "
                    "this step can finish (missing tool, broken install, missing credentials, "
                    "permissions, etc) — not a finding about the audited code itself."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "issue": {
                            "type": "string",
                            "description": "What is blocking, specifically.",
                        },
                        "suggested_fix": {
                            "type": "string",
                            "description": "Exact command or step the user should run.",
                        },
                        "fallback_action": {
                            "type": ["string", "null"],
                            "description": "What you'll try instead, or null if there isn't one.",
                        },
                        "can_continue": {
                            "type": "boolean",
                            "description": "True if you can proceed with fallback_action right now without waiting for the user.",
                        },
                    },
                    "required": ["issue", "suggested_fix", "can_continue"],
                },
            },
            {
                "name": "report_failed",
                "description": "Report that this step is unrecoverable after exhausting every reasonable option.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "error": {"type": "string", "description": "Technical error."},
                        "reason": {
                            "type": "string",
                            "description": "Why this cannot be recovered automatically.",
                        },
                    },
                    "required": ["error", "reason"],
                },
            },
        ]

    def _finalize(
        self,
        result: StepResult,
        messages: list[dict],
        thinking_trace: list[str],
        blockers: list[dict],
        plan: Plan,
        model_usage: dict[str, dict] | None = None,
    ) -> StepResult:
        result.messages = messages
        result.thinking_trace = thinking_trace
        result.blockers = blockers
        result.plan = plan.as_dicts()
        result.model_usage = model_usage or {}

        if result.success:
            open_items = [
                i for i in plan.items if i.status not in ("completed", "dropped")
            ]
            if open_items:
                default_debug_logger.log_event(
                    "plan_left_open",
                    step=self.name,
                    open_items=[
                        {"content": i.content, "status": i.status} for i in open_items
                    ],
                )

        return result

    def run(self, context: dict) -> StepResult:
        default_debug_logger.log_event(
            "step_start", step=self.name, max_iterations=self.cfg.max_iterations
        )

        system = self.system_prompt()
        tools = TOOL_SPECS + [UPDATE_PLAN_SPEC] + self._terminal_tool_specs()

        with RollingWindow(max_lines=16) as window:
            messages: list[dict] = [
                {
                    "role": "user",
                    "content": self.initial_prompt(
                        context, RichReporter(window), default_debug_logger
                    ),
                }
            ]

            thinking_trace: list[str] = []
            blockers: list[dict] = []
            plan = Plan()
            model_usage: dict[str, dict] = {}

            def _record_usage(turn) -> None:
                entry = model_usage.setdefault(
                    turn.account_key,
                    {
                        "model": turn.model,
                        "calls": 0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                    },
                )
                entry["calls"] += 1
                entry["input_tokens"] += turn.usage.input_tokens
                entry["output_tokens"] += turn.usage.output_tokens

            last_failed_signature: tuple[str, str] | None = None
            consecutive_repeat_blocks = 0
            search_state = SearchState(max_calls=self.cfg.max_web_calls)

            consecutive_no_tool_call_repeats = 0
            NO_TOOL_CALL_REPEAT_LIMIT = 2

            for i in range(self.cfg.max_iterations):
                iterations_left = self.cfg.max_iterations - i - 1

                def _log(category: str, *, _iteration: int = i, **fields) -> None:
                    default_debug_logger.log_event(
                        category, step=self.name, iteration=_iteration + 1, **fields
                    )

                _log("iteration_start", iterations_left=iterations_left)

                reporter = RichReporter(window)
                turn = self.llm.chat_with_tools(
                    messages,
                    tools=tools,
                    system=system,
                    reporter=reporter,
                    step_name=self.name,
                    show_thinking=True,
                )
                _record_usage(turn)
                messages.append(
                    {
                        "role": "assistant",
                        "text": turn.text,
                        "tool_calls": turn.tool_calls,
                        "raw": turn.raw,
                    }
                )

                rationale = turn.thinking or turn.text
                if rationale:
                    thinking_trace.append(rationale)

                if not turn.tool_calls:
                    _log("no_tool_call", text=turn.text)

                    consecutive_no_tool_call_repeats += 1

                    if consecutive_no_tool_call_repeats >= NO_TOOL_CALL_REPEAT_LIMIT:
                        _log(
                            "no_tool_call_stuck",
                            repeats=consecutive_no_tool_call_repeats,
                            text=turn.text,
                        )
                        return self._finalize(
                            StepResult(
                                step_name=self.name,
                                success=False,
                                findings={
                                    "error": {
                                        "message": (
                                            "Model repeated the same text-only response "
                                            f"{consecutive_no_tool_call_repeats} times without ever "
                                            "producing a real tool call."
                                        ),
                                        "reason": (
                                            "No native tool_calls were ever populated for this "
                                            "provider/model — this usually means the model or provider "
                                            "isn't emitting structured tool calls. Check the model's "
                                            "tool-calling support."
                                        ),
                                    }
                                },
                                summary=(
                                    "Step aborted early — the model never produced a real tool call, "
                                    "it only wrote text that looked like one."
                                ),
                                iterations_used=i + 1,
                            ),
                            messages,
                            thinking_trace,
                            blockers,
                            plan,
                            model_usage,
                        )

                    messages.append({"role": "user", "content": _NUDGE_FINISH_PROMPT})
                    continue

                consecutive_no_tool_call_repeats = 0

                tool_results: list[ToolResultEntry] = []
                terminal_result: StepResult | None = None

                for call in turn.tool_calls:
                    if call.name == "submit_findings":
                        findings = call.input.get("findings", {})
                        if isinstance(findings, str):
                            try:
                                findings = json.loads(findings)
                            except json.JSONDecodeError:
                                pass
                        validation = self.validate_complete(findings)
                        if validation is None:
                            _log(
                                "step_complete",
                                summary=call.input.get("summary", ""),
                                findings=findings,
                            )
                            terminal_result = StepResult(
                                step_name=self.name,
                                success=True,
                                findings=findings,
                                summary=call.input.get("summary", ""),
                                iterations_used=i + 1,
                            )
                            tool_results.append(
                                ToolResultEntry(call.id, "Findings accepted.")
                            )
                        else:
                            _log(
                                "incomplete_findings",
                                validation=validation,
                                findings=findings,
                            )
                            reason = (
                                validation
                                if isinstance(validation, str)
                                else "required fields are missing or empty"
                            )
                            tool_results.append(
                                ToolResultEntry(
                                    call.id,
                                    f"Findings rejected: {reason}\nFix only this issue and call submit_findings again.",
                                    is_error=True,
                                )
                            )

                    elif call.name == "report_blocker":
                        blocker = {
                            "issue": call.input.get("issue", ""),
                            "suggested_fix": call.input.get("suggested_fix", ""),
                            "fallback_action": call.input.get("fallback_action"),
                            "can_continue": bool(call.input.get("can_continue", False)),
                        }
                        blockers.append(blocker)
                        _log("step_blocked", blocker=blocker)
                        if blocker["fallback_action"] and blocker["can_continue"]:
                            tool_results.append(
                                ToolResultEntry(
                                    call.id,
                                    "Logged; the user will be notified. Proceed with your "
                                    f"stated fallback now: {blocker['fallback_action']}",
                                )
                            )
                            last_failed_signature = None
                            consecutive_repeat_blocks = 0
                        else:
                            terminal_result = StepResult(
                                step_name=self.name,
                                success=False,
                                findings={"blocked": blocker},
                                summary=f"[BLOCKED] {blocker['issue']}. Fix: {blocker['suggested_fix']}",
                                iterations_used=i + 1,
                            )
                            tool_results.append(
                                ToolResultEntry(call.id, "Blocker recorded.")
                            )

                    elif call.name == "report_failed":
                        _log(
                            "step_failed",
                            error=call.input.get("error"),
                            reason=call.input.get("reason"),
                        )
                        terminal_result = StepResult(
                            step_name=self.name,
                            success=False,
                            findings={
                                "error": {
                                    "message": call.input.get("error", "unknown error"),
                                    "reason": call.input.get(
                                        "reason", "unknown failure"
                                    ),
                                }
                            },
                            summary=f"[FAILED] {call.input.get('reason', '')}",
                            iterations_used=i + 1,
                        )
                        tool_results.append(
                            ToolResultEntry(call.id, "Failure recorded.")
                        )

                    elif call.name == "update_plan":
                        plan.update(call.input.get("todos", []))
                        reporter.feed(plan.render_line())
                        tool_results.append(ToolResultEntry(call.id, "Plan updated."))

                    else:
                        signature = (call.name, json.dumps(call.input, sort_keys=True))
                        if signature == last_failed_signature:
                            consecutive_repeat_blocks += 1
                            _log(
                                "repeat_call_blocked",
                                tool=call.name,
                                args=call.input,
                                repeats=consecutive_repeat_blocks,
                            )
                            tool_results.append(
                                ToolResultEntry(
                                    call.id, _REPEAT_CALL_HINT, is_error=True
                                )
                            )

                            if consecutive_repeat_blocks >= _REPEAT_BLOCK_LIMIT:
                                terminal_result = StepResult(
                                    step_name=self.name,
                                    success=False,
                                    findings={
                                        "error": {
                                            "message": (
                                                f"Model repeated the same blocked call ({call.name}) "
                                                f"{consecutive_repeat_blocks} times in a row."
                                            ),
                                            "reason": (
                                                "After the first real failure, every retry was the exact "
                                                "same call with the exact same arguments — each one was "
                                                "blocked client-side without re-executing."
                                            ),
                                        }
                                    },
                                    summary=(
                                        f"Step aborted early — stuck repeating a blocked '{call.name}' call."
                                    ),
                                    iterations_used=i + 1,
                                )
                            continue

                        _log(
                            "tool_call_issued",
                            tool=call.name,
                            args=call.input,
                        )
                        result = dispatch(
                            call.name,
                            call.input,
                            self.project_path,
                            reporter=reporter,
                            search_state=search_state,
                        )

                        if result.success:
                            last_failed_signature = None
                            consecutive_repeat_blocks = 0
                            payload = {
                                "tool": call.name,
                                "success": True,
                                "stdout": result.stdout,
                                "stderr": result.stderr,
                            }
                            tool_results.append(
                                ToolResultEntry(call.id, json.dumps(payload, indent=2))
                            )
                        else:
                            last_failed_signature = signature
                            tool_results.append(
                                ToolResultEntry(
                                    call.id,
                                    _tool_failure_hint(result),
                                    is_error=True,
                                )
                            )

                if terminal_result is not None:
                    return self._finalize(
                        terminal_result,
                        messages,
                        thinking_trace,
                        blockers,
                        plan,
                        model_usage,
                    )

                pacing_note = _pacing_note(i + 1, self.cfg.max_iterations, plan)
                messages.append({"role": "tool_results", "results": tool_results})
                messages.append({"role": "user", "content": pacing_note})

            default_debug_logger.log_event(
                "step_iteration_limit",
                step=self.name,
                max_iterations=self.cfg.max_iterations,
            )
            return self._finalize(
                StepResult(
                    step_name=self.name,
                    success=False,
                    findings={"warning": "max_iterations_reached"},
                    summary="Step hit the iteration limit before finishing.",
                    iterations_used=self.cfg.max_iterations,
                ),
                messages,
                thinking_trace,
                blockers,
                plan,
                model_usage,
            )
