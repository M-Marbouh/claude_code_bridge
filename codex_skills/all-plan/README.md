# All-Plan Skill

Collaborative planning using abstract roles defined in CLAUDE.md Role Assignment table.

## Usage

```
/all-plan <your requirement or feature request>
```

Example:
```
/all-plan Design a caching layer for the API with Redis
```

## How It Works

**5-Phase Design Process:**

1. **Requirement Clarification** - 5-Dimension readiness model, structured Q&A
2. **Inspiration Brainstorming** - Creative ideas from `inspiration` (reference only)
3. **Design** - `designer` creates the full plan, integrating adopted ideas
4. **Ratification** - `reviewer` verdicts the plan (concur / concur-with-amendment / contest / insufficient-evidence)
5. **Final Output** - Actionable plan saved to `plans/` directory

## Roles Used

| Role | Responsibility |
|------|---------------|
| `designer` | Primary planner, owns the plan |
| `inspiration` | Creative consultant (unreliable, user decides) |
| `reviewer` | Ratification gate (concur/amend/contest, see Mutual Ratification in CLAUDE.md) |

Roles resolve to providers via CLAUDE.md `CCB_ROLES` table.

## Key Features

- **Structured Clarification**: 5-Dimension readiness scoring (100 pts)
- **Inspiration Filter**: Adopt / Adapt / Discard with user approval
- **Ratification Gate**: Concur/amend/contest verdict, revise-and-reratify loop (max 3 rounds)
- **Optional Web Research**: Triggered when requirements depend on external info

## When to Use

- Complex features requiring thorough planning
- Architectural decisions with multiple valid approaches
- Tasks involving creative/aesthetic elements (leverages `inspiration`)

## Output

A comprehensive plan including:
- Goal and architecture with rationale
- Implementation steps with dependencies
- Risk management matrix
- Ratification verdict and justification
- Inspiration credits (adopted/adapted/discarded)
