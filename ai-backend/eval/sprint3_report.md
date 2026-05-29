# Animora eval scorecard

**Result: 16/21 passed**

## By category

| category | pass rate | passed / total |
|---|---|---|
| asset | 100% | 3 / 3 |
| character | 50% | 1 / 2 |
| composition | 60% | 3 / 5 |
| furniture | 100% | 1 / 1 |
| lighting | 0% | 0 / 1 |
| primitive | 80% | 4 / 5 |
| question | 100% | 1 / 1 |
| scene | 100% | 1 / 1 |
| vehicle | 100% | 2 / 2 |

## All benchmarks

| benchmark | result | output toks | script len | issues |
|---|---|---|---|---|
| primitive.cube | PASS | 2888 | 7858 | over token budget (2888 > 1500) |
| primitive.cuboid | FAIL | 236 | 0 | missing op `primitive_cube_add\(`; no meaningful .name= assignment |
| primitive.sphere | PASS | 991 | 2856 | — |
| primitive.cylinder | PASS | 1266 | 3105 | — |
| furniture.chair.low_poly | PASS | 3059 | 8333 | — |
| vehicle.car.basic | PASS | 0 | 0 | — |
| vehicle.car.lambo_urus | PASS | 6342 | 17521 | — |
| character.dragon | PASS | 6238 | 17498 | — |
| scene.beach | PASS | 5661 | 15473 | — |
| lighting.studio | FAIL | 262 | 0 | missing op `light_add\(|lights\.new\(` |
| question.bsdf | PASS | 744 | 0 | — |
| primitive.sphere.creative_safety_net | PASS | 205 | 0 | — |
| composition.still_life | PASS | 4525 | 12152 | — |
| composition.depth_layering | FAIL | 490 | 0 | no meaningful .name= assignment; no Principled BSDF material setup; only 0 distinct objects (need >= 3); only 0 distinct positions (need >= 3) — everything stacked?; single-material setup (need >= 2 distinct materials) |
| composition.three_point_lighting | PASS | 3461 | 9162 | — |
| composition.rule_of_thirds | FAIL | 352 | 0 | no meaningful .name= assignment; no Principled BSDF material setup; only 0 distinct objects (need >= 2); only 0 distinct positions (need >= 2) — everything stacked?; single-material setup (need >= 2 distinct materials) |
| composition.grounded_furniture | PASS | 5548 | 15287 | — |
| character.standing_figure | FAIL | 376 | 0 | no meaningful .name= assignment; no Principled BSDF material setup; only 0 distinct objects (need >= 1); no modifiers added (raw primitives only) |
| asset.hdri_lighting | PASS | 65 | 0 | — |
| asset.texture_application | PASS | 2416 | 6386 | — |
| asset.reference_mesh | PASS | 67 | 0 | — |

## Per-benchmark detail

### primitive.cube — PASS
- prompt: `create a cube`
- output_tokens (est): 2888
- script_length: 7858 chars
- truncated: False
- validator: ok
- named: True, material: True
- issues:
  - over token budget (2888 > 1500)

### primitive.cuboid — FAIL
- prompt: `create a cuboid 2m wide, 1m tall, 0.5m deep, at origin`
- output_tokens (est): 236
- script_length: 0 chars
- truncated: False
- validator: ok
- named: False, material: False
- issues:
  - missing op `primitive_cube_add\(`
  - no meaningful .name= assignment

### primitive.sphere — PASS
- prompt: `add a UV sphere of radius 0.5 m at the origin`
- output_tokens (est): 991
- script_length: 2856 chars
- truncated: False
- validator: ok
- named: True, material: True

### primitive.cylinder — PASS
- prompt: `add a cylinder with radius 0.3m and height 1m`
- output_tokens (est): 1266
- script_length: 3105 chars
- truncated: False
- validator: ok
- named: True, material: True

### furniture.chair.low_poly — PASS
- prompt: `Add a low-poly chair`
- output_tokens (est): 3059
- script_length: 8333 chars
- truncated: False
- validator: ok
- named: True, material: True

### vehicle.car.basic — PASS
- prompt: `Make a car`
- output_tokens (est): 0
- script_length: 0 chars
- truncated: False
- validator: ok
- named: False, material: False

