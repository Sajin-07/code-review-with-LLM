#!/usr/bin/env python3
"""
Scan PetClinic repos and extract coding conventions as chunked Markdown docs.
Each chunk is tagged with language, category, AND team.

Usage:  python3 scripts/extract-styles.py
Output: style-guides/chunks/*.md
"""

import os
import re
import json
from pathlib import Path
from collections import Counter

BASE = Path(__file__).resolve().parent.parent
JAVA_ROOT = BASE / "repos" / "spring-petclinic-rest"
NG_ROOT = BASE / "repos" / "spring-petclinic-angular"
OUT = BASE / "style-guides" / "chunks"

# Team names for the PetClinic repos
JAVA_TEAM = "petclinic-backend"
TS_TEAM = "petclinic-frontend"


def find_files(root: Path, ext: str) -> list[Path]:
    return sorted(root.rglob(f"*{ext}"))

def read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")

def write_chunk(filename: str, language: str, category: str, team: str, content: str):
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / filename
    meta = json.dumps({"language": language, "category": category, "team": team})
    path.write_text(f"<!-- META: {meta} -->\n\n{content}", encoding="utf-8")
    print(f"  OK: {filename}  ({language}/{category}/team:{team})  ~{len(content)} chars")


def analyze_java():
    print("\n-- Analyzing Java (spring-petclinic-rest) --")
    src = JAVA_ROOT / "src"
    if not src.exists():
        print("  src/ not found -- skipping Java")
        return
    java_files = find_files(src, ".java")
    print(f"  Found {len(java_files)} Java files")
    all_code = {f: read(f) for f in java_files}

    # Injection patterns
    field_inject = 0
    constructor_inject = 0
    inject_examples = {"field": [], "constructor": []}
    for f, code in all_code.items():
        if "@Autowired" in code:
            fi = re.findall(r"@Autowired\s*\n\s*(private|protected)\s+\w+", code)
            field_inject += len(fi)
            if fi and len(inject_examples["field"]) < 2:
                inject_examples["field"].append(f.name)
        if re.search(r"private\s+final\s+\w+", code):
            constructor_inject += 1
            if len(inject_examples["constructor"]) < 3:
                inject_examples["constructor"].append(f.name)

    write_chunk("java-injection.md", "java", "injection", JAVA_TEAM, f"""# Dependency Injection Convention

## Pattern: Constructor Injection (REQUIRED)
- Constructor injection found in: {constructor_inject} classes
- Field injection (@Autowired on fields) found in: {field_inject} cases

## Rules
- ALL dependencies MUST be injected via constructor
- Fields MUST be declared private final
- Do NOT use @Autowired on fields or setters
- Spring auto-detects single-constructor injection

## Examples from codebase
- Constructor injection: {', '.join(inject_examples['constructor'][:3])}
- Field injection (AVOID): {', '.join(inject_examples['field'][:2]) or 'none found'}
""")

    # Annotations
    class_annot_counter = Counter()
    method_annot_counter = Counter()
    for f, code in all_code.items():
        for m in re.finditer(r"(@\w+(?:\([^)]*\))?)\s*\n\s*public\s+class", code):
            class_annot_counter[m.group(1)] += 1
        for m in re.finditer(r"(@\w+(?:Mapping|ExceptionHandler)[^)]*\))", code):
            method_annot_counter[m.group(1)] += 1

    write_chunk("java-annotations.md", "java", "annotations", JAVA_TEAM, f"""# Annotation Conventions

## Class-Level Annotations
{chr(10).join(f'- {a}: {c}' for a, c in class_annot_counter.most_common(10))}

## Method-Level Annotations
{chr(10).join(f'- {a}: {c}' for a, c in method_annot_counter.most_common(10))}

## Rules
- REST controllers use @RestController (not @Controller + @ResponseBody)
- Use specific verbs: @GetMapping, @PostMapping, @PutMapping, @DeleteMapping
- Do NOT use generic @RequestMapping(method=...) for individual endpoints
""")

    # Naming
    class_names = []
    for code in all_code.values():
        class_names += re.findall(r"public\s+(?:class|interface|enum)\s+(\w+)", code)
    suffixes = Counter()
    for name in class_names:
        for s in ["Controller", "Service", "Repository", "Mapper", "Dto", "DTO", "Config", "Exception"]:
            if name.endswith(s):
                suffixes[s] += 1

    write_chunk("java-naming.md", "java", "naming", JAVA_TEAM, f"""# Naming Conventions

## Class Naming -- Suffix by Role
{chr(10).join(f'- *{s}: {c} classes' for s, c in suffixes.most_common())}

## Rules
- Classes use PascalCase with role suffix: OwnerController, PetService, VisitRepository
- Methods use camelCase with verb prefix: findById, addOwner, updatePet
- Boolean methods use is* or has* prefix
- Repository methods follow Spring Data naming: findBy*, deleteBy*
""")

    # Exceptions
    exception_classes = []
    for f, code in all_code.items():
        if "extends" in code and "Exception" in code:
            exception_classes += re.findall(r"class\s+(\w+)\s+extends\s+\w*Exception", code)
    try_catches = sum(code.count("try {") + code.count("try{") for code in all_code.values())

    write_chunk("java-exceptions.md", "java", "exceptions", JAVA_TEAM, f"""# Exception Handling Convention

## Custom Exceptions: {', '.join(exception_classes) or 'None found'}
## try/catch blocks: {try_catches}

## Rules
- Use @RestControllerAdvice for global exception handling
- Custom exceptions extend RuntimeException for business errors
- Return proper HTTP status codes via ResponseEntity or @ResponseStatus
- Do NOT catch generic Exception -- catch specific types
""")

    # REST API
    endpoints = []
    for f, code in all_code.items():
        for m in re.finditer(r'@(Get|Post|Put|Delete|Patch)Mapping\s*\(\s*["\']([^"\']*)', code):
            endpoints.append((m.group(1), m.group(2), f.stem))

    write_chunk("java-rest-api.md", "java", "api", JAVA_TEAM, f"""# REST API Convention

## Endpoints Found: {len(endpoints)}
{chr(10).join(f'- {v}Mapping {p} in {c}' for v, p, c in endpoints[:15])}

## Rules
- URL paths use kebab-case and plural nouns: /api/owners, /api/pets
- Controllers return ResponseEntity<T> with explicit status codes
- Use @Valid on @RequestBody for input validation
- Use DTOs for request/response -- never expose JPA entities directly
""")

    # Testing
    test_files = [f for f in java_files if "test" in str(f).lower() or "Test" in f.name]
    test_annotations = Counter()
    for f in test_files:
        code = all_code.get(f, "")
        for a in ["@Test", "@BeforeEach", "@MockBean", "@WebMvcTest", "@SpringBootTest", "@DataJpaTest"]:
            if a in code:
                test_annotations[a] += 1

    write_chunk("java-testing.md", "java", "testing", JAVA_TEAM, f"""# Testing Convention

## Test Files: {len(test_files)}
## Annotations: {', '.join(f'{a}:{c}' for a, c in test_annotations.most_common())}

## Rules
- Test class naming: <ClassName>Test.java
- Use JUnit 5 (@Test from org.junit.jupiter.api)
- Use @WebMvcTest for controller tests (slice test)
- Use @DataJpaTest for repository tests
- Use @SpringBootTest only for integration tests
""")

    # Service layer
    service_files = [f for f in java_files if "Service" in f.name]
    transactional_count = sum(1 for f in service_files if "@Transactional" in all_code.get(f, ""))

    write_chunk("java-service-layer.md", "java", "service", JAVA_TEAM, f"""# Service Layer Convention

## Service Files: {len(service_files)}, Using @Transactional: {transactional_count}

## Rules
- Services use @Service annotation
- Business logic lives in services -- NOT in controllers or repositories
- Use @Transactional on methods that modify data
- Read-only methods use @Transactional(readOnly = true)
- Services accept and return DTOs
""")


