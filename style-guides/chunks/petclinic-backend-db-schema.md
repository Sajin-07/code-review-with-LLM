<!-- META: {"language": "java", "category": "db-schema", "team": "petclinic-backend"} -->

# Db-Schema Convention (petclinic-backend)

## Rule
Uses JPA/Hibernate with Spring Data JPA repositories. Entities use JPA annotations (@Entity, @Table, @Column).

## Evidence
repository: 7 (multiple repository interfaces). Standard Spring Data JPA pattern with entity models like Owner, Pet, Visit.

## Examples

### BAD (AVOID)
```java
Raw JDBC or no ORM
```

### GOOD (FOLLOW)
```java
@Repository interface PetRepository extends JpaRepository<Pet, Integer>
```
