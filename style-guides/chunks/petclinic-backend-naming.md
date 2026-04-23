<!-- META: {"language": "java", "category": "naming", "team": "petclinic-backend"} -->

# Naming Convention (petclinic-backend)

## Rule
Java classes use PascalCase (OwnerRestController, PetDto), packages use lowercase with dots, and DTOs/entities follow domain naming with appropriate suffixes (Dto, Mapper).

## Evidence
Files show OwnerRestController, OwnerDto, PetDto, VisitDto, OwnerMapper, PetMapper - all following standard Java naming. Package org.springframework.samples.petclinic.rest.controller uses lowercase.

## Examples

### BAD (AVOID)
```java
class ownerRestController or owner_dto
```

### GOOD (FOLLOW)
```java
class OwnerRestController, OwnerDto, OwnerMapper
```
