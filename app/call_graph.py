"""
Static call graph analysis.
Extracts method relationships, field access, null contracts -- no LLM needed.

Runs in <1 second on any file. Output is injected into every chunk
so the LLM knows cross-method relationships.
"""

import re
from dataclasses import dataclass, field


@dataclass
class MethodInfo:
    name: str
    params: list[dict] = field(default_factory=list)       # [{name, type}]
    return_type: str = "void"
    calls: list[str] = field(default_factory=list)          # methods this calls
    called_by: list[str] = field(default_factory=list)      # methods that call this
    reads_fields: list[str] = field(default_factory=list)   # this.x reads
    writes_fields: list[str] = field(default_factory=list)  # this.x = ... writes
    has_null_check: bool = False                             # checks null on params?
    can_return_null: bool = False                            # returns null anywhere?
    throws: list[str] = field(default_factory=list)          # declared exceptions
    is_public: bool = True
    line_start: int = 0
    line_end: int = 0


@dataclass
class CallGraph:
    class_name: str
    language: str
    methods: dict[str, MethodInfo] = field(default_factory=dict)
    fields: dict[str, str] = field(default_factory=dict)   # field_name -> type

    def format_for_prompt(self) -> str:
        """Format the call graph as text for LLM injection."""
        if not self.methods:
            return ""

        lines = ["[CALL GRAPH]"]

        for name, info in self.methods.items():
            sig_parts = []
            if info.return_type:
                sig_parts.append(info.return_type)
            param_str = ", ".join(f"{p.get('type','?')} {p.get('name','?')}" for p in info.params)
            sig = f"{name}({param_str})"
            if info.return_type and info.return_type != "void":
                sig = f"{info.return_type} {sig}"

            lines.append(f"- {sig}")

            if info.calls:
                lines.append(f"    calls: {', '.join(info.calls)}")
            if info.called_by:
                lines.append(f"    called by: {', '.join(info.called_by)}")
            if info.writes_fields:
                lines.append(f"    writes: {', '.join('this.' + f for f in info.writes_fields)}")
            if info.reads_fields:
                reads_only = [f for f in info.reads_fields if f not in info.writes_fields]
                if reads_only:
                    lines.append(f"    reads: {', '.join('this.' + f for f in reads_only)}")
            if not info.has_null_check and any(p.get("type") not in ("int", "long", "double", "float", "boolean", "char", "byte", "short", "number") for p in info.params):
                lines.append(f"    WARNING: no null check on parameters")
            if info.can_return_null:
                lines.append(f"    WARNING: can return null")
            if info.throws:
                lines.append(f"    throws: {', '.join(info.throws)}")

        return "\n".join(lines)


# ---- Java Call Graph --------------------------------------------------------

def _extract_java_methods_detailed(code: str) -> dict[str, MethodInfo]:
    """Extract detailed method info from Java code."""
    lines = code.split("\n")
    methods: dict[str, MethodInfo] = {}

    # Find all method signatures and their bodies
    i = 0
    while i < len(lines):
        line = lines[i]

        sig = re.match(
            r"\s*(public|protected|private)\s+"
            r"(?:static\s+)?(?:final\s+)?(?:synchronized\s+)?"
            r"(?:<[^>]+>\s+)?"
            r"(\w+(?:<[^>]+>)?)\s+"       # return type
            r"(\w+)\s*\(([^)]*)\)",        # name(params)
            line,
        )

        if sig:
            access = sig.group(1)
            return_type = sig.group(2)
            method_name = sig.group(3)
            params_str = sig.group(4).strip()

            # Parse params
            params = []
            if params_str:
                for p in re.finditer(r"(@\w+\s+)?(\w+(?:<[^>]+>)?)\s+(\w+)", params_str):
                    params.append({"type": p.group(2), "name": p.group(3)})

            # Check for throws
            throws = []
            throws_match = re.search(r"throws\s+([\w,\s]+)", line)
            if throws_match:
                throws = [t.strip() for t in throws_match.group(1).split(",")]

            # Find method body boundaries
            brace_line = i
            while brace_line < len(lines) and "{" not in lines[brace_line]:
                brace_line += 1

            if brace_line >= len(lines):
                i += 1
                continue

            depth = 0
            method_end = brace_line
            for j in range(brace_line, len(lines)):
                depth += lines[j].count("{") - lines[j].count("}")
                if depth == 0:
                    method_end = j
                    break

            body = "\n".join(lines[brace_line:method_end + 1])

            # Analyze body
            info = MethodInfo(
                name=method_name,
                params=params,
                return_type=return_type,
                is_public=(access == "public"),
                line_start=i + 1,
                line_end=method_end + 1,
                throws=throws,
            )

            # Detect method calls within body
            for call_match in re.finditer(r"(?:this\.)?(\w+)\s*\(", body):
                called = call_match.group(1)
                # Skip common non-method patterns
                if called not in ("if", "for", "while", "switch", "catch", "new", "return",
                                  "System", "Objects", "String", "Integer", "Long", "List",
                                  "Optional", "Arrays", "Collections", "Math", "super",
                                  method_name):  # skip recursion self-reference
                    if called not in info.calls:
                        info.calls.append(called)

            # Detect field reads: this.fieldName or just fieldName (if not local var)
            for field_read in re.finditer(r"this\.(\w+)", body):
                fname = field_read.group(1)
                # Check if it's a write (this.x = ...) or read
                write_pattern = rf"this\.{fname}\s*="
                read_pattern = rf"this\.{fname}(?!\s*=)"
                if re.search(write_pattern, body):
                    if fname not in info.writes_fields:
                        info.writes_fields.append(fname)
                if re.search(read_pattern, body):
                    if fname not in info.reads_fields:
                        info.reads_fields.append(fname)

            # Detect null checks
            for p in params:
                pname = p["name"]
                if re.search(rf"{pname}\s*==\s*null|{pname}\s*!=\s*null|Objects\.requireNonNull\(\s*{pname}|@NotNull|@NonNull", body) or \
                   re.search(rf"{pname}\s*==\s*null|{pname}\s*!=\s*null|Objects\.requireNonNull\(\s*{pname}", line):
                    info.has_null_check = True
                    break

            # Detect if method can return null
            if re.search(r"return\s+null\s*;", body):
                info.can_return_null = True
            if re.search(r"\.orElse\(\s*null\s*\)", body):
                info.can_return_null = True

            methods[method_name] = info
            i = method_end + 1
        else:
            i += 1

    # Build called_by (reverse lookup)
    for name, info in methods.items():
        for called in info.calls:
            if called in methods:
                if name not in methods[called].called_by:
                    methods[called].called_by.append(name)

    return methods


