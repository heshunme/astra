from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml

from ..config import DEFAULT_SYSTEM_PROMPT, ResolvedRuntimeConfig
from ..models import ToolSpec
from .builtin_capabilities import load_builtin_tools


ExactCommandHandler = Callable[[str], bool]
PrefixCommandHandler = Callable[[str, str], bool]


@dataclass(slots=True)
class PromptFragment:
    key: str
    text: str
    source: str


@dataclass(slots=True)
class PromptInspectionFragment:
    key: str
    source: str
    text_length: int


@dataclass(slots=True)
class PromptInspection:
    assembled: str
    fragments: list[PromptInspectionFragment] = field(default_factory=list)


@dataclass(slots=True)
class SkillSpec:
    name: str
    summary: str
    when_to_use: str
    source: str
    source_label: str
    root_kind: str
    root_order: int
    shadowed_sources: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    file_aliases: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class SkillConflictInfo:
    name: str
    winner_source: str
    winner_source_label: str
    shadowed_sources: list[str] = field(default_factory=list)
    shadowed_source_labels: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RuntimeDiagnostics:
    warnings: list[str] = field(default_factory=list)
    loaded_prompts: list[str] = field(default_factory=list)
    loaded_skills: list[str] = field(default_factory=list)
    skill_conflicts: list[SkillConflictInfo] = field(default_factory=list)


@dataclass(slots=True)
class RuntimeSnapshot:
    tools: dict[str, ToolSpec]
    prompt_fragments: dict[str, PromptFragment]
    skills: dict[str, SkillSpec]
    skill_file_aliases: dict[str, Path]
    diagnostics: RuntimeDiagnostics


@dataclass(slots=True)
class CommandSpec:
    name: str
    usage: str
    summary: str
    handler: ExactCommandHandler


@dataclass(slots=True)
class PrefixCommandSpec:
    prefix: str
    usage: str
    summary: str
    handler: PrefixCommandHandler


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolSpec] = {}

    def register(self, tool: ToolSpec) -> None:
        self._tools[tool.name] = tool

    def resolve_enabled(self, enabled_names: list[str]) -> dict[str, ToolSpec]:
        unknown_tools = [name for name in enabled_names if name not in self._tools]
        if unknown_tools:
            unknown = ", ".join(sorted(unknown_tools))
            raise ValueError(f"Unknown tools in config: {unknown}")
        return {name: self._tools[name] for name in enabled_names}


class PromptRegistry:
    def __init__(self):
        self._fragments: dict[str, PromptFragment] = {}

    def register(self, fragment: PromptFragment) -> None:
        self._fragments[fragment.key] = fragment

    def get(self, key: str) -> PromptFragment | None:
        return self._fragments.get(key)

    def items(self) -> dict[str, PromptFragment]:
        return dict(self._fragments)


class SkillRegistry:
    def __init__(self):
        self._skills: dict[str, list[SkillSpec]] = {}

    def register(self, skill: SkillSpec) -> None:
        self._skills.setdefault(skill.name, []).append(skill)

    def get(self, name: str) -> SkillSpec | None:
        skills = self._skills.get(name)
        if not skills:
            return None
        return self._select_winner(skills)

    def items(self) -> dict[str, SkillSpec]:
        result: dict[str, SkillSpec] = {}
        for name, skills in self._skills.items():
            if not skills:
                continue
            result[name] = self._select_winner(skills)
        return result

    def finalize(self, diagnostics: RuntimeDiagnostics) -> dict[str, SkillSpec]:
        result: dict[str, SkillSpec] = {}
        diagnostics.loaded_skills = []
        diagnostics.skill_conflicts = []

        for name in sorted(self._skills):
            skills = self._skills[name]
            if not skills:
                continue
            winner = self._select_winner(skills)
            shadowed = [skill for skill in skills if skill.source != winner.source]
            winner.shadowed_sources = [skill.source for skill in shadowed]
            result[name] = winner
            diagnostics.loaded_skills.append(name)
            if not shadowed:
                continue
            diagnostics.skill_conflicts.append(
                SkillConflictInfo(
                    name=name,
                    winner_source=winner.source,
                    winner_source_label=winner.source_label,
                    shadowed_sources=[skill.source for skill in shadowed],
                    shadowed_source_labels=[skill.source_label for skill in shadowed],
                )
            )
            shadowed_labels = ", ".join(skill.source_label for skill in shadowed)
            diagnostics.warnings.append(
                f"Skill conflict for {name}: using {winner.source_label}; shadowed {shadowed_labels}."
            )

        return result

    def _select_winner(self, skills: list[SkillSpec]) -> SkillSpec:
        return max(skills, key=lambda skill: (self._root_priority(skill.root_kind), skill.root_order, skill.source))

    def _root_priority(self, root_kind: str) -> int:
        if root_kind == "project":
            return 2
        if root_kind == "extra":
            return 1
        return 0


