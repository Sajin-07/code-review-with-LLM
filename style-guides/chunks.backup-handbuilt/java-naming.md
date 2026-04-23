<!-- META: {"language": "java", "category": "naming", "team": "petclinic-backend"} -->

# Naming Conventions

## Class Naming -- Suffix by Role
- *Repository: 14 classes
- *Mapper: 8 classes
- *Controller: 8 classes
- *Config: 4 classes
- *Service: 2 classes

## Rules
- Classes use PascalCase with role suffix: OwnerController, PetService, VisitRepository
- Methods use camelCase with verb prefix: findById, addOwner, updatePet
- Boolean methods use is* or has* prefix
- Repository methods follow Spring Data naming: findBy*, deleteBy*