def _extract_java_fields(code: str) -> dict[str, str]:
    """Extract class fields."""
    fields = {}
    for m in re.finditer(r"\s*(?:private|protected|public)\s+(?:final\s+)?(\w+(?:<[^>]+>)?)\s+(\w+)\s*[;=]", code):
        fields[m.group(2)] = m.group(1)
    return fields


# ---- TypeScript Call Graph --------------------------------------------------

def _extract_ts_methods_detailed(code: str) -> dict[str, MethodInfo]:
    """Extract detailed method info from TypeScript code."""
    lines = code.split("\n")
    methods: dict[str, MethodInfo] = {}

    i = 0
    while i < len(lines):
        line = lines[i]

        sig = re.match(
            r"\s*(?:async\s+)?"
            r"(?:public\s+|private\s+|protected\s+)?"
            r"(?:static\s+)?"
            r"(ngOnInit|ngOnDestroy|ngOnChanges|ngAfterViewInit|ngDoCheck|\w+)"
            r"\s*\(([^)]*)\)\s*(?::\s*(\w+(?:<[^>]+>)?))?\s*\{",
            line,
        )

        if sig:
            method_name = sig.group(1)

            if method_name == "constructor":
                depth = 0
                for j in range(i, len(lines)):
                    depth += lines[j].count("{") - lines[j].count("}")
                    if depth == 0:
                        i = j + 1
                        break
                else:
                    i += 1
                continue

            params_str = sig.group(2).strip()
            return_type = sig.group(3) or "void"

            params = []
            if params_str:
                for p in re.finditer(r"(\w+)\s*(?::\s*(\w+(?:<[^>]+>)?))?", params_str):
                    params.append({"name": p.group(1), "type": p.group(2) or "any"})

            depth = 0
            method_end = i
            for j in range(i, len(lines)):
                depth += lines[j].count("{") - lines[j].count("}")
                if depth == 0:
                    method_end = j
                    break

            body = "\n".join(lines[i:method_end + 1])

            info = MethodInfo(
                name=method_name,
                params=params,
                return_type=return_type,
                line_start=i + 1,
                line_end=method_end + 1,
            )

            # Method calls
            for call_match in re.finditer(r"(?:this\.)?(\w+)\s*\(", body):
                called = call_match.group(1)
                if called not in ("if", "for", "while", "switch", "catch", "new", "return",
                                  "console", "setTimeout", "setInterval", "subscribe",
                                  "pipe", "map", "filter", "tap", "switchMap", "mergeMap",
                                  "catchError", "takeUntil", "forEach", "push", "splice",
                                  method_name):
                    if called not in info.calls:
                        info.calls.append(called)

            # Field access
            for fr in re.finditer(r"this\.(\w+)", body):
                fname = fr.group(1)
                if re.search(rf"this\.{fname}\s*=", body):
                    if fname not in info.writes_fields:
                        info.writes_fields.append(fname)
                else:
                    if fname not in info.reads_fields:
                        info.reads_fields.append(fname)

            # Null/undefined checks
            for p in params:
                pname = p["name"]
                if re.search(rf"{pname}\s*===?\s*null|{pname}\s*===?\s*undefined|{pname}\s*!==?\s*null|!\s*{pname}", body):
                    info.has_null_check = True
                    break

            if re.search(r"return\s+(null|undefined)\s*;", body):
                info.can_return_null = True

            methods[method_name] = info
            i = method_end + 1
        else:
            i += 1

    # Build called_by
    for name, info in methods.items():
        for called in info.calls:
            if called in methods:
                if name not in methods[called].called_by:
                    methods[called].called_by.append(name)

    return methods


# ---- Public API -------------------------------------------------------------

def build_call_graph(code: str, language: str, class_name: str = "") -> CallGraph:
    """Build a static call graph from source code."""
    if language == "java":
        methods = _extract_java_methods_detailed(code)
        fields = _extract_java_fields(code)
    elif language in ("typescript", "ts"):
        methods = _extract_ts_methods_detailed(code)
        fields = {}
    else:
        methods = {}
        fields = {}

    return CallGraph(
        class_name=class_name or "Unknown",
        language=language,
        methods=methods,
        fields=fields,
    )
