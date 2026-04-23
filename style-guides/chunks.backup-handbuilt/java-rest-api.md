<!-- META: {"language": "java", "category": "api", "team": "petclinic-backend"} -->

# REST API Convention

## Endpoints Found: 0


## Rules
- URL paths use kebab-case and plural nouns: /api/owners, /api/pets
- Controllers return ResponseEntity<T> with explicit status codes
- Use @Valid on @RequestBody for input validation
- Use DTOs for request/response -- never expose JPA entities directly