class CommandRegistry:
    def __init__(self):
        self._commands: dict[str, CommandSpec] = {}
        self._prefixes: list[PrefixCommandSpec] = []

    def register(self, command: CommandSpec) -> None:
        self._commands[command.name] = command

    def register_prefix(self, prefix: PrefixCommandSpec) -> None:
        self._prefixes.append(prefix)

    def dispatch(self, line: str) -> bool:
        command_name, _, _rest = line.partition(" ")
        command = self._commands.get(command_name)
        if command is not None:
            return command.handler(line)
        for prefix in self._prefixes:
            if line.startswith(prefix.prefix):
                return prefix.handler(line, line[len(prefix.prefix) :])
        return False

    def help_lines(self) -> list[str]:
        lines = [command.usage for command in self._commands.values()]
        lines.extend(prefix.usage for prefix in self._prefixes)
        return lines


class CapabilityRuntime:
    def __init__(self, cwd: Path):
        self.cwd = cwd
        self._snapshot = RuntimeSnapshot(
            tools={},
            prompt_fragments={},
            skills={},
            skill_file_aliases={},
            diagnostics=RuntimeDiagnostics(),
        )

    def snapshot(self) -> RuntimeSnapshot:
        return self._snapshot

    def reload(self, runtime_config: ResolvedRuntimeConfig) -> RuntimeSnapshot:
        diagnostics = RuntimeDiagnostics()
        tool_registry = ToolRegistry()
        prompt_registry = PromptRegistry()
        skill_registry = SkillRegistry()

        for tool in load_builtin_tools().values():
            tool_registry.register(tool)
        enabled_tools = tool_registry.resolve_enabled(runtime_config.tools.enabled_tools)

        prompt_registry.register(
            PromptFragment(key="builtin:base", text=DEFAULT_SYSTEM_PROMPT, source="builtin")
        )
        prompt_registry.register(
            PromptFragment(key="config:system", text=runtime_config.system_prompt, source="runtime config")
        )

        for prompt_file in self._iter_prompt_files(runtime_config):
            key = f"prompt:{prompt_file.stem}"
            self._register_prompt_file(prompt_registry, diagnostics, key, prompt_file)

        for skill_dir in self._iter_skill_dirs(runtime_config):
            self._register_skill(skill_registry, diagnostics, skill_dir)

        resolved_skills = skill_registry.finalize(diagnostics)

        if resolved_skills and "read" not in enabled_tools:
            diagnostics.warnings.append("Skills are available but the read tool is disabled; skill details cannot be loaded on demand.")

        for ref in self._default_prompt_refs(runtime_config):
            normalized_ref = self.normalize_prompt_ref(ref)
            if prompt_registry.get(normalized_ref) is None:
                diagnostics.warnings.append(f"Prompt reference not found: {normalized_ref}")

        self._snapshot = RuntimeSnapshot(
            tools=enabled_tools,
            prompt_fragments=prompt_registry.items(),
            skills=resolved_skills,
            skill_file_aliases=self._collect_skill_file_aliases(resolved_skills),
            diagnostics=diagnostics,
        )
        return self._snapshot

    def has_skill(self, name: str) -> bool:
        return name in self._snapshot.skills

    def has_template(self, name: str) -> bool:
        return self.normalize_prompt_ref(f"template:{name}") in self._snapshot.prompt_fragments

    def list_skill_names(self) -> list[str]:
        return sorted(self._snapshot.skills)

    def get_skill(self, name: str) -> SkillSpec | None:
        return self._snapshot.skills.get(name)

    def list_template_names(self) -> list[str]:
        template_names = [key.split(":", 1)[1] for key in self._snapshot.prompt_fragments if key.startswith("prompt:")]
        return sorted(template_names)

    def list_prompt_keys(self) -> list[str]:
        return sorted(self._snapshot.prompt_fragments)

    def warnings(self) -> list[str]:
        return list(self._snapshot.diagnostics.warnings)

    def inspect_prompt(
        self,
        runtime_config: ResolvedRuntimeConfig,
        active_refs: list[str] | None = None,
    ) -> PromptInspection:
        ordered_refs = self._default_prompt_refs(runtime_config)
        ordered_refs.extend(active_refs or [])

        seen: set[str] = set()
        prompt_parts: list[str] = []
        fragments: list[PromptInspectionFragment] = []
        for ref in ordered_refs:
            normalized_ref = self.normalize_prompt_ref(ref)
            if normalized_ref in seen:
                continue
            seen.add(normalized_ref)
            fragment = self._snapshot.prompt_fragments.get(normalized_ref)
            if fragment is None:
                continue
            text = fragment.text.strip()
            if not text:
                continue
            prompt_parts.append(text)
            fragments.append(
                PromptInspectionFragment(
                    key=fragment.key,
                    source=fragment.source,
                    text_length=len(text),
                )
            )
        return PromptInspection(assembled="\n\n".join(prompt_parts), fragments=fragments)

    def assemble_system_prompt(self, runtime_config: ResolvedRuntimeConfig, active_refs: list[str] | None = None) -> str:
        return self.inspect_prompt(runtime_config, active_refs).assembled

    def normalize_prompt_ref(self, ref: str) -> str:
        normalized_ref = ref.strip()
        if not normalized_ref:
            return normalized_ref
        if normalized_ref.startswith("template:"):
            return f"prompt:{normalized_ref.split(':', 1)[1]}"
        if ":" not in normalized_ref:
            return f"prompt:{normalized_ref}"
        return normalized_ref

    def _default_prompt_refs(self, runtime_config: ResolvedRuntimeConfig) -> list[str]:
        return list(runtime_config.prompts.order)

    def _iter_prompt_files(self, runtime_config: ResolvedRuntimeConfig) -> list[Path]:
        prompt_files: list[Path] = []
        seen: set[Path] = set()
        for prompt_dir in self._prompt_dirs(runtime_config):
            if not prompt_dir.exists() or not prompt_dir.is_dir():
                continue
            for prompt_file in sorted(prompt_dir.glob("*.md")):
                resolved_file = prompt_file.resolve()
                if resolved_file in seen:
                    continue
                seen.add(resolved_file)
                prompt_files.append(resolved_file)
        return prompt_files

    def _iter_skill_dirs(self, runtime_config: ResolvedRuntimeConfig) -> list[SkillRootDir]:
        skill_dirs: list[SkillRootDir] = []
        seen: set[Path] = set()
        for root in self._skill_roots(runtime_config):
            if not root.path.exists() or not root.path.is_dir():
                continue
            for child in sorted(root.path.iterdir()):
                if not child.is_dir():
                    continue
                resolved_dir = child.resolve()
                if resolved_dir in seen:
                    continue
                seen.add(resolved_dir)
                skill_dirs.append(SkillRootDir(path=resolved_dir, source=root))
        return skill_dirs

    def _prompt_dirs(self, runtime_config: ResolvedRuntimeConfig) -> list[Path]:
        prompt_dirs = [Path.home() / ".astra-python" / "prompts", self.cwd / ".astra" / "prompts"]
        prompt_dirs.extend(self._resolve_extra_paths(runtime_config.capabilities.prompts.paths))
        return prompt_dirs

    def _skill_roots(self, runtime_config: ResolvedRuntimeConfig) -> list[SkillRootSource]:
        skill_dirs = [
            SkillRootSource(
                path=(Path.home() / ".astra-python" / "skills").resolve(),
                kind="global",
                order=0,
                label="global (~/.astra-python/skills)",
            )
        ]
        skill_dirs.extend(
            SkillRootSource(
                path=path,
                kind="extra",
                order=index,
                label=f"extra[{index + 1}] ({path})",
            )
            for index, path in enumerate(self._resolve_extra_paths(runtime_config.capabilities.skills.paths))
        )
        skill_dirs.append(
            SkillRootSource(
                path=(self.cwd / ".astra" / "skills").resolve(),
                kind="project",
                order=0,
                label="project (.astra/skills)",
            )
        )
        return skill_dirs

    def _resolve_extra_paths(self, raw_paths: list[str]) -> list[Path]:
        resolved_paths: list[Path] = []
        for raw_path in raw_paths:
            path = Path(raw_path)
            if not path.is_absolute():
                path = self.cwd / path
            resolved_paths.append(path.resolve())
        return resolved_paths

    def _register_prompt_file(
        self,
        prompt_registry: PromptRegistry,
        diagnostics: RuntimeDiagnostics,
        key: str,
        prompt_file: Path,
    ) -> None:
        try:
            text = prompt_file.read_text(encoding="utf-8")
        except Exception as exc:
            diagnostics.warnings.append(f"Failed to load prompt {prompt_file}: {exc}")
            return
        prompt_registry.register(PromptFragment(key=key, text=text, source=str(prompt_file)))
        diagnostics.loaded_prompts.append(key)

    def _register_skill(
        self,
        skill_registry: SkillRegistry,
        diagnostics: RuntimeDiagnostics,
        skill_dir: SkillRootDir,
    ) -> None:
        resolved_skill_dir = skill_dir.path.resolve()
        skill_file = resolved_skill_dir / "skill.yaml"
        if not skill_file.exists():
            return
        try:
            loaded = yaml.safe_load(skill_file.read_text(encoding="utf-8"))
        except Exception as exc:
            diagnostics.warnings.append(f"Failed to parse skill file {skill_file}: {exc}")
            return
        if not isinstance(loaded, dict):
            diagnostics.warnings.append(f"Skill file must contain a mapping: {skill_file}")
            return

        name = loaded.get("name")
        summary = loaded.get("summary")
        when_to_use = loaded.get("when_to_use")
        if not isinstance(name, str) or not name.strip():
            diagnostics.warnings.append(f"Skill file missing string name: {skill_file}")
            return
        if not isinstance(summary, str) or not summary.strip():
            diagnostics.warnings.append(f"Skill file missing string summary: {skill_file}")
            return
        if when_to_use is not None and (not isinstance(when_to_use, str) or not when_to_use.strip()):
            diagnostics.warnings.append(f"Skill file when_to_use must be a non-empty string: {skill_file}")
            return

        prompt_files = self._require_string_list(diagnostics, loaded.get("prompt_files"), skill_file, "prompt_files")
        template_files = self._require_string_list(diagnostics, loaded.get("template_files"), skill_file, "template_files")
        context_files = self._require_string_list(diagnostics, loaded.get("context_files"), skill_file, "context_files")
        if prompt_files is None or template_files is None or context_files is None:
            return

        ordered_files = prompt_files + template_files + context_files
        if not ordered_files:
            diagnostics.warnings.append(f"Skill has no text resources: {skill_file}")
            return

        loaded_files: list[str] = []
        file_aliases: dict[str, str] = {}
        for relative_path in ordered_files:
            resource_file = self._resolve_skill_resource(resolved_skill_dir, relative_path)
            if resource_file is None:
                diagnostics.warnings.append(
                    f"Skill resource escapes skill directory: {skill_file} ({self._format_skill_resource_reference(relative_path)})"
                )
                return
            if not resource_file.exists() or not resource_file.is_file():
                diagnostics.warnings.append(
                    f"Skill resource not found: {skill_file} ({self._format_skill_resource_reference(relative_path)})"
                )
                return
            alias = self._build_skill_resource_alias(name.strip(), resource_file.relative_to(resolved_skill_dir))
            loaded_files.append(alias)
            file_aliases[alias] = str(resource_file)

        skill_registry.register(
            SkillSpec(
                name=name,
                summary=summary.strip(),
                when_to_use=(when_to_use or "").strip(),
                source=str(skill_file),
                source_label=skill_dir.source.label,
                root_kind=skill_dir.source.kind,
                root_order=skill_dir.source.order,
                files=loaded_files,
                file_aliases=file_aliases,
            )
        )

    def _require_string_list(
        self,
        diagnostics: RuntimeDiagnostics,
        value: object,
        skill_file: Path,
        label: str,
    ) -> list[str] | None:
        if value is None:
            return []
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            diagnostics.warnings.append(f"Skill field must be a list of strings: {skill_file} ({label})")
            return None
        return [item for item in value]

    def _resolve_skill_resource(self, skill_dir: Path, relative_path: str) -> Path | None:
        resource_file = (skill_dir / relative_path).resolve()
        try:
            resource_file.relative_to(skill_dir)
        except ValueError:
            return None
        return resource_file

    def _format_skill_resource_reference(self, resource_path: str) -> str:
        if Path(resource_path).is_absolute():
            return "<absolute path>"
        return resource_path

    def _build_skill_resource_alias(self, skill_name: str, relative_path: Path) -> str:
        normalized = relative_path.as_posix().lstrip("/")
        return f"skill://{skill_name}/{normalized}"

    def _collect_skill_file_aliases(self, skills: dict[str, SkillSpec]) -> dict[str, Path]:
        aliases: dict[str, Path] = {}
        for skill in skills.values():
            for alias, target in skill.file_aliases.items():
                aliases[alias] = Path(target)
        return aliases


@dataclass(slots=True)
class SkillRootSource:
    path: Path
    kind: str
    order: int
    label: str


@dataclass(slots=True)
class SkillRootDir:
    path: Path
    source: SkillRootSource
