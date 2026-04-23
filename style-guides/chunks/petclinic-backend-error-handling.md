<!-- META: {"language": "java", "category": "error-handling", "team": "petclinic-backend"} -->

# Error-Handling Convention (petclinic-backend)

## Rule
Global exception handling via @ControllerAdvice (ExceptionControllerAdvice), with throws declarations propagated. Try-catch used sparingly (9 occurrences).

## Evidence
exception_handler: 3, throws: 112 (high), try_catch: 9 (low). Files import ExceptionControllerAdvice for centralized error handling.

## Examples

### BAD (AVOID)
```java
try-catch in every method with local error handling
```

### GOOD (FOLLOW)
```java
throw specific exceptions from service layer, handle globally via @ExceptionHandler in advice class
```
