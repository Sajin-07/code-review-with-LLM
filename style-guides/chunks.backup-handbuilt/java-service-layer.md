<!-- META: {"language": "java", "category": "service", "team": "petclinic-backend"} -->

# Service Layer Convention

## Service Files: 14, Using @Transactional: 3

## Rules
- Services use @Service annotation
- Business logic lives in services -- NOT in controllers or repositories
- Use @Transactional on methods that modify data
- Read-only methods use @Transactional(readOnly = true)
- Services accept and return DTOs
