"""
Few-shot pairs for RAG injection.
Each pair: BAD code -> GOOD code -> explanation.
Tagged with team for multi-team isolation.
"""

JAVA_FEW_SHOTS = [
    {
        "id": "petclinic-java-fs-01",
        "language": "java",
        "category": "injection",
        "team": "petclinic-backend",
        "title": "Field injection -> Constructor injection",
        "bad": "@RestController\npublic class OwnerController {\n    @Autowired\n    private OwnerService ownerService;\n\n    @Autowired\n    private PetService petService;\n}",
        "good": "@RestController\npublic class OwnerController {\n    private final OwnerService ownerService;\n    private final PetService petService;\n\n    public OwnerController(OwnerService ownerService, PetService petService) {\n        this.ownerService = ownerService;\n        this.petService = petService;\n    }\n}",
        "explanation": "Field injection hides dependencies and prevents immutability. Constructor injection makes dependencies explicit, fields final, and the class testable without Spring context.",
    },
    {
        "id": "petclinic-java-fs-02",
        "language": "java",
        "category": "api",
        "team": "petclinic-backend",
        "title": "Exposing JPA entity directly -> Use DTO",
        "bad": "@GetMapping(\"/owners/{id}\")\npublic Owner getOwner(@PathVariable Long id) {\n    return ownerRepository.findById(id).get();\n}",
        "good": "@GetMapping(\"/owners/{id}\")\npublic ResponseEntity<OwnerDto> getOwner(@PathVariable Long id) {\n    Owner owner = ownerService.findById(id);\n    return ResponseEntity.ok(ownerMapper.toDto(owner));\n}",
        "explanation": "Never expose JPA entities in API responses. Use DTOs to control serialization, avoid lazy-loading issues, and decouple internal model from API contract.",
    },
    {
        "id": "petclinic-java-fs-03",
        "language": "java",
        "category": "exceptions",
        "team": "petclinic-backend",
        "title": "Generic catch-all -> Specific exception handling",
        "bad": "@PostMapping(\"/visits\")\npublic ResponseEntity<?> addVisit(@RequestBody Visit visit) {\n    try {\n        visitService.save(visit);\n        return ResponseEntity.ok(visit);\n    } catch (Exception e) {\n        return ResponseEntity.status(500).body(\"Error\");\n    }\n}",
        "good": "@PostMapping(\"/visits\")\npublic ResponseEntity<VisitDto> addVisit(@Valid @RequestBody VisitDto visitDto) {\n    VisitDto saved = visitService.create(visitDto);\n    return ResponseEntity.status(HttpStatus.CREATED).body(saved);\n}\n// Exception handling in @RestControllerAdvice",
        "explanation": "Don't catch generic Exception in controllers. Use @Valid for input validation, let business exceptions propagate to @RestControllerAdvice.",
    },
    {
        "id": "petclinic-java-fs-04",
        "language": "java",
        "category": "naming",
        "team": "petclinic-backend",
        "title": "Vague naming -> Intention-revealing names",
        "bad": "public List<Owner> get(String s, int t) {\n    var l = repo.findAll();\n    return l.stream().filter(o -> o.getName().contains(s)).limit(t).toList();\n}",
        "good": "public List<OwnerDto> findOwnersByNameContaining(String nameFragment, int maxResults) {\n    return ownerRepository.findByLastNameContaining(nameFragment,\n            PageRequest.of(0, maxResults))\n        .stream().map(ownerMapper::toDto).toList();\n}",
        "explanation": "Method name should describe what it does. Parameters need meaningful names. Use repository query methods instead of fetching all and filtering in memory.",
    },
    {
        "id": "petclinic-java-fs-05",
        "language": "java",
        "category": "service",
        "team": "petclinic-backend",
        "title": "Business logic in controller -> Move to service layer",
        "bad": "@PostMapping(\"/owners/{ownerId}/pets\")\npublic ResponseEntity<Pet> addPet(@PathVariable Long ownerId, @RequestBody Pet pet) {\n    Owner owner = ownerRepository.findById(ownerId).orElseThrow();\n    pet.setOwner(owner);\n    pet.setRegistrationDate(LocalDate.now());\n    petRepository.save(pet);\n    return ResponseEntity.ok(pet);\n}",
        "good": "@PostMapping(\"/owners/{ownerId}/pets\")\npublic ResponseEntity<PetDto> addPet(\n        @PathVariable Long ownerId,\n        @Valid @RequestBody PetCreateDto petDto) {\n    PetDto created = petService.addPetToOwner(ownerId, petDto);\n    return ResponseEntity.status(HttpStatus.CREATED).body(created);\n}",
        "explanation": "Controllers handle HTTP concerns only. Business logic (setting dates, linking entities) belongs in the service layer.",
    },
]

