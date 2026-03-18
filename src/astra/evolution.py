from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .models import AgentSnapshot, Message


GENERATED_BLOCK_START = "<!-- astra:evolution:start -->"
GENERATED_BLOCK_END = "<!-- astra:evolution:end -->"
_STOP_WORDS = {
    "a",
    "an",
    "and",
    "at",
    "for",
    "from",
    "how",
    "i",
    "in",
    "into",
    "is",
    "it",
    "of",
    "on",
    "or",
    "please",
    "review",
    "show",
    "that",
    "the",
    "this",
    "to",
    "use",
    "why",
}
_SKIP_PREFIXES = ("/",)


@dataclass(slots=True)
class EvolutionRequest:
    goal: str | None = None


@dataclass(slots=True)
class ExperienceRecord:
    user_request: str
    assistant_response: str
    tool_names: list[str] = field(default_factory=list)
    skill_name: str = ""
    skill_slug: str = ""
    summary: str = ""
    when_to_use: str = ""
    checklist_lines: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SkillMutationPlan:
    skill_name: str
    skill_slug: str
    action: str
    skill_dir: Path
    skill_file: Path
    checklist_file: Path
    summary: str
    when_to_use: str
    generated_block: str


@dataclass(slots=True)
class EvolutionOutcome:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skill_name: str | None = None
    written_files: list[str] = field(default_factory=list)
    experience: ExperienceRecord | None = None


