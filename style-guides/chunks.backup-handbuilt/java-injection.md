<!-- META: {"language": "java", "category": "injection", "team": "petclinic-backend"} -->

# Dependency Injection Convention

## Pattern: Constructor Injection (REQUIRED)
- Constructor injection found in: 10 classes
- Field injection (@Autowired on fields) found in: 20 cases

## Rules
- ALL dependencies MUST be injected via constructor
- Fields MUST be declared private final
- Do NOT use @Autowired on fields or setters
- Spring auto-detects single-constructor injection

## Examples from codebase
- Constructor injection: BindingErrorsResponse.java, OwnerRestController.java, PetRestController.java
- Field injection (AVOID): BasicAuthenticationConfig.java, UserServiceImpl.java
