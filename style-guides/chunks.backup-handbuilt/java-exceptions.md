<!-- META: {"language": "java", "category": "exceptions", "team": "petclinic-backend"} -->

# Exception Handling Convention

## Custom Exceptions: None found
## try/catch blocks: 18

## Rules
- Use @RestControllerAdvice for global exception handling
- Custom exceptions extend RuntimeException for business errors
- Return proper HTTP status codes via ResponseEntity or @ResponseStatus
- Do NOT catch generic Exception -- catch specific types
