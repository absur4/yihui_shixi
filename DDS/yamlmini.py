"""零依赖的 YAML 子集读取器。

契约层 `DDS/adapter.py` 必须在**主解释器**里被成功导入（控制台进程可能没有
PyYAML，也可能没有 Fast DDS 原生绑定），因此不能依赖第三方库来读取条件矩阵。

本模块只支持 `DDS/config.yaml` 用到的子集：

* 缩进 2 空格的嵌套映射；
* `- key: value` 形式的映射列表，续行缩进比列表项更深；
* 标量：字符串（可加引号）、整数、浮点数、`true/false`、`null`；
* `#` 注释（行首或空白之后）。

不支持锚点、多行标量、流式集合等高级语法。矩阵文件变更后由
`DDS/tests/test_config_and_transport.py` 校验解析结果。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = ["load", "loads"]


class YamlSubsetError(ValueError):
    pass


def _strip_comment(line: str) -> str:
    out: list[str] = []
    quote: str | None = None
    for index, char in enumerate(line):
        if quote is not None:
            out.append(char)
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
            out.append(char)
            continue
        if char == "#" and (index == 0 or line[index - 1] in " \t"):
            break
        out.append(char)
    return "".join(out).rstrip()


def _split_key(content: str) -> tuple[str, str]:
    quote: str | None = None
    for index, char in enumerate(content):
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
            continue
        if char == ":" and (index + 1 == len(content) or content[index + 1] in " \t"):
            return content[:index].strip(), content[index + 1 :].strip()
    raise YamlSubsetError(f"Expected 'key: value' but found {content!r}")


def _scalar(token: str) -> Any:
    if token == "":
        return None
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    lowered = token.lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"null", "~"}:
        return None
    try:
        return int(token)
    except ValueError:
        pass
    try:
        return float(token)
    except ValueError:
        pass
    return token


def _parse_map(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    while index < len(lines):
        current_indent, content = lines[index]
        if current_indent < indent or content.startswith("- "):
            break
        if current_indent > indent:
            raise YamlSubsetError(f"Unexpected indentation near {content!r}")
        key, rest = _split_key(content)
        index += 1
        if rest:
            result[key] = _scalar(rest)
            continue
        if index < len(lines) and lines[index][0] > indent:
            result[key], index = _parse_block(lines, index, lines[index][0])
        else:
            result[key] = None
    return result, index


def _parse_sequence(
    lines: list[tuple[int, str]], index: int, indent: int
) -> tuple[list[Any], int]:
    items: list[Any] = []
    while index < len(lines):
        current_indent, content = lines[index]
        if current_indent != indent or not content.startswith("- "):
            break
        item_text = content[2:].strip()
        index += 1
        if item_text == "":
            if index < len(lines) and lines[index][0] > indent:
                value, index = _parse_block(lines, index, lines[index][0])
            else:
                value = None
            items.append(value)
            continue
        if item_text[0] in "\"'" or ":" not in item_text:
            items.append(_scalar(item_text))
            continue
        nested_indent = indent + 2
        pseudo: list[tuple[int, str]] = [(nested_indent, item_text)]
        while index < len(lines) and lines[index][0] > indent:
            pseudo.append(lines[index])
            index += 1
        value, consumed = _parse_block(pseudo, 0, nested_indent)
        if consumed != len(pseudo):
            raise YamlSubsetError(f"Could not parse list item near {item_text!r}")
        items.append(value)
    return items, index


def _parse_block(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[Any, int]:
    if lines[index][1].startswith("- "):
        return _parse_sequence(lines, index, indent)
    return _parse_map(lines, index, indent)


def loads(text: str) -> Any:
    lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        content = _strip_comment(raw)
        if not content.strip():
            continue
        if "\t" in content[: len(content) - len(content.lstrip())]:
            raise YamlSubsetError("Tabs are not supported for indentation")
        lines.append((len(content) - len(content.lstrip(" ")), content.strip()))
    if not lines:
        return {}
    value, index = _parse_block(lines, 0, lines[0][0])
    if index != len(lines):
        raise YamlSubsetError(f"Trailing content near {lines[index][1]!r}")
    return value


def load(path: Path) -> Any:
    return loads(Path(path).read_text(encoding="utf-8"))
