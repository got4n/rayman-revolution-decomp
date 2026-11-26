from __future__ import annotations

import re

FUNCTION_HEAD = re.compile(r"\b([A-Za-z_]\w*)\s*\([^;{}]*\)\s*$", re.S)
CONTROL_HEADS = {"for", "if", "switch", "while", "__attribute__", "__declspec"}


def mask_comments_strings(text: str) -> str:
    chars = list(text)
    index = 0
    state = "code"
    line_start = True
    while index < len(chars):
        ch = chars[index]
        nxt = chars[index + 1] if index + 1 < len(chars) else ""
        if state == "code":
            if line_start and ch in " \t":
                index += 1
                continue
            if line_start and ch == "#":
                state = "preprocessor"
            elif ch == "/" and nxt == "*":
                state = "block"
            elif ch == "/" and nxt == "/":
                state = "line"
            elif ch == '"':
                state = "string"
                chars[index] = " "
                line_start = False
                index += 1
                continue
            elif ch == "'":
                state = "char"
                chars[index] = " "
                line_start = False
                index += 1
                continue
            else:
                line_start = ch == "\n"
                index += 1
                continue
        if state == "block" and ch == "*" and nxt == "/":
            chars[index] = chars[index + 1] = " "
            index += 2
            state = "code"
            continue
        if state in ("string", "char") and ch == "\\" and nxt:
            chars[index] = chars[index + 1] = " "
            index += 2
            continue
        if state == "string" and ch == '"' or state == "char" and ch == "'":
            state = "code"
        if ch == "\n":
            line_start = True
            if state == "line":
                state = "code"
            elif state == "preprocessor":
                line_start_offset = text.rfind("\n", 0, index) + 1
                continued = text[line_start_offset:index].rstrip(" \t\r").endswith("\\")
                if not continued:
                    state = "code"
        else:
            line_start = False
            chars[index] = " "
        index += 1
    return "".join(chars)


def merged_functions(text: str, expected: set[str]) -> tuple[str, dict[str, str], list[str]]:
    masked = mask_comments_strings(text)
    spans: list[tuple[int, int, str]] = []
    depth = 0
    boundary = 0
    open_name: str | None = None
    open_start = 0
    legacy_name: str | None = None
    legacy_name_pos = 0
    for index, ch in enumerate(masked):
        if ch == "{" and depth == 0:
            head = masked[boundary:index]
            match = FUNCTION_HEAD.search(head)
            if legacy_name:
                open_name = legacy_name
                name_pos = legacy_name_pos
            else:
                open_name = match.group(1) if match and match.group(1) not in CONTROL_HEADS else None
                name_pos = boundary + match.start(1) if open_name else 0
            if open_name:
                open_start = text.rfind("\n", boundary, name_pos) + 1
                if not masked[open_start:name_pos].strip():
                    candidate = open_start
                    while candidate > boundary:
                        previous = text.rfind("\n", boundary, candidate - 1) + 1
                        line = masked[previous:candidate]
                        raw_line = text[previous:candidate]
                        if not line.strip():
                            if masked[open_start:name_pos].strip():
                                break
                            open_start = previous
                            candidate = previous
                            continue
                        if any(token in line for token in ";{}") or raw_line.lstrip().startswith("#"):
                            break
                        open_start = previous
                        candidate = previous
                if not masked[open_start:name_pos].strip():
                    raise ValueError(f"multi-line function signature: {open_name}")
            legacy_name = None
            depth = 1
        elif ch == "{" and depth:
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0:
                if open_name:
                    spans.append((open_start, index + 1, open_name))
                open_name = None
                boundary = index + 1
        elif ch == ";" and depth == 0:
            if legacy_name is None:
                head = masked[boundary:index]
                for legacy in re.finditer(
                    r"\b([A-Za-z_]\w*)\s*\(([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)\)\s*(.*)\Z",
                    head,
                    re.S,
                ):
                    params = [part.strip() for part in legacy.group(2).split(",")]
                    declarations = legacy.group(3)
                    if declarations.strip() and any(
                        re.search(rf"\b{re.escape(param)}\b", declarations) for param in params
                    ):
                        legacy_name = legacy.group(1)
                        legacy_name_pos = boundary + legacy.start(1)
                        break
            if legacy_name is None:
                boundary = index + 1
    if depth:
        raise ValueError("unbalanced braces in merged source")
    names = [name for _, _, name in spans]
    selected_names = [name for name in names if name in expected]
    if len(selected_names) != len(set(selected_names)):
        raise ValueError("duplicate in-range function definition in merged source")
    prelude = []
    cursor = 0
    definitions = {}
    excluded = []
    for start, end, name in spans:
        if start < cursor:
            raise ValueError(f"overlapping function definition: {name}")
        prelude.append(text[cursor:start])
        if name in expected:
            definitions[name] = text[start:end].strip() + "\n"
        else:
            excluded.append(name)
        cursor = end
    prelude.append(text[cursor:])
    outside = "".join(prelude)
    return outside, definitions, excluded


REGISTRY = "config/revo.c_compilation_partitions.json"
ASM_INCLUDE = re.compile(r'^\s*INCLUDE_ASM\([^\n]+\);[ \t]*$', re.M)


def boundary_for(root, source: str) -> str | None:
    import json
    from pathlib import Path
    path = Path(root) / REGISTRY
    if not path.is_file():
        return None
    document = json.loads(path.read_text())
    if document.get("schema_version") != 1:
        raise ValueError("unsupported C compilation partition registry")
    return document["boundaries"].get(Path(source).as_posix())


def partition_source(text: str, name: str | None) -> list[str]:
    if name is None:
        return [text]
    _, bodies, _ = merged_functions(text, {name})
    if name not in bodies:
        raise ValueError(f"C compilation boundary has no definition: {name}")
    body = bodies[name].strip()
    end = text.index(body) + len(body)
    prefix, suffix = text[:end], text[end:]
    _, _, names = merged_functions(prefix, set())
    _, definitions, _ = merged_functions(prefix, set(names))
    declarations = prefix
    spans = []
    for definition in definitions.values():
        definition = definition.strip()
        head = definition.split("{", 1)[0].strip()
        if re.search(r"\bstatic\b", mask_comments_strings(head)):
            raise ValueError("static function crosses a C compilation boundary")
        spans.append((prefix.index(definition), definition, head + ";"))
    for pos, definition, declaration in sorted(spans, reverse=True):
        declarations = declarations[:pos] + declaration + declarations[pos + len(definition):]
    declarations = ASM_INCLUDE.sub("", declarations)
    if re.search(r"\bINCLUDE_RODATA\s*\(", mask_comments_strings(declarations)):
        raise ValueError("private rodata crosses a C compilation boundary")
    return [prefix, declarations + suffix]