### vehicle.car.lambo_urus — PASS
- prompt: `Build me a hero Lamborghini Urus, studio-shot quality`
- output_tokens (est): 6342
- script_length: 17521 chars
- truncated: False
- validator: ok
- named: True, material: True

### character.dragon — PASS
- prompt: `model a low-poly dragon`
- output_tokens (est): 6238
- script_length: 17498 chars
- truncated: False
- validator: ok
- named: True, material: True

### scene.beach — PASS
- prompt: `make a beach scene with palm trees and water`
- output_tokens (est): 5661
- script_length: 15473 chars
- truncated: False
- validator: ok
- named: True, material: True

### lighting.studio — FAIL
- prompt: `light this scene like a studio product shot`
- output_tokens (est): 262
- script_length: 0 chars
- truncated: False
- validator: ok
- named: False, material: False
- issues:
  - missing op `light_add\(|lights\.new\(`

### question.bsdf — PASS
- prompt: `what is a Principled BSDF node and when should I use it?`
- output_tokens (est): 744
- script_length: 0 chars
- truncated: False
- validator: ok
- named: False, material: False

### primitive.sphere.creative_safety_net — PASS
- prompt: `add something interesting to the scene`
- output_tokens (est): 205
- script_length: 0 chars
- truncated: False
- validator: ok
- named: False, material: False

### composition.still_life — PASS
- prompt: `create a still life with three objects in a triangular composition on a table`
- output_tokens (est): 4525
- script_length: 12152 chars
- truncated: False
- validator: ok
- named: True, material: True

### composition.depth_layering — FAIL
- prompt: `build an outdoor scene with a tree in the foreground, a house in the midground, and mountains in the background`
- output_tokens (est): 490
- script_length: 0 chars
- truncated: False
- validator: ok
- named: False, material: False
- issues:
  - no meaningful .name= assignment
  - no Principled BSDF material setup
  - only 0 distinct objects (need >= 3)
  - only 0 distinct positions (need >= 3) — everything stacked?
  - single-material setup (need >= 2 distinct materials)

### composition.three_point_lighting — PASS
- prompt: `set up a three-point lighting rig for a portrait — key, fill, and rim lights`
- output_tokens (est): 3461
- script_length: 9162 chars
- truncated: False
- validator: ok
- named: True, material: True

### composition.rule_of_thirds — FAIL
- prompt: `create a landscape scene with the horizon on the lower third of the frame and a focal subject at a rule-of-thirds intersection`
- output_tokens (est): 352
- script_length: 0 chars
- truncated: False
- validator: ok
- named: False, material: False
- issues:
  - no meaningful .name= assignment
  - no Principled BSDF material setup
  - only 0 distinct objects (need >= 2)
  - only 0 distinct positions (need >= 2) — everything stacked?
  - single-material setup (need >= 2 distinct materials)

### composition.grounded_furniture — PASS
- prompt: `create an interior room with a desk, a chair, and a lamp — every object grounded on the floor (no floating)`
- output_tokens (est): 5548
- script_length: 15287 chars
- truncated: False
- validator: ok
- named: True, material: True

### character.standing_figure — FAIL
- prompt: `model a stylized human standing in T-pose with proper proportions`
- output_tokens (est): 376
- script_length: 0 chars
- truncated: False
- validator: ok
- named: False, material: False
- issues:
  - no meaningful .name= assignment
  - no Principled BSDF material setup
  - only 0 distinct objects (need >= 1)
  - no modifiers added (raw primitives only)

### asset.hdri_lighting — PASS
- prompt: `set up the scene with a warm golden-hour outdoor HDRI for lighting`
- output_tokens (est): 65
- script_length: 0 chars
- truncated: False
- validator: ok
- named: False, material: False

### asset.texture_application — PASS
- prompt: `create a square table and apply a realistic weathered wood texture to it`
- output_tokens (est): 2416
- script_length: 6386 chars
- truncated: False
- validator: ok
- named: True, material: True

### asset.reference_mesh — PASS
- prompt: `add a modern chair to the scene as a placeholder furniture piece`
- output_tokens (est): 67
- script_length: 0 chars
- truncated: False
- validator: ok
- named: False, material: False