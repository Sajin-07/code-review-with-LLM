<!-- META: {"language": "java", "category": "api-style", "team": "petclinic-backend"} -->

# Api-Style Convention (petclinic-backend)

## Rule
REST controllers use @RestController with @RequestMapping at class level, implement interface-based API definitions (OwnersApi), and return ResponseEntity<T> for all endpoints.

## Evidence
rest_controller: 5, response_entity: 84 (very high usage). OwnerRestController uses @RestController, @RequestMapping("/api"), implements OwnersApi, returns ResponseEntity types.

## Examples

### BAD (AVOID)
```java
@Controller returning raw objects or void
```

### GOOD (FOLLOW)
```java
@RestController @RequestMapping("/api") public class OwnerRestController implements OwnersApi { ... return ResponseEntity.ok(ownerDto);
```