TYPESCRIPT_FEW_SHOTS = [
    {
        "id": "petclinic-ts-fs-01",
        "language": "typescript",
        "category": "lifecycle",
        "team": "petclinic-frontend",
        "title": "Logic in constructor -> Use ngOnInit",
        "bad": "export class OwnerListComponent {\n    owners: Owner[];\n    constructor(private ownerService: OwnerService) {\n        this.ownerService.getOwners().subscribe(data => {\n            this.owners = data;\n        });\n    }\n}",
        "good": "export class OwnerListComponent implements OnInit, OnDestroy {\n    owners: Owner[] = [];\n    private destroy$ = new Subject<void>();\n    constructor(private ownerService: OwnerService) {}\n    ngOnInit(): void {\n        this.ownerService.getOwners()\n            .pipe(takeUntil(this.destroy$))\n            .subscribe(owners => this.owners = owners);\n    }\n    ngOnDestroy(): void {\n        this.destroy$.next();\n        this.destroy$.complete();\n    }\n}",
        "explanation": "Constructor is for DI only. Use ngOnInit for initialization. Always unsubscribe with takeUntil + destroy$ pattern.",
    },
    {
        "id": "petclinic-ts-fs-02",
        "language": "typescript",
        "category": "typing",
        "team": "petclinic-frontend",
        "title": "Using any -> Proper interface typing",
        "bad": "export class PetService {\n    getById(id: number): Observable<any> {\n        return this.http.get<any>(`/api/pets/${id}`);\n    }\n}",
        "good": "export interface Pet {\n    id: number;\n    name: string;\n    birthDate: string;\n}\nexport class PetService {\n    getById(id: number): Observable<Pet> {\n        return this.http.get<Pet>(`${environment.apiUrl}/pets/${id}`);\n    }\n}",
        "explanation": "Never use any for API responses. Define interfaces matching the API contract. Use environment variables for base URL.",
    },
    {
        "id": "petclinic-ts-fs-03",
        "language": "typescript",
        "category": "rxjs",
        "team": "petclinic-frontend",
        "title": "Nested subscribes -> Use switchMap",
        "bad": "loadPetDetails(ownerId: number, petId: number) {\n    this.ownerService.getOwner(ownerId).subscribe(owner => {\n        this.owner = owner;\n        this.petService.getPet(petId).subscribe(pet => {\n            this.pet = pet;\n        });\n    });\n}",
        "good": "loadPetDetails(ownerId: number, petId: number) {\n    this.ownerService.getOwner(ownerId).pipe(\n        tap(owner => this.owner = owner),\n        switchMap(() => this.petService.getPet(petId)),\n        takeUntil(this.destroy$)\n    ).subscribe(pet => this.pet = pet);\n}",
        "explanation": "Nested subscribes are unreadable and leak subscriptions. Use RxJS operators: switchMap for dependent calls, takeUntil for cleanup.",
    },
    {
        "id": "petclinic-ts-fs-04",
        "language": "typescript",
        "category": "components",
        "team": "petclinic-frontend",
        "title": "Fat component -> Delegate to service",
        "bad": "export class OwnerSearchComponent {\n    owners: Owner[] = [];\n    constructor(private http: HttpClient) {}\n    search() {\n        this.http.get<Owner[]>('/api/owners').subscribe(all => {\n            this.owners = all.filter(o => o.lastName.includes(this.term));\n        });\n    }\n}",
        "good": "export class OwnerSearchComponent implements OnInit {\n    owners$!: Observable<Owner[]>;\n    private searchTerms = new Subject<string>();\n    constructor(private ownerService: OwnerService) {}\n    ngOnInit(): void {\n        this.owners$ = this.searchTerms.pipe(\n            debounceTime(300),\n            distinctUntilChanged(),\n            switchMap(term => this.ownerService.findByLastName(term))\n        );\n    }\n}",
        "explanation": "Components should not use HttpClient directly. Use reactive search with debounce. Use async pipe with Observable.",
    },
    {
        "id": "petclinic-ts-fs-05",
        "language": "typescript",
        "category": "services",
        "team": "petclinic-frontend",
        "title": "No error handling in HTTP service -> Proper catchError",
        "bad": "export class VisitService {\n    private url = 'http://localhost:9966/petclinic/api/visits';\n    constructor(private http: HttpClient) {}\n    getVisits(petId: number): Observable<Visit[]> {\n        return this.http.get<Visit[]>(this.url + '?petId=' + petId);\n    }\n}",
        "good": "export class VisitService {\n    constructor(private http: HttpClient, private errorHandler: ErrorHandlerService) {}\n    getVisits(petId: number): Observable<Visit[]> {\n        return this.http.get<Visit[]>(\n            `${environment.apiUrl}/visits`,\n            { params: { petId: petId.toString() } }\n        ).pipe(catchError(err => this.errorHandler.handle<Visit[]>(err, [])));\n    }\n}",
        "explanation": "Never hardcode URLs. Use HttpParams for query parameters. Handle errors with catchError in the service.",
    },
]

ALL_FEW_SHOTS = JAVA_FEW_SHOTS + TYPESCRIPT_FEW_SHOTS


def format_for_embedding(shot: dict) -> str:
    return f"""## {shot['title']}

### BAD -- do NOT write code like this:
```
{shot['bad']}
```

### GOOD -- follow this pattern:
```
{shot['good']}
```

### Why: {shot['explanation']}
"""
