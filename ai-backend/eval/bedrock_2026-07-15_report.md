> **Provenance (added when archiving):** CI eval run 29422605790, 2026-07-15, the first
> complete full-suite CI run. Provider: **Bedrock** (Opus calls executed on Opus 4.6 — see
> docs/BEDROCK.md), commit a62e404. All 31 benchmarks executed, zero API errors, ~44 min.
> This run minted `baseline.json` (the CI gate target while CI runs on Bedrock); the prior
> Anthropic-provider baseline is preserved as `baseline_anthropic.json` — never compare
> across providers. Raw dump: `bedrock_2026-07-15_run.json`. The listed run cost is a
> list-price/cold-cache ESTIMATE; actual AWS billing is lower with prompt caching.

# Animora eval scorecard

**Result: 24/31 passed**
**Mean critic score: 0.92**
**First-step accuracy: 100%** (28/28)
**Composition critic score: 0.94** (the MCP's known weak spot)
**Run cost: $21.7220** (mean $0.7007/benchmark, list prices, cold cache)
**Quality per dollar: 0.0** (mean critic ÷ run cost)

## By category

| category | pass rate | mean critic | target | mean cost | passed / total |
|---|---|---|---|---|---|
| asset | 67% | 0.80 | BELOW (pass 67% < 70%) | $0.2812 | 2 / 3 |
| character | 100% | 1.00 | MET | $1.2174 | 2 / 2 |
| composition | 60% | 0.94 | BELOW (pass 60% < 70%) | $0.9712 | 3 / 5 |
| furniture | 67% | 0.91 | BELOW (pass 67% < 80%) | $0.7824 | 6 / 9 |
| lighting | 100% | 1.00 | MET | $0.2999 | 1 / 1 |
| primitive | 100% | 0.91 | MET | $0.1715 | 5 / 5 |
| question | 100% | 0.64 | BELOW (critic 0.64 < 0.70) | $0.0008 | 1 / 1 |
| scene | 100% | 1.00 | MET | $0.9694 | 3 / 3 |
| vehicle | 50% | 0.95 | BELOW (pass 50% < 60%) | $1.2401 | 1 / 2 |

## All benchmarks

| benchmark | result | critic | first step | output toks | cost | issues |
|---|---|---|---|---|---|---|
| primitive.cube | PASS | 0.86 | ok | 787 | $0.1214 | — |
| primitive.cuboid | PASS | 1.00 | ok | 596 | $0.0658 | — |
| primitive.sphere | PASS | 0.86 | ok | 283 | $0.0340 | — |
| primitive.cylinder | PASS | 0.86 | ok | 767 | $0.1131 | — |
| furniture.chair.low_poly | PASS | 1.00 | ok | 2944 | $0.5157 | — |
| vehicle.car.basic | PASS | 1.00 | ok | 7662 | $1.1685 | — |
| vehicle.car.lambo_urus | FAIL | 0.91 | — | 9629 | $1.3116 | missing op `light_add\(|light\.energy\s*=|bpy\.data\.lights\.new\(|ShaderNodeTexEnvironment|EnvironmentTexture` |
| character.dragon | PASS | 1.00 | ok | 6868 | $1.0403 | — |
| scene.beach | PASS | 1.00 | ok | 4588 | $0.8211 | — |
| lighting.studio | PASS | 1.00 | ok | 1766 | $0.2999 | — |
| question.bsdf | PASS | 0.64 | — | 156 | $0.0008 | — |
| primitive.sphere.creative_safety_net | PASS | 1.00 | ok | 3831 | $0.5230 | — |
| composition.still_life | PASS | 1.00 | ok | 5447 | $0.9374 | over token budget (5447 > 5000) |
| composition.depth_layering | PASS | 1.00 | ok | 7060 | $1.1741 | — |
| composition.three_point_lighting | FAIL | 0.69 | ok | 6461 | $0.8743 | no meaningful .name= assignment; over token budget (6461 > 3500) |
| composition.rule_of_thirds | PASS | 1.00 | ok | 5659 | $0.9481 | — |
| composition.grounded_furniture | FAIL | 1.00 | ok | 6463 | $0.9222 | no modifiers added (raw primitives only) |
| character.standing_figure | PASS | 1.00 | ok | 8816 | $1.3946 | over token budget (8816 > 8000) |
| asset.hdri_lighting | PASS | 0.64 | — | 502 | $0.0603 | — |
| asset.texture_application | FAIL | 0.76 | ok | 2813 | $0.4753 | no Principled BSDF material setup |
| asset.reference_mesh | PASS | 1.00 | ok | 2477 | $0.3079 | — |
| furniture.chair.wooden_hero | PASS | 1.00 | ok | 5939 | $0.9859 | — |
| furniture.coffee_table.hero | PASS | 1.00 | ok | 3527 | $0.6038 | — |
| furniture.sofa.modern | PASS | 1.00 | ok | 8938 | $1.3237 | over token budget (8938 > 8000) |
| furniture.floor_lamp | FAIL | 0.70 | ok | 1433 | $0.2181 | missing op `light_add\(|lights\.new\(|create_light`; only 0 light sources (need >= 1) |
| furniture.bookshelf | PASS | 1.00 | ok | 6462 | $1.0891 | over token budget (6462 > 6000) |
| scene.warm_evening_beach | PASS | 1.00 | ok | 5307 | $0.9541 | — |
| scene.cozy_living_room | PASS | 1.00 | ok | 7765 | $1.1331 | — |
| furniture.sideboard.luxury_vintage | FAIL | 0.52 | ok | 2469 | $0.3212 | only 9 distinct objects (need >= 10) |
| furniture.chair.modern_minimalist | PASS | 1.00 | ok | 3998 | $0.6652 | — |
| furniture.shelf.industrial | FAIL | 1.00 | ok | 8100 | $1.3188 | missing op `metallic\s*=\s*1\.0|metallic=1\.0`; over token budget (8100 > 6500) |

## Per-benchmark detail

### primitive.cube — PASS
- prompt: `create a cube`
- output_tokens (est): 787
- script_length: 0 chars
- truncated: False
- validator: ok
- named: False, material: True

### primitive.cuboid — PASS
- prompt: `create a cuboid 2m wide, 1m tall, 0.5m deep, at origin`
- output_tokens (est): 596
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True

### primitive.sphere — PASS
- prompt: `add a UV sphere of radius 0.5 m at the origin`
- output_tokens (est): 283
- script_length: 0 chars
- truncated: False
- validator: ok
- named: False, material: True

### primitive.cylinder — PASS
- prompt: `add a cylinder with radius 0.3m and height 1m`
- output_tokens (est): 767
- script_length: 0 chars
- truncated: False
- validator: ok
- named: False, material: True

### furniture.chair.low_poly — PASS
- prompt: `Add a low-poly chair`
- output_tokens (est): 2944
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True

### vehicle.car.basic — PASS
- prompt: `Make a car`
- output_tokens (est): 7662
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True

### vehicle.car.lambo_urus — FAIL
- prompt: `Build me a hero Lamborghini Urus, studio-shot quality`
- output_tokens (est): 9629
- script_length: 2330 chars
- truncated: False
- validator: ok
- named: True, material: True
- issues:
  - missing op `light_add\(|light\.energy\s*=|bpy\.data\.lights\.new\(|ShaderNodeTexEnvironment|EnvironmentTexture`

### character.dragon — PASS
- prompt: `model a low-poly dragon`
- output_tokens (est): 6868
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True

### scene.beach — PASS
- prompt: `make a beach scene with palm trees and water`
- output_tokens (est): 4588
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True

### lighting.studio — PASS
- prompt: `light this scene like a studio product shot`
- output_tokens (est): 1766
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True

### question.bsdf — PASS
- prompt: `what is a Principled BSDF node and when should I use it?`
- output_tokens (est): 156
- script_length: 0 chars
- truncated: False
- validator: ok
- named: False, material: False

### primitive.sphere.creative_safety_net — PASS
- prompt: `add something interesting to the scene`
- output_tokens (est): 3831
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True

### composition.still_life — PASS
- prompt: `create a still life with three objects in a triangular composition on a table`
- output_tokens (est): 5447
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True
- issues:
  - over token budget (5447 > 5000)

### composition.depth_layering — PASS
- prompt: `build an outdoor scene with a tree in the foreground, a house in the midground, and mountains in the background`
- output_tokens (est): 7060
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True

### composition.three_point_lighting — FAIL
- prompt: `set up a three-point lighting rig for a portrait — key, fill, and rim lights`
- output_tokens (est): 6461
- script_length: 0 chars
- truncated: False
- validator: ok
- named: False, material: False
- issues:
  - no meaningful .name= assignment
  - over token budget (6461 > 3500)

### composition.rule_of_thirds — PASS
- prompt: `create a landscape scene with the horizon on the lower third of the frame and a focal subject at a rule-of-thirds intersection`
- output_tokens (est): 5659
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True

### composition.grounded_furniture — FAIL
- prompt: `create an interior room with a desk, a chair, and a lamp — every object grounded on the floor (no floating)`
- output_tokens (est): 6463
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True
- issues:
  - no modifiers added (raw primitives only)

### character.standing_figure — PASS
- prompt: `model a stylized human standing in T-pose with proper proportions`
- output_tokens (est): 8816
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True
- issues:
  - over token budget (8816 > 8000)

### asset.hdri_lighting — PASS
- prompt: `set up the scene with a warm golden-hour outdoor HDRI for lighting`
- output_tokens (est): 502
- script_length: 0 chars
- truncated: False
- validator: ok
- named: False, material: False

### asset.texture_application — FAIL
- prompt: `create a square table and apply a realistic weathered wood texture to it`
- output_tokens (est): 2813
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: False
- issues:
  - no Principled BSDF material setup

### asset.reference_mesh — PASS
- prompt: `add a modern chair to the scene as a placeholder furniture piece`
- output_tokens (est): 2477
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True

### furniture.chair.wooden_hero — PASS
- prompt: `Build a wooden chair`
- output_tokens (est): 5939
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True

### furniture.coffee_table.hero — PASS
- prompt: `Build a wooden coffee table`
- output_tokens (est): 3527
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True

### furniture.sofa.modern — PASS
- prompt: `Build a modern three-seat sofa`
- output_tokens (est): 8938
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True
- issues:
  - over token budget (8938 > 8000)

### furniture.floor_lamp — FAIL
- prompt: `Build a floor lamp with a bulb`
- output_tokens (est): 1433
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True
- issues:
  - missing op `light_add\(|lights\.new\(|create_light`
  - only 0 light sources (need >= 1)

### furniture.bookshelf — PASS
- prompt: `Build a wooden bookshelf`
- output_tokens (est): 6462
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True
- issues:
  - over token budget (6462 > 6000)

### scene.warm_evening_beach — PASS
- prompt: `Build a warm evening beach`
- output_tokens (est): 5307
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True

### scene.cozy_living_room — PASS
- prompt: `Build a cozy living room`
- output_tokens (est): 7765
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True

### furniture.sideboard.luxury_vintage — FAIL
- prompt: `Build a luxury vintage wooden sideboard`
- output_tokens (est): 2469
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True
- issues:
  - only 9 distinct objects (need >= 10)

### furniture.chair.modern_minimalist — PASS
- prompt: `Build a modern minimalist chair`
- output_tokens (est): 3998
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True

### furniture.shelf.industrial — FAIL
- prompt: `Build an industrial metal shelf`
- output_tokens (est): 8100
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True
- issues:
  - missing op `metallic\s*=\s*1\.0|metallic=1\.0`
  - over token budget (8100 > 6500)