def analyze_angular():
    print("\n-- Analyzing Angular (spring-petclinic-angular) --")
    src = NG_ROOT / "src"
    if not src.exists():
        print("  src/ not found -- skipping Angular")
        return
    ts_files = find_files(src, ".ts")
    html_files = find_files(src, ".html")
    print(f"  Found {len(ts_files)} TypeScript, {len(html_files)} HTML files")
    all_ts = {f: read(f) for f in ts_files}

    # Components
    components = []
    for f, code in all_ts.items():
        if "@Component" in code:
            name = re.search(r"export\s+class\s+(\w+)", code)
            sel = re.search(r"selector:\s*['\"]([^'\"]+)", code)
            components.append({"class": name.group(1) if name else f.stem, "selector": sel.group(1) if sel else "?"})

    write_chunk("ts-components.md", "typescript", "components", TS_TEAM, f"""# Angular Component Conventions

## Components Found: {len(components)}

## Rules
- Selector uses kebab-case with app- prefix: app-owner-list, app-pet-edit
- One component per file
- Component class: PascalCase + Component suffix
- Keep templates in separate .html files
- Components should be thin -- delegate logic to services
- Use OnInit for initialization, not constructor
""")

    # Services
    services = []
    for f, code in all_ts.items():
        if "@Injectable" in code:
            name = re.search(r"export\s+class\s+(\w+)", code)
            services.append(name.group(1) if name else f.stem)

    write_chunk("ts-services.md", "typescript", "services", TS_TEAM, f"""# Angular Service Conventions

## Services Found: {len(services)}

## Rules
- Use @Injectable({{ providedIn: 'root' }})
- Service class: PascalCase + Service suffix
- HTTP calls return Observable<T>
- Base API URL from environment config, not hardcoded
- Error handling via catchError in the service
""")

    # RxJS
    rxjs_ops = Counter()
    for code in all_ts.values():
        for op in ["map", "tap", "catchError", "switchMap", "filter", "take", "takeUntil", "pipe"]:
            count = code.count(f".{op}(")
            if count: rxjs_ops[op] += count
    subscribe_count = sum(code.count(".subscribe(") for code in all_ts.values())
    async_pipe_count = sum(read(f).count("| async") for f in html_files)

    write_chunk("ts-rxjs.md", "typescript", "rxjs", TS_TEAM, f"""# RxJS / Observable Conventions

## .subscribe() calls: {subscribe_count}, async pipe: {async_pipe_count}
## Top operators: {', '.join(f'{op}:{c}' for op, c in rxjs_ops.most_common(8))}

## Rules
- Prefer async pipe in templates over manual .subscribe()
- If you must subscribe manually, unsubscribe in ngOnDestroy
- Use takeUntil(destroy$) pattern for bulk unsubscription
- Use switchMap for dependent HTTP calls
- Use catchError in services to handle HTTP errors
""")

    # Lifecycle
    hooks = Counter()
    for code in all_ts.values():
        for hook in ["ngOnInit", "ngOnDestroy", "ngOnChanges", "ngAfterViewInit"]:
            if hook in code: hooks[hook] += 1

    write_chunk("ts-lifecycle.md", "typescript", "lifecycle", TS_TEAM, f"""# Lifecycle Hook Conventions

## Hooks Used: {', '.join(f'{h}:{c}' for h, c in hooks.most_common())}

## Rules
- Use ngOnInit for initialization -- NOT the constructor
- Constructor is ONLY for dependency injection
- Implement OnDestroy if the component has manual subscriptions
- Do NOT put heavy logic in ngDoCheck or ngAfterViewChecked
""")

    # Modules
    modules = []
    for f, code in all_ts.items():
        if "@NgModule" in code:
            name = re.search(r"export\s+class\s+(\w+)", code)
            modules.append(name.group(1) if name else f.stem)

    write_chunk("ts-modules.md", "typescript", "modules", TS_TEAM, f"""# Module & Routing Conventions

## Modules Found: {len(modules)}

## Rules
- Feature modules group related components, services, and routes
- Lazy-load feature modules via loadChildren
- Route paths use kebab-case: /owners, /pets/:id/edit
- Shared components go in SharedModule
""")

    # Typing
    any_count = sum(code.count(": any") + code.count("<any>") for code in all_ts.values())
    interface_count = sum(len(re.findall(r"export\s+interface\s+\w+", code)) for code in all_ts.values())

    write_chunk("ts-typing.md", "typescript", "typing", TS_TEAM, f"""# TypeScript Typing Conventions

## Interfaces: {interface_count}, Uses of any: {any_count}

## Rules
- AVOID any -- define interfaces for all data structures
- API response models use interface (not class)
- Use strict TypeScript (strict: true in tsconfig)
- Prefer readonly for properties that should not be reassigned
""")


if __name__ == "__main__":
    print("Style Extraction -- scanning PetClinic repos\n")
    if JAVA_ROOT.exists():
        analyze_java()
    else:
        print(f"WARNING: {JAVA_ROOT} not found. Run: bash scripts/clone-repos.sh")
    if NG_ROOT.exists():
        analyze_angular()
    else:
        print(f"WARNING: {NG_ROOT} not found. Run: bash scripts/clone-repos.sh")
    chunks = list(OUT.glob("*.md")) if OUT.exists() else []
    print(f"\nExtracted {len(chunks)} style chunks into {OUT}/")
