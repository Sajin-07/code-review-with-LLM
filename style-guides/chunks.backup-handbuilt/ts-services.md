<!-- META: {"language": "typescript", "category": "services", "team": "petclinic-frontend"} -->

# Angular Service Conventions

## Services Found: 10

## Rules
- Use @Injectable({ providedIn: 'root' })
- Service class: PascalCase + Service suffix
- HTTP calls return Observable<T>
- Base API URL from environment config, not hardcoded
- Error handling via catchError in the service
