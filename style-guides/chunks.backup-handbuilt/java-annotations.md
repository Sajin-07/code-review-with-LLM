<!-- META: {"language": "java", "category": "annotations", "team": "petclinic-backend"} -->

# Annotation Conventions

## Class-Level Annotations
- @Profile("jdbc"): 7
- @Profile("jpa"): 7
- @RequestMapping("api"): 6
- @Profile("spring-data-jpa"): 4
- @MappedSuperclass: 3
- @Service: 2
- @SpringBootApplication: 1
- @Configuration: 1
- @Table(name = "owners"): 1
- @Table(name = "pets"): 1

## Method-Level Annotations
- @RequestMapping("api"): 6
- @RequestMapping("/api"): 1
- @RequestMapping("/"): 1
- @RequestMapping(value = "/"): 1

## Rules
- REST controllers use @RestController (not @Controller + @ResponseBody)
- Use specific verbs: @GetMapping, @PostMapping, @PutMapping, @DeleteMapping
- Do NOT use generic @RequestMapping(method=...) for individual endpoints
