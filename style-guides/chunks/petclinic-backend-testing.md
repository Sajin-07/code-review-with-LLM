<!-- META: {"language": "java", "category": "testing", "team": "petclinic-backend"} -->

# Testing Convention (petclinic-backend)

## Rule
Uses JUnit 5 (@Test, @BeforeEach) with Spring Boot test annotations (@SpringBootTest, @ContextConfiguration). Controllers tested via MockMvc with @MockitoBean for mocking services.

## Evidence
junit: 81 (high), test files show @SpringBootTest, @ContextConfiguration, MockMvc setup, @MockitoBean usage, @WithMockUser for security.

## Examples

### BAD (AVOID)
```java
Testing without Spring context or using JUnit 4
```

### GOOD (FOLLOW)
```java
@SpringBootTest @ContextConfiguration(classes=ApplicationTestConfig.class) class OwnerRestControllerTests { @MockitoBean private ClinicService clinicService; }
```
