<!-- META: {"language": "typescript", "category": "rxjs", "team": "petclinic-frontend"} -->

# RxJS / Observable Conventions

## .subscribe() calls: 44, async pipe: 0
## Top operators: pipe:35, filter:3

## Rules
- Prefer async pipe in templates over manual .subscribe()
- If you must subscribe manually, unsubscribe in ngOnDestroy
- Use takeUntil(destroy$) pattern for bulk unsubscription
- Use switchMap for dependent HTTP calls
- Use catchError in services to handle HTTP errors
