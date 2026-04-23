<!-- META: {"language": "typescript", "category": "components", "team": "petclinic-frontend"} -->

# Angular Component Conventions

## Components Found: 24

## Rules
- Selector uses kebab-case with app- prefix: app-owner-list, app-pet-edit
- One component per file
- Component class: PascalCase + Component suffix
- Keep templates in separate .html files
- Components should be thin -- delegate logic to services
- Use OnInit for initialization, not constructor
