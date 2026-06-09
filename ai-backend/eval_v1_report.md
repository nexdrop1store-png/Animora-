# Animora eval scorecard

**Result: 26/31 passed**
**Mean critic score: 0.92**
**First-step accuracy: 100%** (29/29)
**Composition critic score: 0.94** (the MCP's known weak spot)
**Run cost: $20.7037** (mean $0.6679/benchmark, list prices, cold cache)
**Quality per dollar: 0.0** (mean critic ÷ run cost)

## By category

| category | pass rate | mean critic | target | mean cost | passed / total |
|---|---|---|---|---|---|
| asset | 67% | 0.80 | BELOW (pass 67% < 70%) | $0.3759 | 2 / 3 |
| character | 100% | 1.00 | MET | $1.3299 | 2 / 2 |
| composition | 100% | 0.94 | MET | $0.8461 | 5 / 5 |
| furniture | 67% | 0.94 | BELOW (pass 67% < 80%) | $0.8173 | 6 / 9 |
| lighting | 100% | 1.00 | MET | $0.2415 | 1 / 1 |
| primitive | 100% | 0.91 | MET | $0.1345 | 5 / 5 |
| question | 100% | 0.64 | BELOW (critic 0.64 < 0.70) | $0.0021 | 1 / 1 |
| scene | 100% | 1.00 | MET | $0.8570 | 3 / 3 |
| vehicle | 50% | 0.88 | BELOW (pass 50% < 60%) | $0.9217 | 1 / 2 |

## All benchmarks

| benchmark | result | critic | first step | output toks | cost | issues |
|---|---|---|---|---|---|---|
| primitive.cube | PASS | 0.86 | ok | 306 | $0.0427 | — |
| primitive.cuboid | PASS | 1.00 | ok | 357 | $0.0478 | — |
| primitive.sphere | PASS | 0.86 | ok | 283 | $0.0340 | — |
| primitive.cylinder | PASS | 0.86 | ok | 286 | $0.0341 | — |
| furniture.chair.low_poly | PASS | 1.00 | ok | 3057 | $0.5218 | — |
| vehicle.car.basic | PASS | 1.00 | ok | 7718 | $1.1811 | — |
| vehicle.car.lambo_urus | FAIL | 0.76 | ok | 5214 | $0.6622 | missing op `light_add\(|light\.energy\s*=|bpy\.data\.lights\.new\(|ShaderNodeTexEnvironment|EnvironmentTexture` |
| character.dragon | PASS | 1.00 | ok | 7303 | $1.1007 | — |
| scene.beach | PASS | 1.00 | ok | 4684 | $0.7432 | — |
| lighting.studio | PASS | 1.00 | ok | 1584 | $0.2415 | — |
| question.bsdf | PASS | 0.64 | — | 414 | $0.0021 | — |
| primitive.sphere.creative_safety_net | PASS | 1.00 | ok | 3265 | $0.5138 | — |
| composition.still_life | PASS | 1.00 | ok | 5548 | $0.9059 | over token budget (5548 > 5000) |
| composition.depth_layering | PASS | 1.00 | ok | 5320 | $0.8601 | — |
| composition.three_point_lighting | PASS | 0.69 | ok | 2974 | $0.4620 | — |
| composition.rule_of_thirds | PASS | 1.00 | ok | 6016 | $1.0831 | over token budget (6016 > 6000) |
| composition.grounded_furniture | PASS | 1.00 | ok | 6887 | $0.9195 | — |
| character.standing_figure | PASS | 1.00 | ok | 9697 | $1.5591 | over token budget (9697 > 8000) |
| asset.hdri_lighting | PASS | 0.64 | — | 434 | $0.0522 | — |
| asset.texture_application | FAIL | 0.76 | ok | 2918 | $0.4872 | no Principled BSDF material setup |
| asset.reference_mesh | PASS | 1.00 | ok | 3726 | $0.5881 | — |
| furniture.chair.wooden_hero | PASS | 1.00 | ok | 6957 | $1.0564 | — |
| furniture.coffee_table.hero | PASS | 1.00 | ok | 5332 | $0.8439 | — |
| furniture.sofa.modern | PASS | 1.00 | ok | 9559 | $1.5147 | over token budget (9559 > 8000) |
| furniture.floor_lamp | FAIL | 0.70 | ok | 1308 | $0.1971 | missing op `light_add\(|lights\.new\(|create_light`; only 0 light sources (need >= 1) |
| furniture.bookshelf | PASS | 1.00 | ok | 7285 | $1.1646 | over token budget (7285 > 6000) |
| scene.warm_evening_beach | PASS | 1.00 | ok | 4741 | $0.8435 | — |
| scene.cozy_living_room | PASS | 1.00 | ok | 7716 | $0.9844 | — |
| furniture.sideboard.luxury_vintage | FAIL | 0.76 | ok | 2149 | $0.2762 | only 6 distinct objects (need >= 10); single-material setup (need >= 2 distinct materials) |
| furniture.chair.modern_minimalist | FAIL | 1.00 | ok | 3526 | $0.6143 | forbidden op `metallic\s*=\s*1\.0|metallic=1\.0` present |
| furniture.shelf.industrial | PASS | 1.00 | ok | 7743 | $1.1664 | over token budget (7743 > 6500) |

## Per-benchmark detail

### primitive.cube — PASS
- prompt: `create a cube`
- output_tokens (est): 306
- script_length: 0 chars
- truncated: False
- validator: ok
- named: False, material: True

### primitive.cuboid — PASS
- prompt: `create a cuboid 2m wide, 1m tall, 0.5m deep, at origin`
- output_tokens (est): 357
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
- output_tokens (est): 286
- script_length: 0 chars
- truncated: False
- validator: ok
- named: False, material: True

### furniture.chair.low_poly — PASS
- prompt: `Add a low-poly chair`
- output_tokens (est): 3057
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True

### vehicle.car.basic — PASS
- prompt: `Make a car`
- output_tokens (est): 7718
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True

### vehicle.car.lambo_urus — FAIL
- prompt: `Build me a hero Lamborghini Urus, studio-shot quality`
- output_tokens (est): 5214
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True
- issues:
  - missing op `light_add\(|light\.energy\s*=|bpy\.data\.lights\.new\(|ShaderNodeTexEnvironment|EnvironmentTexture`

### character.dragon — PASS
- prompt: `model a low-poly dragon`
- output_tokens (est): 7303
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True

### scene.beach — PASS
- prompt: `make a beach scene with palm trees and water`
- output_tokens (est): 4684
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True

### lighting.studio — PASS
- prompt: `light this scene like a studio product shot`
- output_tokens (est): 1584
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True

### question.bsdf — PASS
- prompt: `what is a Principled BSDF node and when should I use it?`
- output_tokens (est): 414
- script_length: 0 chars
- truncated: False
- validator: ok
- named: False, material: False

### primitive.sphere.creative_safety_net — PASS
- prompt: `add something interesting to the scene`
- output_tokens (est): 3265
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True

### composition.still_life — PASS
- prompt: `create a still life with three objects in a triangular composition on a table`
- output_tokens (est): 5548
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True
- issues:
  - over token budget (5548 > 5000)

### composition.depth_layering — PASS
- prompt: `build an outdoor scene with a tree in the foreground, a house in the midground, and mountains in the background`
- output_tokens (est): 5320
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True

### composition.three_point_lighting — PASS
- prompt: `set up a three-point lighting rig for a portrait — key, fill, and rim lights`
- output_tokens (est): 2974
- script_length: 578 chars
- truncated: False
- validator: ok
- named: True, material: False

### composition.rule_of_thirds — PASS
- prompt: `create a landscape scene with the horizon on the lower third of the frame and a focal subject at a rule-of-thirds intersection`
- output_tokens (est): 6016
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True
- issues:
  - over token budget (6016 > 6000)

### composition.grounded_furniture — PASS
- prompt: `create an interior room with a desk, a chair, and a lamp — every object grounded on the floor (no floating)`
- output_tokens (est): 6887
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True

### character.standing_figure — PASS
- prompt: `model a stylized human standing in T-pose with proper proportions`
- output_tokens (est): 9697
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True
- issues:
  - over token budget (9697 > 8000)

### asset.hdri_lighting — PASS
- prompt: `set up the scene with a warm golden-hour outdoor HDRI for lighting`
- output_tokens (est): 434
- script_length: 0 chars
- truncated: False
- validator: ok
- named: False, material: False

### asset.texture_application — FAIL
- prompt: `create a square table and apply a realistic weathered wood texture to it`
- output_tokens (est): 2918
- script_length: 395 chars
- truncated: False
- validator: ok
- named: True, material: False
- issues:
  - no Principled BSDF material setup

### asset.reference_mesh — PASS
- prompt: `add a modern chair to the scene as a placeholder furniture piece`
- output_tokens (est): 3726
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True

### furniture.chair.wooden_hero — PASS
- prompt: `Build a wooden chair`
- output_tokens (est): 6957
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True

### furniture.coffee_table.hero — PASS
- prompt: `Build a wooden coffee table`
- output_tokens (est): 5332
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True

### furniture.sofa.modern — PASS
- prompt: `Build a modern three-seat sofa`
- output_tokens (est): 9559
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True
- issues:
  - over token budget (9559 > 8000)

### furniture.floor_lamp — FAIL
- prompt: `Build a floor lamp with a bulb`
- output_tokens (est): 1308
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True
- issues:
  - missing op `light_add\(|lights\.new\(|create_light`
  - only 0 light sources (need >= 1)

### furniture.bookshelf — PASS
- prompt: `Build a wooden bookshelf`
- output_tokens (est): 7285
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True
- issues:
  - over token budget (7285 > 6000)

### scene.warm_evening_beach — PASS
- prompt: `Build a warm evening beach`
- output_tokens (est): 4741
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True

### scene.cozy_living_room — PASS
- prompt: `Build a cozy living room`
- output_tokens (est): 7716
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True

### furniture.sideboard.luxury_vintage — FAIL
- prompt: `Build a luxury vintage wooden sideboard`
- output_tokens (est): 2149
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True
- issues:
  - only 6 distinct objects (need >= 10)
  - single-material setup (need >= 2 distinct materials)

### furniture.chair.modern_minimalist — FAIL
- prompt: `Build a modern minimalist chair`
- output_tokens (est): 3526
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True
- issues:
  - forbidden op `metallic\s*=\s*1\.0|metallic=1\.0` present

### furniture.shelf.industrial — PASS
- prompt: `Build an industrial metal shelf`
- output_tokens (est): 7743
- script_length: 0 chars
- truncated: False
- validator: ok
- named: True, material: True
- issues:
  - over token budget (7743 > 6500)