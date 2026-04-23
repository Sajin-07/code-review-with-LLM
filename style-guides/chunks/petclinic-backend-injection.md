<!-- META: {"language": "java", "category": "injection", "team": "petclinic-backend"} -->

# Injection Convention (petclinic-backend)

## Rule
Dependency injection uses constructor injection with private final fields, not field injection. The high autowired_fields count (16) likely comes from test classes, while production code uses constructor injection.

## Evidence
OwnerRestController shows 'private final ClinicService clinicService; private final OwnerMapper ownerMapper;' with constructor injection pattern. The 13 private_final count reflects this.

## Examples

### BAD (AVOID)
```java
@Autowired private ClinicService clinicService;
```

### GOOD (FOLLOW)
```java
private final ClinicService clinicService; // constructor injected
```
