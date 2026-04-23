<!-- META: {"language": "typescript", "category": "typing", "team": "petclinic-frontend"} -->

# TypeScript Typing Conventions

## Interfaces: 6, Uses of any: 4

## Rules
- AVOID any -- define interfaces for all data structures
- API response models use interface (not class)
- Use strict TypeScript (strict: true in tsconfig)
- Prefer readonly for properties that should not be reassigned
