from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


ExactCommandHandler = Callable[[str], bool]
PrefixCommandHandler = Callable[[str, str], bool]


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