class SkillEvolutionService:
    def __init__(self, cwd: Path):
        self.cwd = cwd

    def evolve(
        self,
        snapshot: AgentSnapshot,
        request: EvolutionRequest | None = None,
    ) -> EvolutionOutcome:
        outcome = EvolutionOutcome()
        experience = self._extract_experience(snapshot, request or EvolutionRequest(), outcome.warnings)
        if experience is None:
            outcome.skipped.append("no_reusable_experience")
            return outcome

        outcome.experience = experience
        outcome.skill_name = experience.skill_name
        plan = self._build_mutation_plan(experience)
        self._apply_plan(plan, outcome)
        return outcome

    def evolve_from_session(
        self,
        snapshot: AgentSnapshot,
        request: EvolutionRequest | None = None,
    ) -> EvolutionOutcome:
        return self.evolve(snapshot, request=request)

    def _extract_experience(
        self,
        snapshot: AgentSnapshot,
        request: EvolutionRequest,
        warnings: list[str],
    ) -> ExperienceRecord | None:
        messages = snapshot.conversation.messages
        turn_messages = self._latest_completed_turn(messages)
        if not turn_messages:
            warnings.append("No completed normal user turn was available for evolution.")
            return None

        user_message = turn_messages[0]
        assistant_messages = [message for message in turn_messages if message.role == "assistant"]
        tool_messages = [message for message in turn_messages if message.role == "tool_result"]
        if not assistant_messages:
            warnings.append("Latest turn has no assistant response to learn from.")
            return None

        user_request = user_message.content.strip()
        assistant_response = "\n\n".join(
            message.content.strip() for message in assistant_messages if message.content.strip()
        ).strip()
        tool_names = self._ordered_unique(
            [tool_call.name for message in assistant_messages for tool_call in message.tool_calls if tool_call.name]
            + [message.tool_name for message in tool_messages if message.tool_name]
        )
        if not tool_names and len(user_request.split()) < 4:
            warnings.append("Latest turn is too small to derive a reusable skill.")
            return None

        skill_slug = self._derive_skill_slug(user_request, tool_names)
        skill_name = skill_slug.replace("-", "_")
        summary = self._derive_summary(user_request, request.goal)
        when_to_use = self._derive_when_to_use(user_request, request.goal)
        checklist_lines = self._build_checklist_lines(user_request, assistant_response, tool_names, request.goal)
        return ExperienceRecord(
            user_request=user_request,
            assistant_response=assistant_response,
            tool_names=tool_names,
            skill_name=skill_name,
            skill_slug=skill_slug,
            summary=summary,
            when_to_use=when_to_use,
            checklist_lines=checklist_lines,
        )

    def _latest_completed_turn(self, messages: list[Message]) -> list[Message]:
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if message.role != "user":
                continue
            raw_user_input = str(message.metadata.get("raw_user_input", "")).strip()
            if raw_user_input.startswith(_SKIP_PREFIXES):
                continue
            turn = messages[index:]
            if any(item.role == "user" for item in turn[1:]):
                continue
            if not turn or turn[-1].role != "assistant":
                continue
            if not any(item.role == "assistant" for item in turn[1:]):
                continue
            return turn
        return []

    def _build_mutation_plan(self, experience: ExperienceRecord) -> SkillMutationPlan:
        skill_dir = self.cwd / ".astra" / "skills" / experience.skill_slug
        skill_file = skill_dir / "skill.yaml"
        checklist_file = skill_dir / "checklist.md"
        generated_block = self._render_generated_block(experience)
        action = "create" if not skill_file.exists() else "update"
        return SkillMutationPlan(
            skill_name=experience.skill_name,
            skill_slug=experience.skill_slug,
            action=action,
            skill_dir=skill_dir,
            skill_file=skill_file,
            checklist_file=checklist_file,
            summary=experience.summary,
            when_to_use=experience.when_to_use,
            generated_block=generated_block,
        )

    def _apply_plan(self, plan: SkillMutationPlan, outcome: EvolutionOutcome) -> None:
        existing_yaml: dict[str, object] | None = None
        if plan.skill_file.exists():
            try:
                loaded = yaml.safe_load(plan.skill_file.read_text(encoding="utf-8"))
            except Exception as exc:
                outcome.warnings.append(f"Failed to parse existing skill file {plan.skill_file}: {exc}")
                outcome.skipped.append("invalid_skill_yaml")
                return
            if loaded is None:
                loaded = {}
            if not isinstance(loaded, dict):
                outcome.warnings.append(f"Existing skill file must contain a mapping: {plan.skill_file}")
                outcome.skipped.append("invalid_skill_yaml")
                return
            existing_yaml = loaded

        plan.skill_dir.mkdir(parents=True, exist_ok=True)
        new_yaml = self._build_skill_yaml(plan, existing_yaml or {})
        new_checklist = self._build_checklist_text(plan)

        current_yaml_text = plan.skill_file.read_text(encoding="utf-8") if plan.skill_file.exists() else None
        current_checklist_text = plan.checklist_file.read_text(encoding="utf-8") if plan.checklist_file.exists() else None
        next_yaml_text = yaml.safe_dump(new_yaml, sort_keys=False, allow_unicode=False)

        if current_yaml_text == next_yaml_text and current_checklist_text == new_checklist:
            outcome.skipped.append("no_changes")
            return

        written_files: list[str] = []
        if current_yaml_text != next_yaml_text:
            plan.skill_file.write_text(next_yaml_text, encoding="utf-8")
            written_files.append(str(plan.skill_file))
        if current_checklist_text != new_checklist:
            plan.checklist_file.write_text(new_checklist, encoding="utf-8")
            written_files.append(str(plan.checklist_file))

        outcome.written_files.extend(written_files)
        if plan.action == "create" and existing_yaml is None:
            outcome.created.append(plan.skill_name)
        else:
            outcome.updated.append(plan.skill_name)

    def _build_skill_yaml(self, plan: SkillMutationPlan, existing_yaml: dict[str, object]) -> dict[str, object]:
        payload = dict(existing_yaml)
        payload["name"] = self._coerce_non_empty_string(existing_yaml.get("name")) or plan.skill_name
        payload["summary"] = plan.summary
        payload["when_to_use"] = plan.when_to_use
        payload["prompt_files"] = ["checklist.md"]
        payload.setdefault("template_files", [])
        payload.setdefault("context_files", [])
        return payload

    def _build_checklist_text(self, plan: SkillMutationPlan) -> str:
        if not plan.checklist_file.exists():
            title = f"# {plan.skill_name.replace('_', ' ').title()}\n\n"
            return title + plan.generated_block

        existing = plan.checklist_file.read_text(encoding="utf-8")
        if GENERATED_BLOCK_START in existing and GENERATED_BLOCK_END in existing:
            start = existing.index(GENERATED_BLOCK_START)
            end = existing.index(GENERATED_BLOCK_END) + len(GENERATED_BLOCK_END)
            updated = existing[:start] + plan.generated_block + existing[end:]
            return updated.rstrip() + "\n"

        separator = "" if existing.endswith("\n\n") else "\n\n"
        return existing.rstrip() + separator + plan.generated_block

    def _render_generated_block(self, experience: ExperienceRecord) -> str:
        lines = [
            GENERATED_BLOCK_START,
            "## Generated guidance",
            "",
            "Derived from the latest reusable session turn.",
            "",
        ]
        lines.extend(experience.checklist_lines)
        lines.extend(["", GENERATED_BLOCK_END, ""])
        return "\n".join(lines)

    def _build_checklist_lines(
        self,
        user_request: str,
        assistant_response: str,
        tool_names: list[str],
        goal: str | None,
    ) -> list[str]:
        lines = [
            "### Trigger",
            f"- Use when handling requests similar to: {user_request}",
        ]
        if goal:
            lines.append(f"- Keep the evolution goal in mind: {goal}")

        lines.extend(["", "### Repeatable steps"])
        steps = self._derive_steps(tool_names)
        if not steps:
            steps = [
                "Clarify the concrete task and success condition from the user request.",
                "Inspect the most relevant local context before answering.",
                "Respond with concrete findings and next actions.",
            ]
        for index, step in enumerate(steps, start=1):
            lines.append(f"{index}. {step}")

        if assistant_response:
            excerpt = self._normalize_sentence(assistant_response)
            if excerpt:
                lines.extend(["", "### Response pattern", f"- Favor an answer shaped like: {excerpt}"])
        return lines

    def _derive_steps(self, tool_names: list[str]) -> list[str]:
        steps: list[str] = []
        if any(name in {"find", "grep"} for name in tool_names):
            steps.append("Search the workspace to locate the most relevant files before making claims.")
        if "read" in tool_names:
            steps.append("Read the relevant files directly and verify details from source.")
        if "bash" in tool_names:
            steps.append("Run focused shell commands to confirm behavior or gather evidence.")
        if any(name in {"write", "edit"} for name in tool_names):
            steps.append("Keep edits minimal and aligned with the verified local context.")
        if tool_names:
            joined_tools = ", ".join(tool_names)
            steps.append(f"Summarize the outcome and mention the evidence gathered via: {joined_tools}.")
        return steps

    def _derive_skill_slug(self, user_request: str, tool_names: list[str]) -> str:
        lowered = user_request.lower()
        if any(token in lowered for token in ("bug", "debug", "failure", "failing", "error")):
            return "debug-issue-workflow"
        if "review" in lowered:
            return "code-review-workflow"
        if "test" in lowered:
            return "test-investigation-workflow"

        tokens = [token for token in re.findall(r"[a-z0-9]+", lowered) if token not in _STOP_WORDS]
        if len(tokens) >= 3:
            return "-".join(tokens[:3]) + "-workflow"
        if len(tokens) == 2:
            return "-".join(tokens) + "-workflow"
        if len(tokens) == 1:
            return tokens[0] + "-workflow"
        if tool_names:
            return "-".join(tool_names[:2]) + "-workflow"
        return "general-analysis-workflow"

    def _derive_summary(self, user_request: str, goal: str | None) -> str:
        basis = goal.strip() if goal else user_request.strip()
        basis = basis.rstrip(".")
        if len(basis) > 72:
            basis = basis[:69].rstrip() + "..."
        return f"Reusable workflow for {basis.lower()}"

    def _derive_when_to_use(self, user_request: str, goal: str | None) -> str:
        basis = goal.strip() if goal else user_request.strip()
        basis = self._normalize_sentence(basis)
        return f"Use when the task matches this pattern: {basis}"

    def _normalize_sentence(self, text: str) -> str:
        compact = " ".join(text.split())
        if len(compact) > 160:
            compact = compact[:157].rstrip() + "..."
        return compact

    def _ordered_unique(self, items: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for item in items:
            if item in seen:
                continue
            seen.add(item)
            result.append(item)
        return result

    def _coerce_non_empty_string(self, value: object) -> str | None:
        if not isinstance(value, str):
            return None
        stripped = value.strip()
        return stripped or None
