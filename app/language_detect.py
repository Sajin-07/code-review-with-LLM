"""
Auto-detect programming language from code content.
Supports: Java, TypeScript, JavaScript, Python, Go, Rust, C#, C++, Ruby, PHP, Kotlin, Swift.
Falls back to user-provided language if detection confidence is low.
"""

import re

LANGUAGE_SIGNALS = {
    "java": {
        "strong": [r"public\s+class\s+\w+", r"@Override", r"@Autowired", r"@Service", r"@RestController",
                   r"System\.out\.", r"import\s+java\.", r"import\s+org\.", r"package\s+\w+"],
        "moderate": [r"private\s+final\s+\w+", r"void\s+\w+\(", r"\.class\b", r"throws\s+\w+"],
    },
    "typescript": {
        "strong": [r"@Component", r"@Injectable", r"@NgModule", r"@Input\(\)", r"@Output\(\)",
                   r"Observable<", r"ngOnInit", r"ngOnDestroy", r":\s*\w+\[\]", r"import.*from\s+['\"]@"],
        "moderate": [r"interface\s+\w+\s*\{", r":\s*string", r":\s*number", r":\s*boolean",
                    r"export\s+class", r"export\s+interface"],
    },
    "javascript": {
        "strong": [r"require\(", r"module\.exports", r"const\s+\w+\s*=\s*require",
                   r"export\s+default", r"=>\s*\{"],
        "moderate": [r"function\s+\w+\(", r"let\s+\w+", r"const\s+\w+", r"var\s+\w+",
                    r"console\.log", r"document\."],
    },
    "python": {
        "strong": [r"def\s+\w+\(self", r"import\s+\w+", r"from\s+\w+\s+import", r"class\s+\w+:",
                   r"if\s+__name__\s*==", r"print\(", r"self\.\w+"],
        "moderate": [r"def\s+\w+\(", r":\s*$", r"#\s+", r"return\s+", r"lambda\s+"],
    },
    "go": {
        "strong": [r"func\s+\w+\(", r"package\s+main", r"import\s+\(", r"fmt\.\w+",
                   r":=\s+", r"func\s+\(\w+\s+\*?\w+\)"],
        "moderate": [r"var\s+\w+\s+\w+", r"go\s+func", r"chan\s+\w+", r"defer\s+"],
    },
    "rust": {
        "strong": [r"fn\s+\w+\(", r"let\s+mut\s+", r"impl\s+\w+", r"pub\s+fn",
                   r"use\s+std::", r"#\[derive", r"->.*\{"],
        "moderate": [r"&self", r"&mut\s+", r"println!\(", r"unwrap\(\)", r"match\s+\w+"],
    },
    "csharp": {
        "strong": [r"using\s+System", r"namespace\s+\w+", r"public\s+class\s+\w+\s*:",
                   r"\[HttpGet\]", r"\[HttpPost\]", r"async\s+Task<"],
        "moderate": [r"var\s+\w+\s*=", r"Console\.Write", r"string\s+\w+", r"int\s+\w+"],
    },
    "cpp": {
        "strong": [r"#include\s*<", r"std::", r"int\s+main\(", r"cout\s*<<",
                   r"nullptr", r"template\s*<"],
        "moderate": [r"void\s+\w+\(", r"::\w+\(", r"->", r"new\s+\w+"],
    },
    "kotlin": {
        "strong": [r"fun\s+\w+\(", r"val\s+\w+", r"var\s+\w+:", r"companion\s+object",
                   r"data\s+class", r"override\s+fun"],
        "moderate": [r"when\s*\(", r"println\(", r"it\.", r"\?\."],
    },
    "ruby": {
        "strong": [r"def\s+\w+\s*$", r"class\s+\w+\s*<", r"require\s+['\"]",
                   r"attr_accessor", r"end\s*$"],
        "moderate": [r"puts\s+", r"\.each\s+do", r"\|.*\|", r"@\w+"],
    },
    "php": {
        "strong": [r"<\?php", r"\$\w+\s*=", r"function\s+\w+\(",
                   r"echo\s+", r"->", r"use\s+\\"],
        "moderate": [r"\$this->", r"namespace\s+", r"public\s+function"],
    },
    "swift": {
        "strong": [r"func\s+\w+\(", r"import\s+Foundation", r"var\s+\w+:\s*\w+",
                   r"let\s+\w+:\s*\w+", r"guard\s+let", r"@IBOutlet"],
        "moderate": [r"print\(", r"nil\b", r"struct\s+\w+", r"enum\s+\w+"],
    },
}


def detect_language(code: str, hint: str = "") -> dict:
    """
    Detect programming language from code content.
    Returns {language, confidence, scores}.
    """
    scores = {}

    for lang, signals in LANGUAGE_SIGNALS.items():
        score = 0
        for pattern in signals.get("strong", []):
            if re.search(pattern, code, re.MULTILINE):
                score += 3
        for pattern in signals.get("moderate", []):
            if re.search(pattern, code, re.MULTILINE):
                score += 1
        scores[lang] = score

    # Sort by score
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    top_lang = ranked[0][0] if ranked else "unknown"
    top_score = ranked[0][1] if ranked else 0

    # Confidence levels
    if top_score >= 6:
        confidence = "high"
    elif top_score >= 3:
        confidence = "medium"
    else:
        confidence = "low"

    # If user gave a hint and confidence is low, trust the hint
    if hint and confidence == "low":
        top_lang = hint
        confidence = "user-provided"

    # If hint matches a detected language, boost confidence
    if hint and hint.lower() == top_lang:
        confidence = "high"

    return {
        "language": top_lang,
        "confidence": confidence,
        "scores": {k: v for k, v in ranked[:5] if v > 0},
    }
