<!-- META: {"language": "java", "category": "testing", "team": "petclinic-backend"} -->

# Testing Convention

## Test Files: 21
## Annotations: @Test:16, @SpringBootTest:16, @BeforeEach:8

## Rules
- Test class naming: <ClassName>Test.java
- Use JUnit 5 (@Test from org.junit.jupiter.api)
- Use @WebMvcTest for controller tests (slice test)
- Use @DataJpaTest for repository tests
- Use @SpringBootTest only for integration tests
