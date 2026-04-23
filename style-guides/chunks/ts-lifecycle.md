<!-- META: {"language": "typescript", "category": "lifecycle", "team": "petclinic-frontend"} -->

# Lifecycle Hook Conventions

## Hooks Used: ngOnInit:22

## Rules
- Use ngOnInit for initialization -- NOT the constructor
- Constructor is ONLY for dependency injection
- Implement OnDestroy if the component has manual subscriptions
- Do NOT put heavy logic in ngDoCheck or ngAfterViewChecked
