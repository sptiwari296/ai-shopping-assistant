# DESIGN.md: NovaMart assistant UI

The UI stays in Streamlit (the project's own framework). The visual system comes from HookForge; the craft rules come from the awwwards skill.

## Concept card
```
Idea:        A shop counter where every price tag is checked before it reaches you.
Metaphor:    A shopkeeper's seal: nothing leaves the counter unstamped.
Signature:   Each product card settles in, then a "✓ price checked" seal stamps onto it.
             It makes the grounding check (step 5 of the pipeline) visible.
Emotion:     Calm and trustworthy. Dark, warm amber, nothing loud.
Motion verb: Stamp and settle.
Screenshot:  The results panel with three cards, each carrying its green seal.
```

## System
| Role | Value |
|---|---|
| Ground | `#08090c` (bg), `#12141a` (panel) |
| Ink | `#ecedf1` |
| Muted | `#9aa1ae` |
| Line | `#23272f` |
| Accent | `#f2a93b` (amber, prices and focus) |
| Verified | `#34d399` (the seal) |

- **Type:** Inter (UI), Instrument Serif (hero italic, prices), JetBrains Mono (IDs, seal, meta).
- **Shape:** cards 14px radius, pills 999px, the seal 6px.
- **Motion:** one ease, `cubic-bezier(.2,.7,.2,1)`. Cards settle in 550ms with a 70ms stagger; the seal lands 380ms after its card.
- **Idle:** the amber glow in the hero breathes over 6s.
- **States:** buttons lift on hover, press down on click, and show an amber ring on keyboard focus.
- **Reduced motion:** every animation and transition is off; all content stays visible.
- **Copy:** no em dashes in visible text.

## Limits (decided on purpose)
Streamlit controls the page shell, so there is no page transition, loader, GSAP or smooth scroll. The craft lives in CSS only.
